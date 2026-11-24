"""演练记录内容库：PDF / OCR 文本按内容哈希落本地，Mongo 只存索引。

两层刻意分开，便于不连库自检：

- **文件层**（纯函数）：`{DRILL_AUDIT_STORE_DIR}/sha256/<前2位>/<sha>.{pdf,txt}`，
  写入走 `mkstemp + os.replace` 原子替换。内容寻址 ⇒ 同一份 PDF 换 file_id 重传
  也只存一份。
- **仓储层**（Motor）：`drill_audit_files`（一份文件一条，`_id=file_id`）、
  `drill_audit_weeks`（一个演练周一条，`_id=week_id`）与 `drill_audit_plans`
  （一个「部门 × 年 × 计划」一条，扫描时整批替换，供看板列出全部演练任务）。

口径：所有 Mongo/磁盘异常只记日志，按「未命中 / 未写入」处理 —— 缓存只省时，
**绝不能成为审查失败的原因**。淘汰只按容量（不设期限）：先删 PDF 保住最贵的
OCR 成果，仍超再删 TXT，按 `last_used_at` 升序（LRU），删到上限的 90% 为止。

已知盲区：上游若复用同一 file_id 替换内容，检测不到（只认 file_id，靠
`refresh=True` 兜）；淘汰（或手工删文件）后首次查询会重新下载 + OCR，属设计内代价。
"""

import asyncio
import hashlib
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from config.mongodb_conn import mongodb_manager
from config.settings import settings

logger = logging.getLogger(__name__)

FILES_COLLECTION = 'drill_audit_files'
WEEKS_COLLECTION = 'drill_audit_weeks'
PLANS_COLLECTION = 'drill_audit_plans'
_EVICT_RATIO = 0.9   # 淘汰到上限的 90%，避免压线反复触发


# ==================== 文件层（纯函数，可单测） ====================


def _root() -> Path:
  return Path(settings.DRILL_AUDIT_STORE_DIR)


def content_paths(sha: str) -> tuple[Path, Path]:
  """→ `(pdf_path, txt_path)`；按 sha 前两位分片，避免单目录过大。"""
  sha = str(sha or '')
  shard = _root() / 'sha256' / sha[:2]
  return shard / f'{sha}.pdf', shard / f'{sha}.txt'


def sha256_of(data: bytes) -> str:
  return hashlib.sha256(data or b'').hexdigest()


def _atomic_write(path: Path, data: bytes) -> None:
  path.parent.mkdir(parents=True, exist_ok=True)
  fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix='.tmp')
  try:
    with os.fdopen(fd, 'wb') as fh:
      fh.write(data)
    os.replace(tmp, path)
  except BaseException:
    Path(tmp).unlink(missing_ok=True)
    raise


def write_content(sha: str, pdf: bytes | None = None, text: str | None = None) -> dict:
  """原子写内容文件。只写非空项，返回 `{pdf_path, text_path, size_pdf, size_text, chars}`。"""
  pdf_path, text_path = content_paths(sha)
  out = {'pdf_path': '', 'text_path': '', 'size_pdf': 0, 'size_text': 0, 'chars': 0}
  if pdf:
    _atomic_write(pdf_path, pdf)
    out['pdf_path'] = str(pdf_path)
    out['size_pdf'] = len(pdf)
  if text:
    body = text.encode('utf-8')
    _atomic_write(text_path, body)
    out['text_path'] = str(text_path)
    out['size_text'] = len(body)
    out['chars'] = len(text)
  return out


def read_text(sha: str) -> str | None:
  """读 OCR 文本；文件不存在/为空返回 None。"""
  if not sha:
    return None
  try:
    text = content_paths(sha)[1].read_text('utf-8')
  except OSError:
    return None
  return text or None


def _ts(value) -> float:
  if isinstance(value, datetime):
    try:
      return value.timestamp()
    except (OverflowError, OSError, ValueError):
      return 0.0
  return 0.0


def count_bytes(rows: list[dict]) -> int:
  return sum(int(r.get('size_pdf') or 0) + int(r.get('size_text') or 0) for r in rows)


def plan_evictions(rows: list[dict], max_bytes: int) -> list[tuple[str, str]]:
  """→ `[(file_id, 'pdf'|'text')]`（纯函数）。

  先删 PDF（数 MB）保住 OCR 成果，仍超再删 TXT（几十 KB）；按 `last_used_at`
  升序，删到容量降到 `max_bytes * 0.9` 为止。只考虑存在标志为真的行。
  """
  if max_bytes <= 0:
    return []
  total = count_bytes(rows)
  if total <= max_bytes:
    return []
  target = int(max_bytes * _EVICT_RATIO)
  ordered = sorted(rows, key=lambda r: _ts(r.get('last_used_at')))
  plan: list[tuple[str, str]] = []
  for kind, size_key, present_key in (
    ('pdf', 'size_pdf', 'pdf_present'),
    ('text', 'size_text', 'text_present'),
  ):
    for row in ordered:
      if total <= target:
        break
      if not row.get(present_key):
        continue
      size = int(row.get(size_key) or 0)
      file_id = str(row.get('_id') or '')
      if size <= 0 or not file_id:
        continue
      plan.append((file_id, kind))
      total -= size
  return plan


# ==================== 仓储层（Motor，失败一律 fail-open） ====================


def _files():
  return mongodb_manager.db[FILES_COLLECTION]


def _weeks():
  return mongodb_manager.db[WEEKS_COLLECTION]


def _plans():
  return mongodb_manager.db[PLANS_COLLECTION]


def _now() -> datetime:
  return datetime.now(timezone.utc)


def _enabled() -> bool:
  return bool(settings.DRILL_AUDIT_STORE_ENABLED)


async def _touch(file_id: str) -> None:
  try:
    await _files().update_one({'_id': str(file_id)}, {'$set': {'last_used_at': _now()}})
  except Exception as e:  # noqa: BLE001 - touch 失败不影响命中
    logger.warning('[DrillAudit] 内容库 touch 失败 file_id=%s: %s', file_id, str(e)[:160])


async def load_text(file_id: str) -> str | None:
  """命中返回 OCR 文本并更新 `last_used_at`；未命中/停用/读失败返回 None。"""
  if not _enabled() or not file_id:
    return None
  try:
    doc = await _files().find_one({'_id': str(file_id)})
  except Exception as e:  # noqa: BLE001
    logger.warning('[DrillAudit] 内容库读取失败 file_id=%s: %s', file_id, str(e)[:160])
    return None
  if not doc or doc.get('status') != 'ok' or not doc.get('text_present'):
    return None
  text = await asyncio.to_thread(read_text, str(doc.get('sha256') or ''))
  if not text:
    return None
  await _touch(file_id)
  return text


async def save_file(
  file_id: str, *, pdf: bytes, text: str, dept_id: str = '', plan_id: str = '',
  week_id: str = '', letter: str = '',
) -> None:
  """写内容文件 + 索引；`pdf`/`text` 任一为空都不写（不缓存失败结果）。"""
  if not _enabled() or not file_id or not pdf or not text:
    return
  try:
    sha = sha256_of(pdf)
    written = await asyncio.to_thread(write_content, sha, pdf, text)
    now = _now()
    await _files().update_one(
      {'_id': str(file_id)},
      {
        '$set': {
          'sha256': sha,
          'size_pdf': written['size_pdf'],
          'size_text': written['size_text'],
          'chars': written['chars'],
          'pdf_path': written['pdf_path'],
          'text_path': written['text_path'],
          'pdf_present': bool(written['pdf_path']),
          'text_present': bool(written['text_path']),
          'status': 'ok',
          'dept_id': dept_id,
          'plan_id': plan_id,
          'week_id': week_id,
          'letter': letter,
          'last_used_at': now,
        },
        '$setOnInsert': {'first_seen_at': now},
      },
      upsert=True,
    )
  except Exception as e:  # noqa: BLE001
    logger.warning('[DrillAudit] 内容库写入失败 file_id=%s: %s', file_id, str(e)[:160])


async def mark_ocr_failed(
  file_id: str, *, error: str = '', dept_id: str = '', plan_id: str = '',
  week_id: str = '', letter: str = '',
) -> None:
  """记失败（下次仍会重试）；不动已有的内容文件与存在标志。"""
  if not _enabled() or not file_id:
    return
  now = _now()
  try:
    await _files().update_one(
      {'_id': str(file_id)},
      {
        '$set': {
          'status': 'ocr_failed',
          'error': str(error or '')[:300],
          'dept_id': dept_id,
          'plan_id': plan_id,
          'week_id': week_id,
          'letter': letter,
          'last_used_at': now,
        },
        '$setOnInsert': {'first_seen_at': now},
      },
      upsert=True,
    )
  except Exception as e:  # noqa: BLE001
    logger.warning('[DrillAudit] 内容库失败标记写入失败 file_id=%s: %s', file_id, str(e)[:160])


async def save_week(record: dict) -> None:
  """按 `week_id` upsert 一条演练周记录（结论 / 班组清单 / EOP 指纹）。"""
  week_id = str(record.get('_id') or record.get('week_id') or '')
  if not _enabled() or not week_id:
    return
  doc = {k: v for k, v in record.items() if k not in ('_id', 'week_id')}
  doc['audited_at'] = _now()
  try:
    await _weeks().update_one({'_id': week_id}, {'$set': doc}, upsert=True)
  except Exception as e:  # noqa: BLE001
    logger.warning('[DrillAudit] 演练周记录写入失败 week_id=%s: %s', week_id, str(e)[:160])


async def get_week(week_id: str) -> dict | None:
  if not _enabled() or not week_id:
    return None
  try:
    return await _weeks().find_one({'_id': str(week_id)})
  except Exception as e:  # noqa: BLE001
    logger.warning('[DrillAudit] 演练周记录读取失败 week_id=%s: %s', week_id, str(e)[:160])
    return None


async def list_weeks(
  *, since: str | None = None, limit: int = 200, include_issues: bool = False,
) -> list[dict]:
  """列演练周记录（页面用），按 `stime` 倒序（无则按 `audited_at`）。

  `include_issues=False` 时剔除逐条问题（列表只需统计，点开详情单独取）；
  `since`（`YYYY-MM` 前缀）按 `stime`/`etime` 收窄（空 = 全部）。异常返回 `[]`。
  """
  if not _enabled():
    return []
  projection = None if include_issues else {
    'issues': 0, 'groups.dimensions': 0, 'groups.missing_steps': 0,
    'groups.step_findings': 0, 'groups.timeline_stats': 0,
  }
  query: dict = {}
  if since:
    prefix = str(since).strip()
    if prefix:
      query = {'$or': [{'stime': {'$regex': f'^{prefix}'}},
                       {'etime': {'$regex': f'^{prefix}'}}]}
  try:
    cursor = _weeks().find(query, projection).sort(
      [('stime', -1), ('audited_at', -1)],
    ).limit(max(1, int(limit)))
    return [doc async for doc in cursor]
  except Exception as e:  # noqa: BLE001
    logger.warning('[DrillAudit] 演练周列表读取失败: %s', str(e)[:160])
    return []


async def save_plans(dept_id: str, year: int, rows: list[dict]) -> None:
  """按「部门 × 年」整批替换演练计划清单（扫描时刷新，失败只记日志）。

  `rows=[]` 视为该部门该年确实没有计划，清掉旧缓存；逐条 upsert 后再删掉本次
  接口未返回的计划（替换语义，避免下线计划永远留在矩阵里）。
  """
  if not _enabled() or not dept_id:
    return
  now = _now()
  keep: list[str] = []
  try:
    for row in rows or []:
      row = row if isinstance(row, dict) else {}
      plan_id = str(row.get('plan_id') or '')
      if not plan_id:
        continue
      keep.append(plan_id)
      await _plans().update_one(
        {'_id': f'{dept_id}|{int(year)}|{plan_id}'},
        {'$set': {
          'dept_id': str(dept_id),
          'year': int(year),
          'plan_id': plan_id,
          'dept_name': str(row.get('dept_name') or ''),
          'company_name': str(row.get('company_name') or ''),
          'plan_name': str(row.get('plan_name') or ''),
          'speciality': str(row.get('speciality') or ''),
          'weeks': row.get('weeks') or [],
          'updated_at': now,
        }},
        upsert=True,
      )
    await _plans().delete_many(
      {'dept_id': str(dept_id), 'year': int(year), 'plan_id': {'$nin': keep}},
    )
  except Exception as e:  # noqa: BLE001
    logger.warning('[DrillAudit] 演练计划清单写入失败 dept=%s year=%s: %s',
                   dept_id, year, str(e)[:160])


async def list_plans() -> list[dict]:
  """全量演练计划清单（看板矩阵的行来源），异常返回 `[]`。"""
  if not _enabled():
    return []
  try:
    cursor = _plans().find({})
    return [doc async for doc in cursor]
  except Exception as e:  # noqa: BLE001
    logger.warning('[DrillAudit] 演练计划清单读取失败: %s', str(e)[:160])
    return []


async def latest_plan_update() -> str:
  """清单最近一次刷新时间（ISO 字符串）；停用/未命中/异常返回 `''`。"""
  if not _enabled():
    return ''
  try:
    doc = await _plans().find_one({}, {'updated_at': 1}, sort=[('updated_at', -1)])
  except Exception as e:  # noqa: BLE001
    logger.warning('[DrillAudit] 演练计划刷新时间读取失败: %s', str(e)[:160])
    return ''
  value = (doc or {}).get('updated_at')
  if isinstance(value, datetime):
    try:
      return value.isoformat()
    except (OverflowError, OSError, ValueError):
      return ''
  return str(value or '')


async def evict_if_needed(max_mb: int | None = None) -> int:
  """按容量淘汰，返回本次删除的文件数；停用或异常一律 0。"""
  if not _enabled():
    return 0
  limit_mb = settings.DRILL_AUDIT_STORE_MAX_MB if max_mb is None else max_mb
  max_bytes = max(0, int(limit_mb)) * 1024 * 1024
  if max_bytes <= 0:
    return 0
  try:
    rows = [doc async for doc in _files().find({})]
    plan = plan_evictions(rows, max_bytes)
    if not plan:
      return 0
    by_id = {str(r.get('_id') or ''): r for r in rows}
    dropped: dict[str, set[str]] = {}
    for file_id, kind in plan:
      row = by_id.get(file_id)
      if not row:
        continue
      path = content_paths(str(row.get('sha256') or ''))[0 if kind == 'pdf' else 1]
      try:
        await asyncio.to_thread(path.unlink, missing_ok=True)
      except OSError as e:  # noqa: BLE001 - 文件已不在也算删掉
        logger.warning('[DrillAudit] 淘汰删除失败 %s: %s', path, str(e)[:120])
      dropped.setdefault(file_id, set()).add(kind)
    for file_id, kinds in dropped.items():
      flags = {('pdf_present' if k == 'pdf' else 'text_present'): False for k in kinds}
      try:
        await _files().update_one({'_id': file_id}, {'$set': flags})
      except Exception as e:  # noqa: BLE001
        logger.warning('[DrillAudit] 淘汰后更新索引失败 %s: %s', file_id, str(e)[:120])
    return sum(len(k) for k in dropped.values())
  except Exception as e:  # noqa: BLE001
    logger.warning('[DrillAudit] 容量淘汰失败: %s', str(e)[:160])
    return 0


async def stats() -> dict:
  """文件数 / 占用 / 可命中数（CLI 用）。"""
  if not _enabled():
    return {'enabled': False}
  try:
    rows = [doc async for doc in _files().find(
      {}, {'status': 1, 'text_present': 1, 'size_pdf': 1, 'size_text': 1},
    )]
    week_count = await _weeks().count_documents({})
  except Exception as e:  # noqa: BLE001
    return {'enabled': True, 'error': str(e)[:200]}
  pdf_bytes = sum(int(r.get('size_pdf') or 0) for r in rows)
  text_bytes = sum(int(r.get('size_text') or 0) for r in rows)
  return {
    'enabled': True,
    'files': len(rows),
    'cached_ok': sum(1 for r in rows if r.get('status') == 'ok' and r.get('text_present')),
    'ocr_failed': sum(1 for r in rows if r.get('status') == 'ocr_failed'),
    'weeks': week_count,
    'pdf_mb': round(pdf_bytes / 1048576, 2),
    'text_mb': round(text_bytes / 1048576, 2),
    'total_mb': round((pdf_bytes + text_bytes) / 1048576, 2),
    'max_mb': settings.DRILL_AUDIT_STORE_MAX_MB,
    'dir': str(_root()),
  }

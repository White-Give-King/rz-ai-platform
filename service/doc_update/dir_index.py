"""部门目录索引：把「清单项 → 共享目录 fid」固定下来，日常不再全树遍历。

共享文件接口只有「按 parentId 列目录」，没有「按名字查 id」，所以根以下每层的
fid 必须先列一次。全量遍历一次后，把每项命中的**稳定目录**（最浅的别名/编号目录）
连同 fid 落库；之后只从这些目录往下遍历 —— 廊坊 A1 实测 1679 个目录会收敛到几十个。

**只存稳定目录，不存每月新建的叶子月目录**：月份目录每月变化，存进去只会让索引膨胀，
且下个月的新目录不在任何已缓存子树里（永远不会被发现）。所以命中的目录若是期号容器
（`2025`、`2026.10`、`10月`、`润泽…【2025年1月总第107期】`、`2025年考勤`），
一律上浮到最近的稳定祖先；被祖先覆盖的子目录根也一并去掉。

集合 `doc_update_dir_index` 一条 = 一个部门：
`{_id, company_id, dept_id, dept_name, roots: [{seq, fid, segments}], full_scan_at}`
"""

import re
from datetime import datetime, timezone

from config.mongodb_conn import mongodb_manager
from config.settings import settings
from service.doc_update.catalog import excluded_segments

COLLECTION = 'doc_update_dir_index'


def _coll():
  return mongodb_manager.db[COLLECTION]


def key(company_id: str, dept_id: str) -> str:
  return f'{company_id or ""}:{dept_id or ""}'


def _norm(text) -> str:
  return str(text or '').replace('\u3000', ' ').strip()


def _compact(text) -> str:
  return ''.join(str(text or '').replace('\u3000', ' ').split()).lower()


def _numbered(name: str, numbers) -> bool:
  """编号前缀匹配，后面不能再接数字或点（`1.6.6.1` 不命中 `1.6.6.10`）。"""
  text = _compact(name)
  for number in numbers or ():
    if text.startswith(number):
      rest = text[len(number):]
      if not (rest[:1].isdigit() or rest[:1] == '.'):
        return True
  return False


def _alias(name: str, folders) -> bool:
  text = _compact(name)
  return bool(text) and any(
    alias and alias in text for alias in (_compact(f) for f in folders or ())
  )


_YEAR_ONLY = re.compile(r'^(?:19|20|21)\d{2}\s*年?$')
_YEAR_PREFIX = re.compile(r'^(?:19|20|21)\d{2}\s*年')            # 2025年考勤 / 2026年隐患排查记录表
_MONTH_ONLY = re.compile(r'^\d{1,2}\s*月(?:份)?$')               # 10月 / 10月份
_YEAR_MONTH = re.compile(r'^(?:19|20|21)\d{2}\s*[-_/年.]\s*\d{1,2}\s*月?(?:份)?$')
_EMBEDDED_YM = re.compile(r'(?:19|20|21)\d{2}\s*年\s*\d{1,2}\s*月')  # …【2025年1月总第107期】
_PAREN_YEAR = re.compile(r'[（(]\s*(?:19|20|21)\d{2}\s*[)）]')        # A9-卫生清洁记录表（2026）
_YEAR_SUFFIX = re.compile(r'(?<!\d)20\d{2}\s*年?\s*$')               # 卫生清洁记录2026


def _dated(name: str) -> bool:
  """是否是期号容器目录（年/月目录或每月新建的归档目录）。

  这些目录每月/每年新建一个，固化进索引会导致下个月的新目录不被遍历。
  """
  text = _norm(name)
  if not text:
    return False
  return bool(
    _YEAR_ONLY.match(text) or _YEAR_PREFIX.match(text)
    or _MONTH_ONLY.match(text) or _YEAR_MONTH.match(text)
    or _EMBEDDED_YM.search(text) or _PAREN_YEAR.search(text)
    or _YEAR_SUFFIX.search(text)
  )


# `paths.py` 判断「末段是不是期号容器」要用同一套口径，故公开这个别名
is_period_dir = _dated


def _root_node(item, chain: list[dict]) -> dict | None:
  """最浅的「名字命中该项别名/编号」的目录；没有就退回文件所在父目录。

  命中的是期号容器时上浮到最近的稳定祖先（顶层目录不再上浮）。
  """
  if not chain:
    return None
  idx = next(
    (i for i, node in enumerate(chain)
     if _alias(node.get('name') or '', item.folders)
     or _numbered(node.get('name') or '', item.numbers)),
    len(chain) - 1,
  )
  while idx > 0 and _dated(chain[idx].get('name') or ''):
    idx -= 1
  return chain[idx]


def roots_for_item(item, matched: list[dict]) -> list[dict]:
  """一项的匹配结果 → 要固化的目录根 `[{seq, fid, segments}]`（按 fid 去重）。

  只取稳定目录（最浅的别名/编号目录），不取每月新建的叶子月目录。
  """
  out: list[dict] = []
  seen: set[str] = set()
  for f in matched:
    chain = f.get('chain') or []
    if excluded_segments(item, [c.get('name') or '' for c in chain]):
      continue
    node = _root_node(item, chain)
    fid = str((node or {}).get('fid') or '')
    if not fid or fid in seen:
      continue
    seen.add(fid)
    idx = next((i for i, c in enumerate(chain) if c.get('fid') == fid), len(chain) - 1)
    out.append({
      'seq': item.seq,
      'fid': fid,
      'segments': [c.get('name') or '' for c in chain[:idx + 1]],
    })
  return out


def _prune(roots: list[dict]) -> list[dict]:
  """去掉被别的根覆盖的子目录（只留最浅的祖先，避免重复遍历同一子树）。"""
  kept: list[dict] = []
  for root in sorted(roots, key=lambda r: (len(r.get('segments') or []), r.get('fid') or '')):
    segs = list(root.get('segments') or [])
    if any(segs[:len(k['segments'])] == k['segments']
           for k in kept if k.get('segments')):
      continue
    kept.append(root)
  return kept


def pick_roots(pairs) -> list[dict]:
  """`[(item, matched), …]` → 整个部门的目录根清单（跨项去重 + 去子根）。"""
  out: list[dict] = []
  seen: set[tuple[int, str]] = set()
  for item, matched in pairs:
    for root in roots_for_item(item, matched):
      pair = (root['seq'], root['fid'])
      if pair in seen:
        continue
      seen.add(pair)
      out.append(root)
  return sorted(_prune(out), key=lambda r: (r.get('seq') or 0, r.get('fid') or ''))


def sibling_roots(items, children: list[dict]) -> list[dict]:
  """部门根这一层的直接子目录 → 新增的别名目录根。

  顶层归档目录（`…能源报告【2025年1月总第107期】`）每月新建一个兄弟目录，
  不在任何已缓存的子树里；每轮只列部门根这一层即可发现，不必全树遍历。
  """
  out: list[dict] = []
  seen: set[tuple[int, str]] = set()
  for item in items:
    for child in children:
      if not child.get('is_dir'):
        continue
      name = child.get('name') or ''
      fid = str(child.get('fid') or '')
      if not fid:
        continue
      if not (_alias(name, item.folders) or _numbered(name, item.numbers)):
        continue
      pair = (item.seq, fid)
      if pair in seen:
        continue
      seen.add(pair)
      out.append({'seq': item.seq, 'fid': fid, 'segments': [name]})
  return out


def merge_roots(existing: list[dict], new: list[dict], seqs: set[int]) -> list[dict]:
  """替换 `seqs` 这些项的根，其余项的根原样保留（单项刷新不会丢别的项）。"""
  kept = [r for r in existing or [] if r.get('seq') not in seqs]
  seen = {(r.get('seq'), r.get('fid')) for r in kept}
  for root in new or []:
    pair = (root.get('seq'), root.get('fid'))
    if pair in seen:
      continue
    seen.add(pair)
    kept.append(root)
  return sorted(_prune(kept), key=lambda r: (r.get('seq') or 0, r.get('fid') or ''))


def drop_excluded(roots: list[dict], items) -> list[dict]:
  """丢掉 segments 命中「对应 seq 的作废路径」的根；其余根与顺序保持原样。"""
  by_seq = {item.seq: item for item in items or ()}
  kept: list[dict] = []
  for root in roots or ():
    item = by_seq.get(root.get('seq'))
    if item is not None and excluded_segments(item, root.get('segments') or []):
      continue
    kept.append(root)
  return kept


def _aware(value):
  if isinstance(value, datetime) and value.tzinfo is None:
    return value.replace(tzinfo=timezone.utc)
  return value


def should_full_scan(doc: dict | None, now: datetime | None = None) -> bool:
  """是否需要全量遍历：没有索引、没有根、或超过 TTL（<=0 表示不按时间自动全量）。"""
  if not doc or not (doc.get('roots') or []):
    return True
  ttl_days = int(settings.DOC_UPDATE_INDEX_TTL_DAYS or 0)
  if ttl_days <= 0:
    return False
  scanned = _aware(doc.get('full_scan_at'))
  if not isinstance(scanned, datetime):
    return True
  ref = now or datetime.now(timezone.utc)
  return (ref - scanned).total_seconds() > ttl_days * 86400


def rescan_allowed(doc: dict | None, now: datetime | None = None) -> bool:
  """「未找到 / 根列举失败」时回退全量的最短间隔（防每轮都对同一个部门全量）。"""
  hours = max(1, int(settings.DOC_UPDATE_INDEX_RESCAN_HOURS or 24))
  scanned = _aware((doc or {}).get('full_scan_at'))
  if not isinstance(scanned, datetime):
    return True
  ref = now or datetime.now(timezone.utc)
  return (ref - scanned).total_seconds() > hours * 3600


async def load(company_id: str, dept_id: str) -> dict | None:
  if not dept_id:
    return None
  return await _coll().find_one({'_id': key(company_id, dept_id)})


async def save(company_id: str, dept_id: str, dept_name: str,
               roots: list[dict], *, full_scan: bool = False) -> None:
  """写索引；`full_scan=True` 刷新 `full_scan_at`（增量遍历只合并不刷新）。"""
  if not dept_id:
    return
  now = datetime.now(timezone.utc)
  update = {
    '$set': {
      'company_id': company_id or '',
      'dept_id': dept_id,
      'dept_name': dept_name or '',
      'roots': roots or [],
      'updated_at': now,
    },
  }
  if full_scan:
    update['$set']['full_scan_at'] = now
  await _coll().update_one({'_id': key(company_id, dept_id)}, update, upsert=True)

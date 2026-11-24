"""共享文档目录遍历（部门 rootFileId 之下）。

接口：`GET /file/share/v2/{parentId}/2/{page}/{pageSize}` → `data.shareFileVOList[]`，
字段 `{fid, name, type, parentId, createTime, size}`。

**两条实测硬约束**：
- `type == '1'` 是文件夹，**其余一律当文件** —— 共享文件节点的 `type` 常常是空串 `''`，
  不是普通整树接口里的 `'0'`；
- `shareFileVOList` 可能为 `null` → 当空目录。

编号剪枝（fail-open）：文件夹名前导数字串与**全部目标编号**都明显不兼容时才跳过
（如目标是 `1.4.5`、目录是 `2.x`）；`1.4.5` 与 `1.4.5.1` 视为兼容，无数字名字一律下钻。
**年份目录与月份目录（`2026年` / `2026.10` / `10月`）不参与剪枝** —— 它们是月度文件的
容器，当成章节编号会被整棵跳过：实测 `1.4.5能源管理报告（能源月报同步）/能源月报/2026年/10月`
与 `4.1.5隐患排查治理/2026年隐患排查记录表/10月` 里的文件都因此从未进入候选集。
宁可多走几页，不可漏匹配。

**遍历必须有界并发**：拦路的是延迟不是条数 —— 实测 `/file/share/v2/…` 单次 1.33~1.46s，
廊坊 A1 一个部门就 ≥1679 个目录，串行约 40 分钟/部门（21 部门跑不完）。8 路并发实测
3.4 req/s、无报错，故按 `DOC_UPDATE_WALK_CONCURRENCY`（默认 8）并发列举；信号量**只包住
单次列举**，不包下钻，否则满槽时互相等子任务会死锁。
"""

import asyncio
import inspect
import logging
import re
import time

import httpx

from config.settings import settings

logger = logging.getLogger(__name__)

PAGE_SIZE = 100
_MAX_PAGES = 50        # 单目录页数安全阀（5000 条）
_PROGRESS_SECONDS = 2.0  # 进度回调节流（租约本身也按 1s 节流）

_LEADING_NUMBER = re.compile(r'^(\d+(?:\.\d+)*)')
_YEAR_FIRST = re.compile(r'^(?:19|20|21)\d{2}(?:$|\.)')
_MONTH_PREFIX = re.compile(r'^\d{1,2}\s*月')   # 10月 / 10月份 / 10 月隐患排查
_RECYCLE = re.compile(r'^回收站')              # 回收站里的文件不算「本期提交」


def _norm(text) -> str:
  return str(text or '').replace('\u3000', ' ').strip()


def _is_period_dir(number: str) -> bool:
  """四位年份开头的是归档目录（`2026` / `2026.10`），不是章节编号，不参与剪枝。"""
  return bool(_YEAR_FIRST.match(number))


def _is_period_name(name: str) -> bool:
  """月份目录（`10月` / `10月份`）也是期号容器，不是章节编号。"""
  return bool(_MONTH_PREFIX.match(_norm(name)))


def _compatible(folder_number: str, numbers: tuple[str, ...]) -> bool:
  """目录编号是否可能与任一目标编号相关（前缀关系任一方向）。"""
  for n in numbers:
    if folder_number == n:
      return True
    if n.startswith(f'{folder_number}.'):
      return True
    if folder_number.startswith(f'{n}.'):
      return True
  return False


def _should_prune(name: str, numbers: tuple[str, ...]) -> bool:
  """该目录是否因编号不兼容而跳过（年份/月份目录 fail-open 下钻）。"""
  m = _LEADING_NUMBER.match(_norm(name))
  if not (m and numbers):
    return False
  number = m.group(1)
  if _is_period_dir(number) or _is_period_name(name):
    return False
  return not _compatible(number, numbers)


_ATTEMPTS = 3


async def _list_page(parent_id: str, page: int):
  """取一页目录；传输层超时/断连退避重试（大树单页 > MGMT_TIMEOUT=30s、高并发下实测会断连）。"""
  from service.query.mgmt import http

  path = f'/file/share/v2/{parent_id}/2/{page}/{PAGE_SIZE}'
  for attempt in range(_ATTEMPTS):
    try:
      return await http.request('GET', path)
    except httpx.TransportError:
      if attempt == _ATTEMPTS - 1:
        raise
      await asyncio.sleep(attempt + 1)


async def list_dir(parent_id: str) -> list[dict]:
  """列一个目录（自动翻页）。返回 `[{fid, name, is_dir, create_time, size}]`。"""
  out: list[dict] = []
  page = 1
  while page <= _MAX_PAGES:
    data = await _list_page(parent_id, page)
    rows = (data or {}).get('shareFileVOList') if isinstance(data, dict) else None
    rows = rows or []
    for row in rows:
      fid = _norm(row.get('fid'))
      if not fid:
        continue
      out.append({
        'fid': fid,
        'name': _norm(row.get('name')),
        'is_dir': str(row.get('type')) == '1',
        'create_time': row.get('createTime'),
        'size': row.get('size'),
      })
    total = (data or {}).get('size') if isinstance(data, dict) else None
    if len(rows) < PAGE_SIZE:
      break
    if isinstance(total, int) and page * PAGE_SIZE >= total:
      break
    page += 1
  return out


def _limits() -> tuple[int, int, int]:
  """(并发数, 目录数安全阀, 深度安全阀)，全部走配置并做下限保护。"""
  return (
    max(1, int(settings.DOC_UPDATE_WALK_CONCURRENCY or 1)),
    max(1, int(settings.DOC_UPDATE_WALK_MAX_DIRS or 1)),
    max(1, int(settings.DOC_UPDATE_WALK_MAX_DEPTH or 1)),
  )


async def _emit_progress(cb, dirs: int, files: int, queued: int) -> None:
  if not cb:
    return
  try:
    out = cb(dirs, files, queued)
    if inspect.isawaitable(out):
      await out
  except Exception as exc:  # noqa: BLE001  进度回调失败不影响遍历
    logger.debug('[DocUpdate] 遍历进度回调失败: %s', exc)


async def walk(
  root_id: str,
  numbers: tuple[str, ...],
  *,
  on_progress=None,
  stats: dict | None = None,
) -> list[dict]:
  """并发递归收集 root 下所有文件（全量遍历）。"""
  return await walk_roots(
    [{'fid': root_id, 'segments': []}], numbers,
    on_progress=on_progress, stats=stats,
  )


async def walk_roots(
  roots: list[dict],
  numbers: tuple[str, ...],
  *,
  on_progress=None,
  stats: dict | None = None,
  max_dirs: int | None = None,
  max_depth: int | None = None,
) -> list[dict]:
  """从多个已知目录出发并发遍历（日常只走索引里的固定目录）。

  `roots` 为 `[{fid, segments}]`：`segments` 是该目录从部门根往下的名字链，
  用于让匹配仍能看到祖先目录名（索引根本身就是别名目录时尤其重要）。
  同一个 fid 只列一次（多根重叠不重复请求），其余与全量遍历一致。

  返回 `[{fid, topic, parent_id, path, segments, chain, create_time, size}]`，
  按 `(path, topic, fid)` 排序；`chain` 是从根到父目录的 `[{name, fid}]`（建索引用）。

  `on_progress(dirs, files, queued)` 每 ~2s 回调一次（可为协程）：`queued` 是队列里
  尚未处理完的目录数（`dirs + queued` ≈ 当前已知总目录数），供进度条在遍历阶段算百分比。
  `stats` 传入 dict 时会填 `dirs`/`files`/`truncated`：**触顶即截断，必须告警** ——
  静默截断会漏掉深层目录里的文件、把「有文件」误判成「未找到」。

  `max_dirs` / `max_depth` 传值只用于小范围探测（如 `probe.py`）；`None` 时仍取
  `settings` 的全局安全阀，日常检查行为逐字不变。
  """
  concurrency, limit_dirs, limit_depth = _limits()
  max_dirs = limit_dirs if max_dirs is None else max(1, int(max_dirs))
  max_depth = limit_depth if max_depth is None else max(1, int(max_depth))
  sem = asyncio.Semaphore(concurrency)
  lock = asyncio.Lock()
  seen: set[str] = set()
  out: list[dict] = []
  truncated = False
  stopped = False
  dirs = 0
  last_report = 0.0
  pending: set[asyncio.Task] = set()

  async def report() -> None:
    nonlocal last_report, stopped
    now = time.monotonic()
    if now - last_report < _PROGRESS_SECONDS:
      return
    last_report = now
    # 队列深度＝已派生但还没跑完的目录数，遍历阶段据此算百分比
    try:
      await _emit_progress(on_progress, dirs, len(out), len(pending))
    except BaseException:
      # 进度回调抛出的控制流（如用户终止）只会杀掉当前 task；置 stopped 让其余
      # 分支不再派生新任务，整棵遍历尽快收敛（异常本身照常向上冒泡）
      stopped = True
      raise

  async def visit(dir_id: str, segments: list[str], depth: int,
                  chain: list[dict]) -> None:
    nonlocal dirs, truncated
    if stopped:
      return
    async with sem:
      async with lock:
        if dir_id in seen:
          return
        seen.add(dir_id)
        if depth > max_depth:
          truncated = True
          return
        if dirs >= max_dirs:
          truncated = True
          return
        dirs += 1
      try:
        rows = await list_dir(dir_id)
      except Exception as exc:  # noqa: BLE001  单个目录失败不拖垮整部门
        logger.warning('[DocUpdate] 共享目录列举失败 parent=%s: %s', dir_id, exc)
        return
    for row in rows:
      if stopped:
        return
      if row['is_dir']:
        if _RECYCLE.match(_norm(row['name'])) or _should_prune(row['name'], numbers):
          continue
        child_chain = chain + [{'name': row['name'], 'fid': row['fid']}]
        task = asyncio.create_task(
          visit(row['fid'], segments + [row['name']], depth + 1, child_chain))
        pending.add(task)
        task.add_done_callback(pending.discard)
        continue
      out.append({
        'fid': row['fid'],
        'topic': row['name'],
        'parent_id': dir_id,
        'path': ' / '.join(segments),
        'segments': list(segments),
        'chain': list(chain),
        'create_time': row['create_time'],
        'size': row['size'],
      })
    await report()

  for root in roots:
    root_id = _norm((root or {}).get('fid'))
    if not root_id:
      continue
    segments = [_norm(s) for s in ((root or {}).get('segments') or []) if _norm(s)]
    chain = [{'name': segments[-1], 'fid': root_id}] if segments else []
    task = asyncio.create_task(visit(root_id, segments, len(segments) + 1, chain))
    pending.add(task)
    task.add_done_callback(pending.discard)
  while pending:
    await asyncio.gather(*pending, return_exceptions=True)

  if truncated:
    logger.warning(
      '[DocUpdate] 共享目录遍历触顶被截断 roots=%s：已访问 %d 个目录（上限 %d）、'
      '深度上限 %d —— 更深/更多的目录未遍历，结果可能漏文件',
      [r.get('fid') for r in roots], dirs, max_dirs, max_depth,
    )
  if stats is not None:
    stats.update(dirs=dirs, files=len(out), truncated=truncated)
  await _emit_progress(on_progress, dirs, len(out), 0)
  return sorted(out, key=lambda f: (f['path'], f['topic'], f['fid']))


async def download_sha256(fid: str) -> dict:
  """共享文件的内容哈希（不落盘）。失败返回 `{sha256:'', error:...}`。

  下载入口是 **POST** `{MGMT_BASE_URL}/file/share/v1/download/{fid}`（直出文件流）。
  `GET /file/share/v1/minio/url/{fid}` 对服务账号恒 403（2026-11-12 实测两个账号都
  拿不到预签名地址），**不要再回退到它**。
  """
  out = {'sha256': '', 'download_size': 0, 'url': '', 'error': '', 'warn': ''}
  url = f'{settings.MGMT_BASE_URL.rstrip("/")}/file/share/v1/download/{fid}'
  try:
    sha, size, _head = await _stream_with_relogin(url)
    out.update(sha256=sha, download_size=size, url=url)
  except Exception as exc:  # noqa: BLE001
    text = f'{type(exc).__name__}: {str(exc)[:200]}'
    if '403' in text or 'Forbidden' in text:
      text += '（服务账号无共享文件下载权限，需管理员开通）'
    out['error'] = text
  return out


async def _stream_with_relogin(url: str):
  """token 过期（401）时清缓存重登一次再下，避免缓存里的旧 token 一直 401。"""
  from config.redis_conn import redis_manager
  from service.doc_update.fetch import stream_sha256
  from service.query.mgmt import http

  for attempt in range(2):
    try:
      return await stream_sha256(url, method='POST')
    except httpx.HTTPStatusError as exc:
      if exc.response.status_code != 401 or attempt:
        raise
      redis_manager.client.delete(redis_manager.key(http._TOKEN_KEY))
      redis_manager.client.delete(redis_manager.key(http._DEPTID_KEY))
  raise RuntimeError('unreachable')  # 循环内必有 return 或 raise


def known_numbers() -> tuple[str, ...]:
  """剪枝用的全部目标编号：清单编号 ∪ 实际目录名里的前导编号。

  实际名称登记（`DocItem.folders`）指向的目录可能挂在别的编号下
  （如同一份月报也在 `1.2.3` / `8.1.2.3`），只收清单编号会被剪枝挡在门外。
  """
  from service.doc_update.catalog import MONTHLY_ITEMS

  out: list[str] = []
  for item in MONTHLY_ITEMS:
    out.extend(item.numbers)
    for folder in item.folders:
      m = _LEADING_NUMBER.match(_norm(folder))
      if m:
        out.append(m.group(1))
  return tuple(dict.fromkeys(out))


def timeout() -> int:
  return settings.DOC_UPDATE_TIMEOUT

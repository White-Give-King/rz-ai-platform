"""取数 + 算哈希：整树 → 匹配 25 条月度项 → 逐文件流式算 sha256（**不落盘**）。

取数走综合管理平台的既有 HTTP 封装（`service/query/mgmt/http.request`，自带 401 重登）：
- 整树 `GET /file/v1/getAll/{tree_id}`（节点只有 id/topic/type/children，没有 size）
- 目录列表 `GET /file/v1/{pid}/1/200`（才有 size/version/updateTime）
- 下载地址 `GET /file/v1/minio/url/{fid}` → `data.document.url`（S3 预签名，**嵌套两层**）
- 字节：直接 GET 预签名地址，`httpx.stream` 边下边喂 hashlib，算完即弃

**必须串行**：mgmt 同账号 × 同接口同时只允许 1 个在途。单文件失败只记 `error`，
不中断整轮（否则一个坏文件让整期永远拿不到结果）。
"""

import hashlib
import inspect
import logging
import re
import time
from datetime import datetime, timezone

import httpx

from config.settings import settings
from service.doc_update import (
  depts,
  dir_index,
  explore,
  match_llm,
  paths,
  period,
  period_llm,
  screenshot,
  share,
)
from service.doc_update.catalog import (
  JUDGE_NEW,
  JUDGE_UPDATE,
  MONTHLY_ITEMS,
  DocItem,
  excluded_segments,
)
from service.doc_update.verdict import MISSING_TEXT, review_names

logger = logging.getLogger(__name__)

_CHUNK = 64 * 1024
_HEAD_BYTES = 1024
_URL_KEYS = ('url', 'presignedUrl', 'downloadUrl', 'path', 'minioUrl', 'data')
_ZIP_MAGIC = b'PK\x03\x04'
_MAGIC = {
  'xlsx': _ZIP_MAGIC, 'docx': _ZIP_MAGIC, 'pptx': _ZIP_MAGIC, 'zip': _ZIP_MAGIC,
  'pdf': b'%PDF-',
}


def _norm(text: str) -> str:
  """去首尾空白 + 全角空格（文档名里常见 `1.2.2 负载…`）。"""
  return (text or '').replace('\u3000', ' ').strip()


async def load_tree(tree_id: str = '') -> dict:
  from service.query.mgmt import http

  tree_id = tree_id or settings.DOC_UPDATE_TREE_ID
  data = await http.request('GET', f'/file/v1/getAll/{tree_id}')
  if not isinstance(data, dict):
    raise RuntimeError(f'整树接口返回异常：{type(data).__name__}')
  return data


def walk_files(root: dict) -> list[dict]:
  """递归收集文件节点（`type == 0` 且没有 children）。分类节点是 `type == 1`。"""
  out: list[dict] = []

  def walk(node: dict, parent_id: str, trail: list[str]) -> None:
    node_id = str(node.get('id') or '')
    topic = _norm(str(node.get('topic') or ''))
    children = node.get('children') or []
    if str(node.get('type')) == '0' and not children:
      out.append({
        'fid': node_id,
        'topic': topic,
        'parent_id': parent_id,
        'path': ' / '.join(trail),
      })
      return
    for child in children:
      walk(child, node_id, trail + [topic] if topic else trail)

  walk(root, '', [])
  return out


_YEAR_PREFIX = re.compile(r'^(?:19|20|21)\d{2}\s*[-_.年]\s*')


def _starts_with_number(topic: str, number: str) -> bool:
  """编号前缀匹配：后面不能再接数字或点（防 `1.6.6.1` 命中 `1.6.6.10`）。

  名字以「四位年份 + 分隔符」开头时先剥掉再比（`2026-2.3.4.4门禁巡检报警记录表`）：
  年份版本化的目录已退回父目录，那里文件名都带年份前缀。
  """
  topic = _YEAR_PREFIX.sub('', topic, count=1)
  if not topic.startswith(number):
    return False
  rest = topic[len(number):]
  return not (rest[:1].isdigit() or rest[:1] == '.')


def _numbered(name: str, numbers) -> bool:
  return any(_starts_with_number(name, n) for n in numbers)


def _compact(text) -> str:
  """去所有空白（含全角）+ 转小写：真实目录名里编号后常带空格。"""
  return ''.join(str(text or '').replace('\u3000', ' ').split()).lower()


def _alias_hit(name: str, folders) -> bool:
  """实际名称匹配：目录/文件名里含登记过的真实写法（清单名只是模糊叫法）。"""
  text = _compact(name)
  return bool(text) and any(
    alias and alias in text for alias in (_compact(a) for a in folders or ())
  )


def match_item(item: DocItem, files: list[dict],
               extra_names=()) -> list[dict]:
  """挑出该清单项对应的文件（可能多个）。

  **实际名称优先**：真实共享树里的目录名常和 PDF 清单名对不上（清单只给「大概叫法」），
  故路径任一段或文件名含 `item.folders` 里登记过的写法即命中，不再过关键词闸。
  兜底仍按编号前缀（文件名或目录名），关键词（`4.1.5` 四条同名表）只对编号命中消歧。

  `extra_names` 是调用方补充的别名（部门口径的按部门真实叫法，见
  `paths._DEPT_ITEM_NAMES`）：与 `item.folders` 同权、命中即算，**不过关键词闸**。

  返回 `files` 的**副本**（同一文件可能命中多项，不能互相污染），
  附带 `match_dir` = 最深命中目录名（仅文件名命中记 `''`）。
  """
  hits = []
  folders = tuple(item.folders or ()) + tuple(extra_names or ())
  for f in files:
    topic = f['topic']
    segments = f.get('segments') or []
    if excluded_segments(item, segments):
      continue
    dir_alias = [s for s in segments if _alias_hit(s, folders)]
    if dir_alias or _alias_hit(topic, folders):
      hits.append(dict(f, match_dir=dir_alias[-1] if dir_alias else ''))
      continue
    dir_number = [s for s in segments if _numbered(s, item.numbers)]
    if not _numbered(topic, item.numbers) and not dir_number:
      continue
    if item.keywords:
      names = [topic, *dir_number]
      if not any(k in name for name in names for k in item.keywords):
        continue
    hits.append(dict(f, match_dir=dir_number[-1] if dir_number else ''))
  return hits


def _under(file_rec: dict, fid: str) -> bool:
  """文件是否位于目录 `fid` 之下（`chain` 是从遍历根到父目录的祖先链）。"""
  fid = str(fid or '')
  if not fid:
    return False
  return any(str(c.get('fid') or '') == fid for c in file_rec.get('chain') or [])


def _covers(files: list[dict], roots: list[dict]) -> bool:
  """这些文件里是否至少有一个落在 `roots` 的任一子树下。"""
  fids = {str(r.get('fid') or '') for r in roots if r.get('fid')}
  if not fids:
    return False
  return any(
    str(c.get('fid') or '') in fids
    for f in files for c in f.get('chain') or []
  )


def _named_roots(cfg_roots: dict) -> set[int]:
  """钉住的目录名可单独当「名字」用的配置根 seq。

  只被本项钉住、且不是其它钉点祖先的目录才算：这类目录的业务归属是确定的
  （`paths._DEPT_PATHS` 逐条照业务文档钉的），目录名就是该项的真实名称 ——
  实测廊坊 A1 的目录名既不带清单编号（没有 `2.2.8…`）也没登记 `folders` 别名，
  只靠编号/别名会整项 0 命中。共享目录（4.1.5 四条、A2 的「记录表」）
  与祖先章节目录（seq19 钉 `2.3.2数据中心监控室管理`）不算，那里仍按编号/别名消歧。
  """
  seqs = [seq for seq, root in cfg_roots.items() if str((root or {}).get('fid') or '')]
  fids = {seq: str(cfg_roots[seq]['fid']) for seq in seqs}
  segs = {seq: paths.normalize_segments(cfg_roots[seq].get('segments') or ())
          for seq in seqs}
  out: set[int] = set()
  for seq in seqs:
    if any(s != seq and fids[s] == fids[seq] for s in seqs):
      continue
    if any(s != seq and len(segs[s]) > len(segs[seq])
           and segs[s][:len(segs[seq])] == segs[seq] for s in seqs):
      continue
    out.add(seq)
  return out


def _match_files(item: DocItem, files: list[dict], config_root: dict | None, *,
                 configured: bool = False, named: bool = False,
                 extra_names=()) -> list[dict]:
  """取该项的候选文件：配了显式路径就先收窄到该子树，子树内仍按别名/编号消歧。

  稳定父目录可被多项共用（如 A18 的 `4.1.5隐患排查治理` 同时挂 29~32），
  所以子树里的文件**不能全算**，必须再过一次 `match_item`。

  `named=True`（该项独占该钉点，见 `_named_roots`）时，钉住的目录名就是该项的名字：
  别名/编号都不命中（目录名不带编号的部门，如廊坊 A1）也按名字认下整个子树。
  只对查新增项生效 —— 查更新项全收会把历史版本一并下载，白烧文件预算。

  `configured=True`（该项配了显式路径）但 `config_root` 为空 ＝ 解析失败，
  返回空、**不回落全树启发式** —— 那是错误结论的来源。

  `extra_names` 透传给 `match_item`（部门口径的按部门文件名别名，不传 ＝ 行为不变）。

  别改成「从配置 fid 带空 segments 直接遍历」：`share.walk_roots` 会把配置根的
  `segments` 播到每个后代文件上（`segments = 根段 + 子目录名`、`chain` 首项 = 配置根），
  配置目录名里的编号（如 `2.3.4.6值班巡检记录表…`）才看得见；实测空 segments 时 seq 22 会 0 命中。
  """
  if configured:
    fid = str((config_root or {}).get('fid') or '')
    scoped = [f for f in files if _under(f, fid)]
    if not scoped:
      return []
    hits = match_item(item, scoped, extra_names)
    if hits or not named or item.judge == JUDGE_UPDATE:
      return hits
    where = (config_root.get('segments') or [''])[-1]
    return [dict(f, match_dir=where) for f in scoped]
  return match_item(item, files, extra_names) if files else []


def _hit_year(f: dict) -> int | None:
  """命中文件的年份：目录链优先，其次文件名里的四位年份；都没有＝None（视作当期）。"""
  year = period._segments_year(f.get('segments') or [])
  if year is not None:
    return year
  parsed = period.name_period(_norm(f.get('topic') or ''))
  return int(parsed[:4]) if parsed else None


def _hit_period(f: dict) -> str:
  """命中文件的月份级期号 `'YYYY-MM'`：目录链优先，其次文件名；解析不到＝空串。"""
  segments = f.get('segments') or []
  found = period.parse_period(segments)
  if found:
    return found
  return period.name_period(_norm(f.get('topic') or ''), period._segments_year(segments))


def _narrow_update_targets(hits: list[dict], expected_year: int) -> list[dict]:
  """查更新项只判本周期的目标文件：先按年份剔除往年，再按月取最新一份。

  `_match_files` 对查更新项把钉点子树里所有编号/别名命中都收进来，于是：
  ① 子树并存 `2025年/` 与 `2026年/` 时往年文件也被下载、被判（"去25年文件里找了"）；
  ② 按月命名的多份文件全被列出（"按月份名找了好几个月"）。这里把候选收窄到
  「本年度 + 最新月份」那一份 —— 判定口径不变（仍是 sha256 vs 上月）。

  - 年份：丢弃**显式其它年份**的命中；但只在「本年度或无年份」的命中非空时才收窄，
    解析不出年份时宁可保住文件，也不把整项清空误判「未找到」。
  - 月份：收窄后只要有一份带月份期号，就只保留月份最大的那一批（丢更早月份与无期号的
    模板/旧件）；全无月份（如 A1 继保单文件、seq 6 供电目录多文档文件夹）原样保留整组，
    逐个哈希。
  """
  year_ok = [f for f in hits if _hit_year(f) in (None, expected_year)]
  narrowed = year_ok or hits
  dated = {f['fid']: _hit_period(f) for f in narrowed}
  periods = [p for p in dated.values() if p]
  if not periods:
    return narrowed
  latest = max(periods)
  return [f for f in narrowed if dated.get(f['fid']) == latest]


_WORD_SPLIT = re.compile(r'[^\w\u4e00-\u9fff]+')
_PATH_SEP = ' / '


def _tokens(item: DocItem) -> list[str]:
  """预筛词元：名称 / 编号 / 登记目录名里 ≥2 字的片段（只用来缩范围，不求语义）。"""
  out: list[str] = []
  for text in [item.name, item.number_label, *list(item.folders or ())]:
    for part in _WORD_SPLIT.split(_norm(text)):
      if len(part) >= 2 and part not in out:
        out.append(part)
  return out


def _scope_files(item: DocItem, files: list[dict], config_root: dict | None,
                 *, configured: bool) -> list[dict]:
  """模型复核的候选范围（确定性匹配一行不动，这里只给它划「已解析出」的圈）。

  配了显式路径且钉点解析成功 → 钉点子树（与 `_match_files` 收窄用的是同一批文件）；
  钉点解析失败 / 未配置 → 按词元子串预筛（`path` 或 `topic` 命中任一词元）。
  筛不出就当没有范围，**不把整棵树交给模型**。
  """
  if configured:
    fid = str((config_root or {}).get('fid') or '')
    if fid:
      return [f for f in files if _under(f, fid)]
  words = _tokens(item)
  if not words:
    return []
  return [f for f in files
          if any(w in str(f.get('topic') or '') or w in str(f.get('path') or '')
                 for w in words)]


def _parent_path(rows: list[dict]) -> str:
  """范围内文件的公共父目录（`path` 是目录链，文件名在 `topic`）。"""
  chains = [str(r.get('path') or '') for r in rows if r.get('path')]
  if not chains:
    return ''
  common = chains[0].split(_PATH_SEP)
  for chain in chains[1:]:
    segs = chain.split(_PATH_SEP)
    n = 0
    while n < len(common) and n < len(segs) and common[n] == segs[n]:
      n += 1
    common = common[:n]
  return _PATH_SEP.join(common)


def _search_path(records: list[dict], config_root: dict | None,
                 scoped: list[dict], matched: list[dict]) -> str:
  """实际查找路径：优先命中文件的真实目录（含年/月），其次范围内公共父目录，
  最后才回退钉点层级。"""
  for rows in (records, matched, scoped):
    path = _parent_path(list(rows))
    if path:
      return path
  segs = list((config_root or {}).get('segments') or ())
  return _PATH_SEP.join(str(s) for s in segs)


def _dir_files(scoped: list[dict]) -> list[str]:
  """范围内去重后的真实文件名（只在「未找到」时写，给页面看目录里到底有什么）。"""
  names: list[str] = []
  for rec in scoped:
    name = _norm(rec.get('topic'))
    if name and name not in names:
      names.append(name)
  return names


def _deep_common_dir(rows: list[dict]) -> dict | None:
  """这些文件祖先链的最深公共目录（`{'fid','segments'}`）；没有公共目录返回 None。"""
  chains = [list(f.get('chain') or ()) for f in rows if f.get('chain')]
  if not chains:
    return None
  common = chains[0]
  for chain in chains[1:]:
    n = 0
    while (n < len(common) and n < len(chain)
           and str(common[n].get('fid') or '') == str(chain[n].get('fid') or '')):
      n += 1
    common = common[:n]
    if not common:
      return None
  fid = str(common[-1].get('fid') or '')
  if not fid:
    return None
  return {'fid': fid, 'segments': [str(c.get('name') or '') for c in common]}


def _explore_start(matched: list[dict], scoped: list[dict],
                   cfg_root: dict | None) -> dict | None:
  """子 agent 的探索起点：配置项用钉点，启发式用范围内文件的最深公共目录。"""
  fid = str((cfg_root or {}).get('fid') or '')
  if fid:
    return {'fid': fid, 'segments': list((cfg_root or {}).get('segments') or ())}
  for rows in (matched, scoped):
    start = _deep_common_dir(rows)
    if start:
      return start
  return None


def _agent_suspect(item: DocItem, matched: list[dict]) -> bool:
  """查新增项里「没命中、或命中的文件都没期号」＝疑似停在模板上，交子 agent 重定位。

  查更新项（sha256）不参与；命中的文件只要有一份能解析出月份级期号，就认为已定位到
  真实记录，不再花模型成本（模板/年度汇总解析不出期号，正是要重定位的对象）。
  """
  if item.judge != JUDGE_NEW:
    return False
  if not matched:
    return True
  return not any(period.period_of_file(f, from_name=True) for f in matched)


async def _run_explore(item: DocItem, *, files: list[dict], cfg_root,
                       matched: list[dict], scoped: list[dict]) -> dict:
  """调受控子 agent 重定位；只选到目录、快照里没文件时按 fid 有界补遍历一次。"""
  start = _explore_start(matched, scoped, cfg_root)
  if not start:
    return {}
  result = await explore.locate(item, files=files, start=start)
  mapped = list(result.get('files') or ())
  if mapped:
    return {'files': mapped, 'reason': result.get('reason') or ''}
  dirs = list(result.get('dirs') or ())
  if not dirs:
    return {}
  fids = {str(d.get('fid') or '') for d in dirs if d.get('fid')}
  if not fids:
    return {}
  try:
    extra = await share.walk_roots(
      [{'fid': d['fid'], 'segments': list(d.get('segments') or ())} for d in dirs],
      (), max_dirs=max(1, int(settings.DOC_UPDATE_EXPLORE_MAX_DIRS or 1)))
  except Exception as exc:  # noqa: BLE001  补遍历失败按未定位处理，保持原结论
    logger.warning('[DocUpdate] 子 agent 定位后补遍历失败：%s', str(exc)[:200])
    return {}
  picked = [f for f in extra
            if any(str(c.get('fid') or '') in fids for c in f.get('chain') or ())]
  if not picked:
    return {}
  return {'files': picked, 'reason': result.get('reason') or ''}


async def _resolve_paths(entry: dict, root: str) -> tuple[dict, dict, dict, dict, dict]:
  """解析该部门的显式路径配置。

  返回 `(cfg, roots, errors, warns, listings)`：`cfg` 是 `{seq: 配置段}`，
  `roots` 是解析成功的 `{seq: {seq, fid, segments}}`（形状与目录索引的根一致），
  `listings` 是本次运行内按父目录缓存的列表（多文档共享前缀只列一次）。
  """
  cfg = paths.configured(entry['company_id'], entry['company_name'], entry['dept_name'])
  roots: dict[int, dict] = {}
  errors: dict[int, str] = {}
  warns: dict[int, str] = {}
  listings: dict = {}
  if not cfg or not root:
    return cfg, roots, errors, warns, listings
  cache = await paths.load_cache(entry['company_id'], entry['dept_id'])
  fresh: dict[int, dict] = {}
  for seq in sorted(cfg):
    res = await paths.resolve(root, seq, cfg[seq], cache=cache, listings=listings)
    if res['fid']:
      roots[seq] = {'seq': seq, 'fid': res['fid'], 'segments': list(res['segments'])}
      if res.get('warn'):
        warns[seq] = res['warn']
      if not res.get('from_cache'):
        fresh[seq] = {'segments': res['segments'], 'fid': res['fid']}
    else:
      errors[seq] = res['error']
  if fresh:
    await paths.save_cache(entry['company_id'], entry['dept_id'], fresh)
  return cfg, roots, errors, warns, listings


async def _refresh_config_roots(*, cfg: dict, roots: dict, errors: dict,
                                entry: dict, root: str, numbers,
                                listings: dict, files: list[dict],
                                on_progress=None) -> tuple[list[dict], dict]:
  """配置子树里一个文件都没有 → 重解析一次（目录改名/移动）并补走新子树。

  只用缓存 fid 时目录可能已被移动；重解析用 `listings` 里的**新列表**核对，
  变了才补走。返回 `(files, refreshed)`，`refreshed` 要写回路径缓存。
  """
  missing = [seq for seq, r in roots.items() if not _covers(files, [r])]
  if not missing:
    return files, {}
  refreshed: dict[int, dict] = {}
  for seq in missing:
    res = await paths.resolve(root, seq, cfg[seq], listings=listings, force=True)
    if not res['fid']:
      errors[seq] = res['error']
      continue
    old = roots[seq]
    if res['fid'] == old['fid']:
      continue
    new_root = {'seq': seq, 'fid': res['fid'], 'segments': list(res['segments'])}
    try:
      extra = await share.walk_roots([new_root], numbers, on_progress=on_progress)
    except Exception as exc:  # noqa: BLE001  补走失败只影响该项
      errors[seq] = f'共享目录遍历失败：{type(exc).__name__}: {str(exc)[:200]}'
      continue
    have = {f['fid'] for f in files}
    files.extend(f for f in extra if f['fid'] not in have)
    roots[seq] = new_root
    errors.pop(seq, None)
    refreshed[seq] = {'segments': res['segments'], 'fid': res['fid']}
    logger.warning('[DocUpdate] 配置路径已移动，重解析 %s/%s seq=%s → %s',
                   entry.get('company_name'), entry.get('dept_name'), seq, res['fid'])
  if refreshed:
    await paths.save_cache(entry['company_id'], entry['dept_id'], refreshed)
  return files, refreshed


async def fetch_dir_meta(parent_id: str) -> dict[str, dict]:
  """目录下的文件清单（size/version/updateTime/uploadTime），按 fid 索引。"""
  from service.query.mgmt import http

  if not parent_id:
    return {}
  data = await http.request('GET', f'/file/v1/{parent_id}/1/200')
  values = (data or {}).get('value') if isinstance(data, dict) else None
  return {str(v.get('fid')): v for v in (values or []) if v.get('fid')}


def extract_url(data, depth: int = 0) -> str:
  """从下载接口的 data 里挑出地址（实测藏在 `data.document.url`，故递归找）。"""
  if isinstance(data, str):
    return data if data.startswith('http') else ''
  if isinstance(data, dict) and depth < 3:
    for key in _URL_KEYS:
      value = data.get(key)
      if isinstance(value, str) and value.startswith('http'):
        return value
    for value in data.values():
      if isinstance(value, (dict, list)):
        found = extract_url(value, depth + 1)
        if found:
          return found
  if isinstance(data, list) and depth < 3:
    for value in data:
      found = extract_url(value, depth + 1)
      if found:
        return found
  return ''


def _expected_magic(file_type: str, file_name: str) -> bytes:
  kind = (file_type or '').lower().lstrip('.')
  if not kind:
    kind = file_name.rsplit('.', 1)[-1].lower() if '.' in file_name else ''
  return _MAGIC.get(kind, b'')


async def stream_sha256(url: str, method: str = 'GET') -> tuple[str, int, bytes]:
  """流式下载算 sha256，返回 (hexdigest, 字节数, 头部若干字节)。**不写文件**。

  共享文件下载入口是 POST（`/file/share/v1/download/{fid}` 直出文件流），故 method 可传。
  """
  headers = {}
  if settings.MGMT_BASE_URL and url.startswith(settings.MGMT_BASE_URL.rstrip('/')):
    # 域内地址带鉴权头；S3 预签名地址不带（避免把 token 发给对象存储）
    from service.query.mgmt import http

    token, dept = await http._get_token_and_dept()
    headers = {'token': token, 'Deptid': dept or ''}

  digest = hashlib.sha256()
  total = 0
  head = b''
  async with httpx.AsyncClient(
    timeout=settings.DOC_UPDATE_TIMEOUT, follow_redirects=True,
  ) as client:
    async with client.stream(method, url, headers=headers) as resp:
      resp.raise_for_status()
      async for chunk in resp.aiter_bytes(_CHUNK):
        if not chunk:
          continue
        if len(head) < _HEAD_BYTES:
          head += chunk[:_HEAD_BYTES - len(head)]
        digest.update(chunk)
        total += len(chunk)
  return digest.hexdigest(), total, head


async def download_sha256(fid: str, file_type: str = '', file_name: str = '') -> dict:
  """取一个文件的哈希。返回 `{sha256, download_size, error, warn}`，失败不抛异常。"""
  from service.query.mgmt import http

  out = {'sha256': '', 'download_size': 0, 'error': '', 'warn': ''}
  try:
    data = await http.request('GET', f'/file/v1/minio/url/{fid}')
    url = extract_url(data)
    if not url:
      out['error'] = f'下载地址为空（data 前 120 字：{str(data)[:120]}）'
      return out
    sha, size, head = await stream_sha256(url)
    magic = _expected_magic(file_type, file_name)
    if magic and not head.startswith(magic):
      out['error'] = f'内容类型不符（期望 {file_type} 文件头，实际前 8 字节 {head[:8]!r}）'
      return out
    out.update(sha256=sha, download_size=size)
  except Exception as exc:  # noqa: BLE001  单文件失败不能拖垮整期
    out['error'] = f'{type(exc).__name__}: {str(exc)[:200]}'
  return out


def _prev_index(previous: dict | None) -> dict[str, tuple[str, datetime | str]]:
  index: dict[str, tuple[str, datetime | str]] = {}
  for item in (previous or {}).get('items') or []:
    for f in item.get('files') or []:
      if f.get('fid'):
        index[str(f['fid'])] = (f.get('sha256') or '', f.get('last_changed_at') or '')
  return index


def _prev_flat_index(previous: dict | None) -> dict[str, tuple[str, str]]:
  """上期快照里所有文件的 (sha256, last_changed_at)，按 fid 索引（部门分支用）。"""
  index: dict[str, tuple[str, str]] = {}
  for dept in (previous or {}).get('depts') or []:
    for item in dept.get('items') or []:
      for f in item.get('files') or []:
        fid = str(f.get('fid') or '')
        if fid:
          index[fid] = (f.get('sha256') or '', f.get('last_changed_at') or '')
  return index


def _size_warn(meta: dict, download_size: int) -> str:
  """档案 size 与实际字节差 >10% 记「待复核」——只提示，不当失败。"""
  try:
    size = float(meta.get('size') or 0)
  except (TypeError, ValueError):
    return ''
  if size <= 0 or download_size <= 0:
    return ''
  diff = abs(size - download_size) / size
  if diff <= 0.10:
    return ''
  return f'实际字节与档案大小不符（档案 {size:.0f} / 实际 {download_size}，差 {diff:.0%}）'


async def take_snapshot(
  *,
  tree_id: str = '',
  previous: dict | None = None,
  progress=None,
  only: list[DocItem] | None = None,
) -> dict:
  """跑一期快照：清单项 → 匹配文件 → 逐文件算 hash。

  `only` 为空时取全部 25 条月度项（整期）；传子集则只算这几项（单项刷新用）。
  `progress(done, total, name)` 用于推送进度（对话工具防止 360s 静默超时）。
  `last_changed_at` 从上期结转：hash 没变就沿用上期时间，变了（或新增）记本次时间。
  """
  started = time.monotonic()
  taken_at = datetime.now(timezone.utc)
  scope = list(only or MONTHLY_ITEMS)
  tree = await load_tree(tree_id)
  files = walk_files(tree)
  prev = _prev_index(previous)
  meta_cache: dict[str, dict[str, dict]] = {}
  items: list[dict] = []
  budget = max(1, settings.DOC_UPDATE_MAX_FILES)

  for idx, item in enumerate(scope, 1):
    matched = match_item(item, files)
    records: list[dict] = []
    for f in matched:
      if budget <= 0:
        records.append({
          'fid': f['fid'], 'file_name': f['topic'], 'path': f['path'], 'size': None,
          'download_size': 0, 'version': '', 'update_time': '', 'upload_time': '',
          'sha256': '', 'warn': '',
          'error': f'超出 DOC_UPDATE_MAX_FILES={settings.DOC_UPDATE_MAX_FILES} 安全阀，未取',
          'last_changed_at': '',
        })
        continue
      budget -= 1
      parent = f['parent_id']
      if parent not in meta_cache:
        try:
          meta_cache[parent] = await fetch_dir_meta(parent)
        except Exception as exc:  # noqa: BLE001
          logger.warning('[DocUpdate] 目录列表失败 parent=%s: %s', parent, exc)
          meta_cache[parent] = {}
      meta = meta_cache[parent].get(f['fid']) or {}
      file_name = _norm(str(meta.get('name') or f['topic']))
      res = await download_sha256(
        f['fid'], file_type=str(meta.get('fileType') or ''), file_name=file_name,
      )
      prev_sha, prev_changed = prev.get(f['fid'], ('', ''))
      changed = bool(res['sha256']) and res['sha256'] != prev_sha
      records.append({
        'fid': f['fid'],
        'file_name': file_name,
        'path': f['path'],
        'size': meta.get('size'),
        'download_size': res['download_size'],
        'version': meta.get('version') or '',
        'update_time': str(meta.get('updateTime') or ''),
        'upload_time': str(meta.get('uploadTime') or ''),
        'sha256': res['sha256'],
        'error': res['error'],
        'warn': res['warn'] or _size_warn(meta, res['download_size']),
        'last_changed_at': taken_at if changed or not prev_changed else prev_changed,
      })

    items.append({
      'seq': item.seq,
      'number': item.number_label,
      'name': item.name,
      'cadence': item.cadence,
      'note': item.note,
      'files': records,
      'error': '' if records else MISSING_TEXT,
    })
    if progress:
      # 进度回调同步/异步都支持（对话工具要 await 才能推 SSE 事件）
      out = progress(idx, len(scope), item.name)
      if inspect.isawaitable(out):
        await out

  elapsed = round(time.monotonic() - started, 2)
  logger.info('[DocUpdate] 快照完成 items=%d files=%d elapsed=%ss',
              len(items), sum(len(i['files']) for i in items), elapsed)
  return {
    'taken_at': taken_at,
    'period_key': taken_at.astimezone(_tz()).strftime('%Y-%m'),
    'period_label': taken_at.astimezone(_tz()).strftime('%Y年%m月'),
    'tree_id': tree_id or settings.DOC_UPDATE_TREE_ID,
    'items': items,
    'file_count': sum(len(i['files']) for i in items),
    'elapsed': elapsed,
  }


def _period_record(f: dict, from_name: bool = False) -> dict:
  """查新增项的文件记录：只带位置与期号，不下载不哈希。"""
  return {
    'fid': f['fid'],
    'file_name': f['topic'],
    'path': f['path'],
    'match_dir': f.get('match_dir') or '',
    'period': period.period_of_file(f, from_name),
    'size': f.get('size'),
    'download_size': 0,
    'sha256': '',
    'error': '',
    'warn': '',
    'last_changed_at': '',
  }


def _pending_files(files) -> list[str]:
  """带月份线索但没有年份锚的文件名（去重保序），仅在按部门改判为查新增时使用。"""
  out: list[str] = []
  seen: set[str] = set()
  for f in files or ():
    name = _norm(f.get('topic') or '')
    if not name or name in seen:
      continue
    if period.month_clue_no_period(name, f.get('path') or ''):
      seen.add(name)
      out.append(name)
  return out


def _flat_record(f: dict, res: dict, prev: dict[str, tuple[str, str]],
                 taken_at: datetime) -> dict:
  """查更新项的文件记录：哈希与上期比（新增或变化才更新 last_changed_at）。"""
  prev_sha, prev_changed = prev.get(f['fid'], ('', ''))
  changed = bool(res['sha256']) and res['sha256'] != prev_sha
  return {
    'fid': f['fid'],
    'file_name': f['topic'],
    'path': f['path'],
    'match_dir': f.get('match_dir') or '',
    'period': '',
    'size': f.get('size'),
    'download_size': res['download_size'],
    'sha256': res['sha256'],
    'error': res['error'],
    'warn': res.get('warn') or _size_warn({'size': f.get('size')}, res['download_size']),
    'last_changed_at': taken_at if changed or not prev_changed else prev_changed,
  }


def _failed_record(f: dict, error: str) -> dict:
  """取不到内容的文件记录：形状与 `_flat_record` 一致，判定为「下载失败」。"""
  return {
    'fid': f.get('fid') or '',
    'file_name': f.get('topic') or '',
    'path': f.get('path') or '',
    'match_dir': f.get('match_dir') or '',
    'period': '',
    'size': f.get('size'),
    'download_size': 0,
    'sha256': '',
    'error': error,
    'warn': '',
    'last_changed_at': '',
  }


async def _load_index(company_id: str, dept_id: str) -> dict | None:
  try:
    return await dir_index.load(company_id, dept_id)
  except Exception as exc:  # noqa: BLE001  读不到索引就按无索引全量，不影响本轮
    logger.warning('[DocUpdate] 目录索引读取失败 %s/%s: %s', company_id, dept_id, exc)
    return None


async def _with_siblings(roots: list[dict], root_id: str, scope) -> list[dict]:
  """补上部门根下**新出现的**别名/编号目录（每月新建的归档兄弟目录不在旧子树里）。"""
  try:
    children = await share.list_dir(root_id)
  except Exception as exc:  # noqa: BLE001  列根失败就用已有根继续
    logger.warning('[DocUpdate] 部门根列举失败 root=%s: %s', root_id, exc)
    return roots
  have = {str(r.get('fid') or '') for r in roots}
  extra = [r for r in dir_index.sibling_roots(scope, children)
           if str(r.get('fid') or '') not in have]
  return roots + extra


async def _save_index(company_id: str, dept_id: str, dept_name: str,
                      scope, files: list[dict]) -> None:
  """全量遍历后重建索引：替换本轮 scope 各项的根，其余项的根保留。"""
  try:
    pairs = [(item, match_item(item, files)) for item in scope]
    existing = await dir_index.load(company_id, dept_id)
    roots = dir_index.merge_roots(
      (existing or {}).get('roots') or [],
      dir_index.pick_roots(pairs),
      {item.seq for item in scope},
    )
    roots = dir_index.drop_excluded(roots, scope)
    await dir_index.save(company_id, dept_id, dept_name, roots, full_scan=True)
  except Exception as exc:  # noqa: BLE001  索引只是加速，失败不影响本轮结果
    logger.warning('[DocUpdate] 目录索引写入失败 %s/%s: %s', company_id, dept_id, exc)


def _relevant_records(records: list[dict], expected: str, judge: str,
                      layout: str) -> list[dict]:
  """查新增 + 按期提交：只保留与本期判定相关的文件。

  判定只看 `max(period) >= expected`，历史期文件对结论零作用（A1 能源月报实测
  命中 312 个、应到期仅 9 个）。一条本期/未来期都没有时保留最新一期（要能判「未更新」
  并显示最新期号）；全部解析不出期号则原样保留（要能判 `failed` 并给原因）。
  查更新项与无期号项**原样保留**（哈希/fid 比对需要全部文件）。
  扁平查新增项的裁剪只在展示层做（`verdict` 读快照时），快照仍存全量：判定证据与
  `changes.added_fids` 都依赖完整集合。
  """
  if judge == JUDGE_UPDATE or layout != period.LAYOUT_PERIOD:
    return records
  return period.relevant(records, expected)


async def _attach_screenshots(entry: dict, cfg_roots: dict) -> None:
  """未找到项按其钉点去重截一张图（同钉点的多项共用一份）；失败只写 `screenshot.error`。

  只截**有钉点**的项：没有显式路径就没有「进哪一层」可导航，硬猜路径比不截更糟。
  """
  targets: dict[str, list[dict]] = {}
  for item in entry.get('items') or []:
    if item.get('error') != MISSING_TEXT:
      continue
    root = cfg_roots.get(item.get('seq'))
    fid = str((root or {}).get('fid') or '')
    if fid:
      targets.setdefault(fid, []).append(item)
  for fid, items in targets.items():
    root = cfg_roots.get(items[0].get('seq')) or {}
    shot = await screenshot.capture(
      company_id=entry.get('company_id') or '',
      dept_name=entry.get('dept_name') or '',
      fid=fid,
      segments=[str(s) for s in root.get('segments') or ()],
    )
    for item in items:
      item['screenshot'] = shot


async def take_dept_snapshot(
  *,
  previous: dict | None = None,
  progress=None,
  only: list[DocItem] | None = None,
  dept_names: list[str] | None = None,
  companies: list[str] | None = None,
  target_period: str | None = None,
) -> dict:
  """跑一期「部门 × 清单项」快照。

  `target_period`（`YYYY-MM`）：补跑历史期（页面「补跑期间」/ CLI `--sync --period`）。
  不传＝当期（原有行为）。补跑期**不叠加**查新增项的过渡放宽。

  判定口径由清单项决定（`DocItem.judge`，一类文档一个口径，与目录形态无关）：
  - `update`（PDF 第三列写「线上更新」）：下载算 sha256，与上一期快照比内容是否变化；
  - `new`（其余 PDF/PPT/文档）：查新增 —— 路径有月份期号就只看本期期号是否已交，
    没有期号就比对 fid（出现上期没有的文件即已交），**不下载**。

  `period_only` 项（能源月报）**强制**按期判定：各部门路径不同，多数期号只写在
  文件名里，故期号允许从文件名解析，且**永不下载**。

  部门清单取固定配置 + 注册表缓存（`depts.resolve_departments`，缓存齐了不联网），
  传 `dept_names` / `companies` 时只跑指定部门/公司。单个部门/文件失败只记 `error`，
  不中断整轮。
  """
  started = time.monotonic()
  taken_at = datetime.now(timezone.utc)
  local_now = taken_at.astimezone(_tz())
  scope = list(only or MONTHLY_ITEMS)
  numbers = share.known_numbers()
  prev = _prev_flat_index(previous)
  budget = max(1, settings.DOC_UPDATE_MAX_FILES)
  # 应到期＝本次检查的期间（选 11 月就查 11 月报告）；期间的 key/label 都用它生成，
  # 两者永远同源，页面期间与判定月份不会再错位。补跑时直接用目标期
  expected = (target_period
              or period.expected_period(local_now,
                                        settings.DOC_UPDATE_PERIOD_GRACE_MONTHS))
  # 「查新增」项的接受下限可临时回退 N 月（默认 0）；**period_key/期间标签仍用 expected**，
  # 否则 11 月检查会被存成上月快照并覆盖真实的上月数据。查更新项不受影响（只用 sha256）。
  # 补跑不叠加这层放宽（那是当期过渡口径）
  accept_prev = 0 if target_period else max(
    0, settings.DOC_UPDATE_NEW_ACCEPT_PREV_MONTHS or 0)
  new_floor = period.shift_period(expected, -accept_prev) if accept_prev else expected

  all_depts = await depts.resolve_departments(dept_names, companies)

  # 遍历与逐项判定同权：每部门 = 遍历 walk_steps 步 + 逐项 len(scope) 步。
  # 否则遍历阶段（一个部门 2~10 分钟）done 一动不动，进度条看着像卡死。
  walk_steps = len(scope)
  total_steps = max(1, len(all_depts) * (walk_steps + len(scope)))
  done = 0
  out_depts: list[dict] = []

  for dept in all_depts:
    entry = {
      'company_id': dept.get('company_id') or '',
      'company_name': dept.get('company_name') or '',
      'dept_id': dept['dept_id'],
      'dept_name': dept['dept_name'],
      'root_fid': '',
      'error': '',
      'items': [],
    }
    dept_label = (f'{entry["company_name"]} · ' if entry['company_name'] else '') \
      + dept['dept_name']
    base = done
    walk_frac = 0.0
    files: list[dict] = []
    root = dept.get('root_fid') or ''
    if not root and dept.get('dept_id'):
      try:
        root = await depts.root_file_id(dept['dept_id'])
      except Exception as exc:  # noqa: BLE001  单个部门失败不拖垮整轮
        root = ''
        entry['error'] = f'部门详情获取失败：{type(exc).__name__}: {str(exc)[:200]}'
    elif dept.get('error'):
      entry['error'] = dept['error']
    if root:
      entry['root_fid'] = root
      walk_stats: dict = {}

      async def _walk_progress(dirs: int, files: int, queued: int = 0, *,
                               _base: int = base) -> None:
        nonlocal walk_frac
        if not progress:
          return
        # 百分比 = 已访问 / (已访问 + 队列)：分母是「当前已知的目录总数」，
        # 未知的子目录被发现时队列会变长，故取历史最高值、不让条倒退
        known = dirs + max(0, queued)
        frac = dirs / known if known else 0.0
        walk_frac = min(0.99, max(walk_frac, frac))
        out = progress(
          _base + round(walk_frac * walk_steps), total_steps,
          f'{dept_label} · 正在遍历目录（已访问 {dirs} 个目录 / {files} 个文件）',
          'walk',
        )
        if inspect.isawaitable(out):
          await out

      if progress:
        out = progress(base, total_steps, f'{dept_label} · 正在遍历目录', 'walk')
        if inspect.isawaitable(out):
          await out
      index_doc = await _load_index(entry['company_id'], entry['dept_id'])
      # 配置了显式路径的项只在该目录子树里取文件，未配置的才走别名/编号启发式
      cfg, cfg_roots, cfg_errors, cfg_warns, listings = await _resolve_paths(
        entry, root)
      index_scope = [i for i in scope if i.seq not in cfg]
      full_scan = dir_index.should_full_scan(index_doc)
      roots = [] if full_scan else list((index_doc or {}).get('roots') or [])
      # 配置项不再用索引根（解析失败也不回落启发式），旧根一并丢掉
      roots = [r for r in roots if r.get('seq') not in cfg]
      # 老索引里可能固化了已作废的路径，先丢掉，别白遍历那棵子树
      roots = dir_index.drop_excluded(roots, index_scope)
      if roots:
        roots = await _with_siblings(roots, root, index_scope)
      walk_list = roots + list(cfg_roots.values())
      try:
        if roots:
          files = await share.walk_roots(
            walk_list, numbers, on_progress=_walk_progress, stats=walk_stats)
        elif index_scope:
          # 有未配置项又没索引：全量遍历已覆盖配置子树，不必再单独走一遍
          files = await share.walk(
            root, numbers, on_progress=_walk_progress, stats=walk_stats)
        elif walk_list:
          files = await share.walk_roots(
            walk_list, numbers, on_progress=_walk_progress, stats=walk_stats)
        else:
          # 全部项都配置了路径且一个都没解析出来，全树遍历也判不出结果
          files = []
      except Exception as exc:  # noqa: BLE001
        entry['error'] = f'共享目录遍历失败：{type(exc).__name__}: {str(exc)[:200]}'
      # 索引子树里一个文件都没有 → 索引可能已过期（目录改名/删除），按最短间隔回退一次全量
      if roots and index_scope and not _covers(files, roots) \
          and not entry['error'] and dir_index.rescan_allowed(index_doc):
        logger.warning('[DocUpdate] 索引子树为空，回退全量 %s', dept_label)
        try:
          walk_frac = 0.0   # 这段是真重新走，允许进度重新推进
          files = await share.walk(
            root, numbers, on_progress=_walk_progress, stats=walk_stats)
        except Exception as exc:  # noqa: BLE001
          entry['error'] = f'共享目录遍历失败：{type(exc).__name__}: {str(exc)[:200]}'
        else:
          full_scan = True
      if cfg_roots and not entry['error']:
        # 缓存里的 fid 可能因目录改名/移动失效，重解析并按新 fid 补遍历
        files, _ = await _refresh_config_roots(
          cfg=cfg, roots=cfg_roots, errors=cfg_errors, entry=entry, root=root,
          numbers=numbers, listings=listings, files=files,
          on_progress=_walk_progress)
      if full_scan and index_scope and not walk_stats.get('truncated'):
        await _save_index(entry['company_id'], entry['dept_id'],
                          entry['dept_name'], index_scope, files)
      entry['walk'] = walk_stats
      if walk_stats.get('truncated'):
        entry.setdefault('warn', '目录数/深度触顶，遍历被截断，结果可能漏文件')
    elif not entry['error']:
      entry['error'] = '部门共享文档根为空（rootFileId 未配置）'

    dept_error = entry['error']
    # 独占的钉点：目录名本身就是该项的名字（子项循环前算一次，目录可能刚重解析过）
    named_roots = _named_roots(cfg_roots)
    # 按部门的文件名别名（只有廊坊 A 部门登记）：4.1.5 四条多数部门合订成一份
    # 「部门（级）隐患排查表」，文件名里没有专业词，光靠编号/关键词挑不出来
    name_map = paths.item_names(
      entry['company_id'], entry['company_name'], entry['dept_name'])
    # 确定性匹配与候选范围先算一遍（循环里复用）。模型复核**按范围合并成一次调用**：
    # 逐项单独问会互相串（A2 的记录表目录一个钉点挂 5 项，实测「客户IT库房应急卡」
    # 把「应急卡使用登记表」也算进来），合并后由模型按编号分派，冲突的条目直接丢弃
    det: dict[int, list[dict]] = {}
    scopes: dict[int, list[dict]] = {}
    rechecks: dict[int, dict] = {}
    if not dept_error:
      for item in scope:
        cfg_root = cfg_roots.get(item.seq)
        configured = item.seq in cfg
        # 配置项先收窄到配置目录子树、子树内仍按别名/编号挑；解析失败时 matched 为空，
        # **不回落启发式**（见 `_match_files`）
        det[item.seq] = _match_files(item, files, cfg_root, configured=configured,
                                     named=item.seq in named_roots,
                                     extra_names=name_map.get(item.seq, ()))
        scopes[item.seq] = _scope_files(item, files, cfg_root, configured=configured)
      # 已被确定性匹配占用的文件不再交给模型（否则未找到项可能被分到别人的文件）
      claimed = {str(f.get('fid')) for rows in det.values() for f in rows}
      groups: dict[tuple, list] = {}
      for item in scope:
        if det[item.seq] or not scopes[item.seq]:
          continue
        key = tuple(sorted(str(f.get('fid')) for f in scopes[item.seq]))
        groups.setdefault(key, []).append(item)
      for group in groups.values():
        pairs = [(member, scopes[member.seq]) for member in group]
        for seq, res in (await match_llm.recheck_many(pairs)).items():
          rechecks[seq] = {
            'files': [f for f in res['files'] if str(f.get('fid')) not in claimed],
            # 拿不准的候选同样剔掉已被确定性匹配占用的 fid（口径与 files 一致）
            'review_files': [f for f in res.get('review_files') or ()
                             if str(f.get('fid')) not in claimed],
            'reason': res.get('reason') or '',
          }
    done = base + walk_steps   # 遍历那一段到此记满
    for item in scope:
      done += 1
      cfg_root = cfg_roots.get(item.seq)
      configured = item.seq in cfg
      matched = det.get(item.seq, []) if not dept_error else []
      scoped = scopes.get(item.seq) or []
      mapped_by = 'path' if configured else 'heuristic'
      match_reason = ''
      # 模型拿不准的候选（仍未命中时才保留，供页面标「待复核」；不当已命中）
      review_files: list[dict] = []
      # 确定性匹配一行不动；只有它一条都没挑到时，才用模型在已解析出的范围里复核的结果
      # （业务叫法与文件名对不上，如「客户IT库房应急卡」↔「字节IT库房应急卡领取登记记录表」）
      if not matched and not dept_error:
        recheck = rechecks.get(item.seq) or {}
        match_reason = recheck.get('reason') or ''
        if recheck.get('files'):
          matched = recheck['files']
          mapped_by = 'llm'
        else:
          review_files = recheck.get('review_files') or []
      # 疑似停在模板上（编号命中的是模板、真实按月记录在不带编号的子目录里）：
      # 交给受控子 agent 在受控目录图内按名称模糊匹配重定位。只对查新增项、
      # 且命中文件都没有月份级期号时触发；成功则整项改用子 agent 的定位结果。
      if not dept_error and _agent_suspect(item, matched):
        if progress:
          out = progress(done, total_steps,
                         f'{dept_label} · {item.name} · 正在定位', 'items')
          if inspect.isawaitable(out):
            await out
        located = await _run_explore(item, files=files, cfg_root=cfg_root,
                                     matched=matched, scoped=scoped)
        if located.get('files'):
          matched = located['files']
          mapped_by = 'agent'
          match_reason = located.get('reason') or ''
          review_files = []
      from_name = bool(getattr(item, 'period_only', False))
      # 查更新项只判本周期的目标文件：先按年份剔除往年、再按月取最新一份。
      # 不改判定口径（仍是 sha256 vs 上月），只收窄「选中了哪些文件」—— 修两个
      # 确定性缺陷：钉点子树里往年文件被一并下载、按月命名的多份文件被整列。
      if not dept_error and item.judge == JUDGE_UPDATE and matched:
        matched = _narrow_update_targets(matched, int(expected[:4]))
      # 判定口径以 PDF 第三列为基准（见 `DocItem.judge`）。线上更新项**不再**按
      # 月份形态改判为查新增（那是「按月份名找了好几个月」的来源），始终按哈希判。
      judge = item.judge
      flipped = False
      # 查新增项用放宽后的接受下限裁剪，查更新项仍用本期（其实它本就不裁剪）
      judge_expected = new_floor if judge == JUDGE_NEW else expected
      layout = period.LAYOUT_PERIOD if from_name else period.detect_layout(matched)
      # 查新增项一律允许「目录无月份 → 文件名兜底」（目录有月份时仍优先，见 period_of_file）。
      # 旧年目录带月、新年目录只到年、月份写在文件名里是实测的普遍形态（A6/A10）；
      # 只看 layout 的话新年文件期号全空、会被按期裁剪掉、页面停在旧年目录。
      use_name = from_name or judge != JUDGE_UPDATE
      latest = period.pick_latest_period(matched, from_name=use_name)
      records: list[dict] = []
      if not dept_error:
        if judge != JUDGE_UPDATE:
          # 查新增：只记位置与期号，不下载不哈希
          records = [_period_record(f, use_name) for f in matched]
        else:
          for f in matched:
            if budget <= 0:
              records.append(_failed_record(
                f, f'超出 DOC_UPDATE_MAX_FILES={settings.DOC_UPDATE_MAX_FILES} 安全阀，未取'))
              continue
            budget -= 1
            try:
              res = await share.download_sha256(f['fid'])
              records.append(_flat_record(f, res, prev, taken_at))
            except Exception as exc:  # noqa: BLE001  单个文件失败不拖累其余文件/部门
              logger.warning('[DocUpdate] 文件处理失败 fid=%s: %s', f.get('fid'), exc)
              records.append(_failed_record(
                f, f'{type(exc).__name__}: {str(exc)[:200]}'))
      # 查新增项：目录链与文件名都解析不出期号的文件（模板/年度汇总/非常规写法）交模型兜底，
      # 只补空期号、过严格校验才写入；调用失败不影响结论
      period_source = 'name' if flipped and records else ''
      if not flipped and judge != JUDGE_UPDATE and records:
        det_periods = any(r.get('period') for r in records)
        if any(not r.get('period') for r in records):
          await period_llm.fill_periods(
            item.number_label, item.name, records, expected)
          periods = [r.get('period') for r in records if r.get('period')]
          latest = max(periods) if periods else latest
        if det_periods:
          period_source = 'name'
        elif any(r.get('period') for r in records):
          period_source = 'llm'
      # 带月份线索、却没有任何年份锚的文件：期号解析不出，交人工确认（不改判定）
      pending_names = _pending_files(matched) if flipped else []
      matched_count = len(matched)
      records = _relevant_records(records, judge_expected, judge, layout)
      # 实际命中的目录名（可能多处，如同一份月报存了三个地方），供页面展示核对
      actual_dirs = sorted({r['match_dir'] for r in records if r.get('match_dir')})
      entry['items'].append({
        'seq': item.seq,
        'number': item.number_label,
        'name': item.name,
        'cadence': item.cadence,
        'note': item.note,
        'judge': judge,
        'layout': layout,
        'period_only': from_name,
        'expected_period': expected,
        'latest_period': latest,
        # 查新增项的期号来源：文件名（确定性）/ 模型兜底 / 都没解析出
        'period_source': period_source,
        'actual_dirs': actual_dirs,
        'matched_count': matched_count,
        'mapped_by': mapped_by,
        # 模型复核的一句依据（没跑/没识别时为空）；不改判定口径
        'match_reason': match_reason,
        # 模型拿不准的候选（仅当仍未命中时非空）：下游判「待复核」，不参与期号/哈希判定。
        # 只存真实文件名（去重）—— 文件对象是内部中间结构，页面契约是 string[]
        'review_files': review_names(review_files),
        # 仅「按部门改判为查新增」的项目：带月份线索但没有年份锚的文件，待人工确认
        'pending_files': pending_names,
        # 实际查找路径（优先命中文件的真实目录，含年/月；无命中才回退钉点）
        'search_path': _search_path(records, cfg_root, scoped, matched),
        # 目录里的真实文件名：只在最终仍未找到时写（页面据此核对「到底有什么」）
        'dir_files': [] if matched else _dir_files(scoped),
        'config_path': list(cfg.get(item.seq) or ()),
        'warn': cfg_warns.get(item.seq, ''),
        'files': records,
        'error': dept_error or cfg_errors.get(item.seq) or ('' if matched else MISSING_TEXT),
      })
      if progress:
        out = progress(done, total_steps, f'{dept_label} · {item.name}', 'items')
        if inspect.isawaitable(out):
          await out

    # 有未找到项的钉点各截一张图（同钉点的多项共用一份）；补跑历史期不截
    if not target_period and not dept_error:
      await _attach_screenshots(entry, cfg_roots)

    out_depts.append(entry)

  elapsed = round(time.monotonic() - started, 2)
  logger.info('[DocUpdate] 部门快照完成 depts=%d files=%d elapsed=%ss',
              len(out_depts),
              sum(len(i['files']) for d in out_depts for i in d['items']), elapsed)
  return {
    'taken_at': taken_at,
    'period_key': expected,
    'period_label': f'{expected[:4]}年{expected[5:]}月',
    'scope': 'dept',
    # 补跑快照：读侧据此把晚于该期的文件排除在「那一期已交」之外
    'backfill': bool(target_period),
    'depts': out_depts,
    'items': [],
    'file_count': sum(len(i['files']) for d in out_depts for i in d['items']),
    'elapsed': elapsed,
  }


def _dept_key(dept: dict) -> tuple[str, str]:
  """部门键：公司 + dept_id（dept_id 空时退回名称，防跨公司串项）。"""
  return (str(dept.get('company_id') or ''),
          str(dept.get('dept_id') or dept.get('dept_name') or ''))


def _merge_dept_snapshot(base: dict, patch: dict) -> dict:
  """按 `(company_id, dept_id)` + `seq` 把单项刷新结果并回整期快照。"""
  by_key = {_dept_key(d): d for d in base.get('depts') or []}
  merged: list[dict] = []
  for dept in patch.get('depts') or []:
    old = by_key.get(_dept_key(dept))
    if not old:
      merged.append(dept)
      continue
    repl = {i.get('seq'): i for i in dept.get('items') or []}
    items = [repl.pop(i.get('seq'), i) for i in old.get('items') or []]
    items.extend(repl.values())  # 本期新增项兜底，不漏项
    entry = dict(old)
    entry.update(dept)
    entry['items'] = items
    entry['error'] = dept.get('error') or old.get('error') or ''
    merged.append(entry)
  seen = {_dept_key(d) for d in merged}
  merged.extend(d for d in base.get('depts') or [] if _dept_key(d) not in seen)

  out = {k: v for k, v in base.items() if k != 'depts'}
  out.update({k: v for k, v in patch.items() if k != 'depts'})
  out['depts'] = merged
  out['file_count'] = sum(len(i.get('files') or [])
                          for d in merged for i in d.get('items') or [])
  return out


def merge_snapshot(base: dict | None, patch: dict) -> dict:
  """把单项刷新的结果并回整期快照：按 `seq` 替换命中项，其余项原样保留。

  `base` 为空时直接返回 patch（临时比对用：拼到上期快照上算 verdict，不落库）。
  部门口径（patch 带 `depts`）按 `((company_id, dept_id), seq)` 合并。
  """
  if not base:
    return patch
  if 'depts' in patch:
    return _merge_dept_snapshot(base, patch)
  replacements = {i.get('seq'): i for i in patch.get('items') or []}
  merged = [replacements.pop(i.get('seq'), i) for i in base.get('items') or []]
  merged.extend(replacements.values())  # 本期新增项兜底，不漏项
  out = dict(base)
  out.update({
    'items': merged,
    'file_count': sum(len(i.get('files') or []) for i in merged),
    'taken_at': patch.get('taken_at') or base.get('taken_at'),
    'elapsed': patch.get('elapsed', 0),
  })
  return out


def _tz():
  from zoneinfo import ZoneInfo

  return ZoneInfo(settings.SCHEDULED_TASK_TIMEZONE or 'Asia/Shanghai')

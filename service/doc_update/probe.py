"""只读探测：把显式路径配置拿去真实共享目录里核一遍，并诊断年/月期号能否对上。

跑法：`python -m service.doc_update.cli --probe [--dept A1,A5] [--company 廊坊]
[--seq 22] [--depth 6]`

**为什么不直接用 `--sync` 核对**：`cli.py --sync` 即使不加 `--yes` 也会写
`doc_update_depts` / `doc_update_paths` / `doc_update_dir_index` 三个 Mongo 缓存，
并真实下载 6 个「查更新」项算 sha256。探测必须是零写库、零下载的独立入口。

允许的副作用只有一处：部门接口与列目录会经 `service/query/mgmt/http` 登录，在 Redis
写服务账号 token 缓存（与任何只读接口一致）。部门 id/根 fid 只做本次内存解析，
**不回写注册表**。

输出全部以 `PROBE ` 开头（逐项一行 + `PROBE SUMMARY` 汇总），便于与业务给的路径文档
逐条对。期号判断只用 `period` 纯函数；目录形态标签用 `dir_index.is_period_dir`。
"""

import re

from service.doc_update import depts, dir_index, paths, period, share
from service.doc_update.catalog import MONTHLY_ITEMS

_MONTH_DIR = re.compile(r'^\d{1,2}\s*月(?:份)?$')

STATUS_OK = 'OK'
STATUS_PATH = '配置路径不存在'
STATUS_PERIOD = '期号未对上'
STATUS_MONTH_NO_YEAR = '月目录缺年份'
STATUS_NO_FILES = '无文件'
STATUS_WALK = '遍历失败'
STATUS_UNCONFIGURED = '未配显式路径'

_MAX_DIR_LINES = 12
_MAX_FILE_LINES = 8


def _norm(text) -> str:
  return str(text or '').replace('\u3000', ' ').strip()


def _join(parts) -> str:
  return ' / '.join(str(p) for p in parts or ())


def _dir_kind(name: str) -> str:
  if _MONTH_DIR.match(_norm(name)):
    return '月容器'
  return '期号容器' if dir_index.is_period_dir(name) else '稳定'


def _month_without_year(file: dict) -> bool:
  """链里有裸 `M月` 目录、但整条链解析不出期号 —— 对齐只能靠上传时间，不可信。"""
  segments = file.get('segments') or []
  if not any(_MONTH_DIR.match(_norm(s)) for s in segments):
    return False
  return not period.parse_period(segments)


def _file_period(file: dict) -> tuple[str, str]:
  """`(期号, 来源)`：目录优先，目录解析不到再看文件名。"""
  found = period.period_of_file(file)
  if found:
    return found, '目录'
  found = period.period_of_file(file, from_name=True)
  return (found, '文件名') if found else ('', '')


def _classify(item, resolved: dict, files: list[dict], expected: str) -> tuple[str, str]:
  """按 `judge` / 目录形态给该项一个探测结论 `(状态, 详情)`。"""
  if resolved.get('error'):
    return STATUS_PATH, resolved['error']
  if not files:
    return STATUS_NO_FILES, ''
  # 线上更新项若在该部门按月新交（多个期号/月份目录），抓取阶段会自动改按期判，
  # 探测结论必须跟着走，否则一直显示「期号无关」
  flipped = item.judge == 'update' and period.auto_flip(files)
  if item.judge == 'update' and not flipped:
    return STATUS_OK, '查更新项（期号无关）'
  if any(_month_without_year(f) for f in files):
    return STATUS_MONTH_NO_YEAR, '月份目录没有年份容器，期号可能靠上传时间推出来'
  # 与抓取侧同一口径：查新增项（及自动改判的更新项）目录解析不出期号时退到文件名，
  # 否则「旧年有月目录、新年只有文件名月份」的项会一直报旧年期号。
  use_name = (bool(getattr(item, 'period_only', False))
              or item.judge != 'update' or flipped)
  latest = period.pick_latest_period(files, from_name=use_name)
  note = '（线上更新项自动改按期判）' if flipped else ''
  if latest and latest >= expected:
    return STATUS_OK, f'latest={latest}{note}'
  return STATUS_PERIOD, f'latest={latest or "-"}（应到 {expected}）{note}'


async def _dept_rows(names, companies) -> list[dict]:
  """配置清单 × 只读注册表缓存 → 待探测部门（缺 id/根的在后续按需联网解析）。"""
  cached = {(str(r.get('company_id') or ''), str(r.get('dept_name') or '')): r
            for r in await depts.cached_registry()}
  wanted = {str(n).upper() for n in (names or [])}
  wanted_c = {str(c).strip().lower() for c in (companies or []) if str(c).strip()}
  out: list[dict] = []
  for group in depts.configured_groups():
    cid = str(group.get('company_id') or '')
    label = str(group.get('company_name') or '')
    if wanted_c and cid.lower() not in wanted_c and label.lower() not in wanted_c:
      continue
    for name in group.get('names') or []:
      if wanted and name.upper() not in wanted:
        continue
      row = dict(cached.get((cid, name)) or {})
      row.update({'company_id': cid, 'company_name': label, 'dept_name': name})
      out.append(row)
  return out


async def _resolve_root(row: dict) -> tuple[str, str]:
  """取部门共享根 fid；缓存缺值时直接查接口，**不写注册表**。返回 `(fid, 说明)`。"""
  if row.get('root_fid') and row.get('dept_id'):
    return str(row['root_fid']), ''
  note = '本次解析未落缓存'
  cid = str(row.get('company_id') or '')
  if not row.get('dept_id'):
    try:
      found = {d['dept_name']: d['dept_id'] for d in await depts.list_departments(cid)}
    except Exception as exc:  # noqa: BLE001  探测失败只报告
      return '', f'部门清单获取失败：{type(exc).__name__}: {str(exc)[:120]}'
    row['dept_id'] = found.get(_norm(row.get('dept_name')), '')
  if not row.get('dept_id'):
    return '', '未在部门清单中找到该部门'
  if not row.get('root_fid'):
    try:
      row['root_fid'] = await depts.root_file_id(row['dept_id'])
    except Exception as exc:  # noqa: BLE001
      return '', f'部门详情获取失败：{type(exc).__name__}: {str(exc)[:120]}'
  if not row.get('root_fid'):
    return '', '部门共享目录根为空'
  return str(row['root_fid']), note


async def _list_children(fid: str, listings: dict) -> list[dict]:
  rows = listings.get(fid)
  if rows is None:
    rows = await share.list_dir(fid)
    listings[fid] = rows
  return rows


def _print_children(children: list[dict], out) -> None:
  dirs = [c for c in children if c.get('is_dir')]
  for child in dirs[:_MAX_DIR_LINES]:
    out(f"PROBE   dir {(_norm(child.get('name')) or '-'):<28} {_dir_kind(child.get('name'))}")
  if len(dirs) > _MAX_DIR_LINES:
    out(f'PROBE   dir …另有 {len(dirs) - _MAX_DIR_LINES} 个子目录')


def _print_files(files: list[dict], out) -> None:
  for file in files[:_MAX_FILE_LINES]:
    value, source = _file_period(file)
    text = f'{value}({source})' if value else '-'
    out(f"PROBE   file {(_norm(file.get('topic')) or '-'):<32} period={text}")
  if len(files) > _MAX_FILE_LINES:
    out(f'PROBE   file …另有 {len(files) - _MAX_FILE_LINES} 个文件')


async def run(dept_names=None, companies=None, seqs=None, depth: int = 6,
              max_dirs: int = 300, out=print) -> int:
  """探测并打印结论；返回进程退出码（部门根全部取不到才算失败）。"""
  items = [i for i in MONTHLY_ITEMS if i.in_scope and (not seqs or i.seq in set(seqs))]
  rows = await _dept_rows(dept_names, companies)
  expected = period.expected_period()
  counts = {key: 0 for key in (STATUS_OK, STATUS_PATH, STATUS_PERIOD, STATUS_MONTH_NO_YEAR,
                               STATUS_NO_FILES, STATUS_WALK, STATUS_UNCONFIGURED)}
  out(f'PROBE 期号应到 {expected}；部门 {len(rows)} 个、清单 {len(items)} 项、深度 {depth}')
  reachable = 0

  for row in rows:
    label = f"{row.get('company_name') or row.get('company_id') or '-'} · {row.get('dept_name')}"
    root, note = await _resolve_root(row)
    if not root:
      out(f'PROBE dept={label} 部门根不可用：{note}')
      continue
    reachable += 1
    if note:
      out(f'PROBE dept={label} 注意：{note}')
    cfg = paths.configured(row['company_id'], row.get('company_name') or '',
                           row.get('dept_name') or '')
    listings: dict = {}
    for item in items:
      segments = cfg.get(item.seq) or ()
      head = f'PROBE {label} seq={item.seq} {item.name}'
      if not segments:
        counts[STATUS_UNCONFIGURED] += 1
        out(f'{head} 未配显式路径（走启发式，不在探测范围）')
        continue
      resolved = await paths.resolve_path(root, segments, listings=listings)
      actual = tuple(resolved.get('segments') or ())
      if not resolved.get('error'):
        try:
          children = await _list_children(resolved['fid'], listings)
        except Exception as exc:  # noqa: BLE001
          counts[STATUS_WALK] += 1
          out(f'{head} 列目录失败：{type(exc).__name__}: {str(exc)[:120]}')
          continue
        _print_children(children, out)
      files: list[dict] = []
      stats: dict = {}
      if resolved.get('fid'):
        try:
          files = await share.walk_roots(
            [{'fid': resolved['fid'], 'segments': list(actual)}], (),
            max_dirs=max_dirs, max_depth=depth, stats=stats,
          )
        except Exception as exc:  # noqa: BLE001
          counts[STATUS_WALK] += 1
          out(f'{head} 遍历失败：{type(exc).__name__}: {str(exc)[:120]}')
          continue
      status, detail = _classify(item, resolved, files, expected)
      counts[status] += 1
      out(f'{head} path={_join(actual) or "-"} fid={resolved.get("fid") or "-"} '
          f'files={len(files)} status={status} {detail}'.rstrip())
      if resolved.get('warn'):
        out(f'PROBE   warn {resolved["warn"]}')
      if stats.get('truncated'):
        out(f'PROBE   warn 遍历触顶被截断（已访问 {stats.get("dirs")} 个目录）')
      _print_files(files, out)

  out('')
  summary = ' · '.join(f'{key} {value}' for key, value in counts.items() if value)
  out(f'PROBE SUMMARY {summary or "无结论"}')
  return 0 if reachable else 1

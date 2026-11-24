"""疑似没定位到真实记录时的受控子 agent：在**已遍历文件的目录图内**按名称模糊匹配找目录。

确定性匹配按编号命中的往往是**外层模板**，真实按月记录在**不带编号**的子目录里
（实测惠州 A2：钉点 `2.3.3交接班管理`，模板 `2.3.3.2…签字表.xlsx` 被编号命中且无期号，
真实记录却在 `交接班签字表2026/`）。逐项登记别名永远补不完，故改由子 agent 探索定位。

**受控范围（allowlist）**：模型只能对「调用方给的起点目录」及其在 `list_dir` 里
**实际列出的子目录**操作；`submit_selection` 只接受 allowlist / 已列出文件内的 fid，
伪造/越界的 fid 一律拒绝。模型无法触及这张图以外的任何目录。

**有界**：总超时、工具往返步数、可下钻目录数、单次列目录条目数都有上限（见 settings）。

**fail-soft**：任何异常/超时都返回空 dict，调用方保持确定性匹配结果；
`SyncCancelled`（继承 BaseException）必须原样上抛，不能被吞成本次失败。
"""

from __future__ import annotations

import asyncio
import json
import logging

from config.settings import settings
from service.doc_update import share
from service.doc_update.sync_state import SyncCancelled

logger = logging.getLogger(__name__)

_SYSTEM = (
  '你是运维共享盘的文档定位器。给定一条清单项（编号/名称/说明/关键词/登记目录名）和'
  '一个起点目录，你要判断该项的**真实月度记录**放在哪个子目录。\n'
  '关键事实：清单编号（如 `2.3.3.2`）常出现在**模板文件或模板目录**上，而真实按月记录'
  '往往放在**不带编号**的子目录里（例：`2.3.3交接班管理/交接班签字表2026/`，'
  '里面按月存 `1月.pdf`、`10月.pdf`）。**不要停在带编号的模板上。**\n'
  '按**名称语义**做模糊匹配，不要求逐字相同（`交接班管理` ↔ `交接班签字表`，'
  '`隐患治理` ↔ `隐患排查记录表` 都算同一主题）。优先选含年份/月份归档、或名字像'
  '「记录/登记/台账/报表」的目录；只选**真实记录**，不选模板/制度/空白表。\n'
  '用 `list_dir` 逐层探索（可只列一次拿到的子目录 fid），拿不准就把最可能的目录交上去'
  '并在 reason 里说明。**不得编造 fid**；找不到合适的目录就不要提交。'
)

_TOOLS = [
  {
    'type': 'function',
    'function': {
      'name': 'list_dir',
      'description': '列出一个目录下的子目录与文件名。只能列出本次给出的目录 fid。',
      'parameters': {
        'type': 'object',
        'properties': {
          'dir_fid': {'type': 'string', 'description': '要列出的目录 fid'},
        },
        'required': ['dir_fid'],
      },
    },
  },
  {
    'type': 'function',
    'function': {
      'name': 'submit_selection',
      'description': '选定最可能是该项真实记录所在的目录（和/或具体文件），结束定位。',
      'parameters': {
        'type': 'object',
        'properties': {
          'dir_fids': {
            'type': 'array', 'items': {'type': 'string'},
            'description': '选中的目录 fid：真实记录在该目录子树内',
          },
          'file_fids': {
            'type': 'array', 'items': {'type': 'string'},
            'description': '直接选中的文件 fid（可选）',
          },
          'reason': {'type': 'string', 'description': '一句判断依据'},
        },
        'required': ['dir_fids'],
      },
    },
  },
]


def _norm(text) -> str:
  return str(text or '').replace('\u3000', ' ').strip()


def _item_brief(item) -> dict:
  return {
    'number': item.number_label,
    'name': item.name,
    'note': item.note or '',
    'keywords': list(item.keywords or ()),
    'folders': list(item.folders or ()),
  }


class _Ctx:
  """一次定位的可写上下文：目录 allowlist、已列出文件、选中结果。"""

  __slots__ = ('allowed', 'files', 'listed_files', 'selected', 'reason',
               'dirs_seen')

  def __init__(self, start_fid: str, start_segments: list[str]):
    # fid → 该目录从部门根往下的名字链（用于回填 segments/match_dir）
    self.allowed: dict[str, list[str]] = {}
    if start_fid:
      self.allowed[start_fid] = list(start_segments)
    self.files: dict[str, str] = {}          # 已列出的文件名 fid → name
    self.listed_files: dict[str, str] = {}   # 同 files（语义别名，便于阅读）
    self.selected: dict | None = None
    self.reason = ''
    self.dirs_seen = 0

  def near_dir(self, file_rec: dict) -> tuple[int, str]:
    """文件落在哪个已选目录下更深（返回 `(链长度, 目录名)`，用于 match_dir）。"""
    best = (-1, '')
    selected = set(self.selected['dir_fids']) if self.selected else set()
    for seg in file_rec.get('chain') or []:
      fid = str(seg.get('fid') or '')
      if fid in selected:
        segs = self.allowed.get(fid) or []
        if len(segs) > best[0]:
          best = (len(segs), segs[-1] if segs else str(seg.get('name') or ''))
    return best


async def _list_dir(name: str, args: dict, ctx: _Ctx, files) -> str:
  fid = _norm(args.get('dir_fid') or args.get('fid') or '')
  if fid not in ctx.allowed:
    return json.dumps({'error': '该目录不在可访问范围（只能列出已给出的目录 fid）'},
                      ensure_ascii=False)
  if ctx.dirs_seen >= max(1, int(settings.DOC_UPDATE_EXPLORE_MAX_DIRS or 1)):
    return json.dumps({'error': '已达目录探索上限，请直接 submit_selection'},
                      ensure_ascii=False)
  ctx.dirs_seen += 1
  parent = ctx.allowed[fid]
  try:
    rows = await share.list_dir(fid)
  except Exception as exc:  # noqa: BLE001  列目录失败反馈给模型，不影响整体
    return json.dumps({'error': f'列目录失败：{type(exc).__name__}'}, ensure_ascii=False)
  limit = max(1, int(settings.DOC_UPDATE_EXPLORE_MAX_ENTRIES or 1))
  out: list[dict] = []
  for row in rows:
    name = _norm(row.get('name'))
    rfid = str(row.get('fid') or '')
    if not name or not rfid:
      continue
    if row.get('is_dir'):
      ctx.allowed.setdefault(rfid, parent + [name])
      out.append({'fid': rfid, 'name': name, 'dir': True})
    else:
      ctx.files[rfid] = name
      ctx.listed_files[rfid] = name
      out.append({'fid': rfid, 'name': name, 'dir': False})
  return json.dumps({'dir_fid': fid, 'entries': out[:limit],
                     'truncated': len(out) > limit}, ensure_ascii=False)


def _submit(args: dict, ctx: _Ctx) -> str:
  raw_dirs = args.get('dir_fids') or []
  raw_files = args.get('file_fids') or []
  if not isinstance(raw_dirs, list) or not isinstance(raw_files, list):
    return json.dumps({'error': 'dir_fids/file_fids 必须是数组'}, ensure_ascii=False)
  dirs = [str(d) for d in raw_dirs if str(d) in ctx.allowed]
  files = [str(f) for f in raw_files if str(f) in ctx.files]
  if raw_dirs and not dirs:
    return json.dumps({'error': '所选目录 fid 均不在可访问范围，请用 list_dir 拿到的 fid'},
                      ensure_ascii=False)
  if not dirs and not files:
    return json.dumps({'error': '没有选中任何有效目录或文件'}, ensure_ascii=False)
  ctx.selected = {'dir_fids': dirs, 'file_fids': files}
  ctx.reason = _norm(args.get('reason'))[:300]
  return json.dumps({'ok': True, 'selected_dirs': len(dirs), 'selected_files': len(files)},
                    ensure_ascii=False)


async def _exec_tool(name: str, args: dict, ctx: _Ctx, files) -> str:
  if name == 'list_dir':
    return await _list_dir(name, args, ctx, files)
  if name == 'submit_selection':
    return _submit(args, ctx)
  return json.dumps({'error': f'未知工具：{name}'}, ensure_ascii=False)


def _make_bound():
  """模型出口（模块级，测试可 monkeypatch，不连网）。"""
  from core.model_gateway import model_gateway

  return model_gateway.as_langchain_model(temperature=0).bind_tools(_TOOLS)


def _mapped(ctx: _Ctx, files) -> list[dict]:
  """把选中的目录/文件映射回真实文件记录（链里含选中目录 fid 的即算）。"""
  if not ctx.selected:
    return []
  dirs = set(ctx.selected['dir_fids'])
  file_fids = set(ctx.selected['file_fids'])
  out: list[dict] = []
  seen: set[str] = set()
  for f in files:
    fid = str(f.get('fid') or '')
    if not fid or fid in seen:
      continue
    hit = fid in file_fids or any(
      str(seg.get('fid') or '') in dirs for seg in f.get('chain') or [])
    if not hit:
      continue
    seen.add(fid)
    _depth, dir_name = ctx.near_dir(f)
    out.append(dict(f, match_dir=dir_name))
  return out


async def locate(item, *, files, start: dict | None, on_progress=None) -> dict:
  """在受控目录图内定位该项的真实记录目录。

  返回 `{'files': [...], 'dir_fids': [...], 'reason': str}`；空 dict 表示未定位 /
  未启用 / 失败（调用方保持确定性结果）。`start` 为 `{'fid','segments'}`；
  无 `start.fid` 时无法探索，直接返回空。
  """
  if not settings.DOC_UPDATE_EXPLORE_ENABLED:
    return {}
  start_fid = _norm((start or {}).get('fid') or '')
  if not start_fid:
    return {}
  start_segments = [_norm(s) for s in ((start or {}).get('segments') or []) if _norm(s)]
  ctx = _Ctx(start_fid, start_segments)
  payload = {'item': _item_brief(item), 'start_fid': start_fid,
             'start_path': ' / '.join(start_segments)}
  messages = [{'role': 'system', 'content': _SYSTEM},
              {'role': 'user', 'content': json.dumps(payload, ensure_ascii=False)}]
  max_steps = max(1, int(settings.DOC_UPDATE_EXPLORE_MAX_STEPS or 1))
  timeout = float(settings.DOC_UPDATE_EXPLORE_TIMEOUT or 0) or None
  try:
    bound = _make_bound()
    async with asyncio.timeout(timeout):
      for _step in range(max_steps):
        ai = await bound.ainvoke(messages)
        messages.append(ai)
        calls = getattr(ai, 'tool_calls', None) or []
        if not calls:
          break
        for call in calls:
          name = call.get('name') or ''
          args = call.get('args') or {}
          result = await _exec_tool(name, args, ctx, files)
          messages.append({'role': 'tool', 'tool_call_id': call.get('id'),
                          'content': result})
        if ctx.selected:
          break
  except SyncCancelled:
    raise
  except asyncio.TimeoutError:
    logger.warning('[DocUpdate] 子 agent 定位超时：%s %s', item.number_label, item.name)
    return {}
  except Exception as exc:  # noqa: BLE001  定位失败 fail-soft，保持确定性结论
    logger.warning('[DocUpdate] 子 agent 定位失败：%s %s: %s',
                   item.number_label, item.name, str(exc)[:200])
    return {}
  if not ctx.selected:
    return {}
  mapped = _mapped(ctx, files)
  logger.info('[DocUpdate] 子 agent 定位 %s %s：%d 个目录、%d 个文件（%s）',
              item.number_label, item.name, len(ctx.selected['dir_fids']),
              len(mapped), ctx.reason[:80])
  # 选中目录的 fid→名字链：快照里没映射到文件时，调用方据此按 fid 有界补遍历
  dirs = [{'fid': fid, 'segments': list(ctx.allowed.get(fid) or ())}
          for fid in ctx.selected['dir_fids']]
  return {
    'files': mapped,
    'dir_fids': list(ctx.selected['dir_fids']),
    'dirs': dirs,
    'reason': ctx.reason,
  }

"""群消息检索的纯函数：关键词 / 发送人 / 类型过滤 + 表格行渲染。

数据来自 `source.load_messages`（含 `kind='other'`），本模块**不连库、不调外部接口**，
便于纯 assert 自检。关键词口径与 `detail.search` 一致：按中英文逗号/顿号/分号/空白切 token，
**全部命中**（AND）、忽略大小写。
"""

import re
from datetime import datetime
from zoneinfo import ZoneInfo

_TZ = ZoneInfo('Asia/Shanghai')

KINDS = ('change', 'sos', 'first', 'other')
_KIND_LABELS = {'change': '变更', 'sos': '速报', 'first': '通报', 'other': '消息'}
# 中文别名 → kind 码（模型常直接写「速报/通报」，别让它因此报错）
_KIND_ALIASES = {'变更': 'change', '变更通知': 'change',
                 '速报': 'sos', '首报': 'first', '通报': 'first',
                 '消息': 'other', '普通消息': 'other'}
_TOKEN_SPLIT = re.compile(r'[,，、;；\s]+')

TABLE_COLUMNS = ('时间', '园区', '类型', '发送人', '内容')


def kind_label(kind: str) -> str:
  """kind 码 → 中文（未知值原样返回，不丢行）。"""
  return _KIND_LABELS.get((kind or '').strip(), kind or '')


def kind_from_alias(value: str) -> str:
  """kind 入参归一：''/all/全部 → ''（不过滤）；中文别名 → 码；其余转小写原样返回。"""
  text = (value or '').strip()
  if text in ('', 'all', '全部', '所有'):
    return ''
  return _KIND_ALIASES.get(text, text.lower())


def split_tokens(query: str) -> list[str]:
  """查询串 → 去重后的小写 token；空串 = 不过滤。"""
  tokens: list[str] = []
  for token in _TOKEN_SPLIT.split((query or '').strip()):
    lowered = token.lower()
    if lowered and lowered not in tokens:
      tokens.append(lowered)
  return tokens


def _haystack(row: dict) -> str:
  return f"{row.get('title') or ''}\n{row.get('text') or ''}".lower()


def filter_messages(rows: list[dict], *, tokens: list[str] | tuple[str, ...] = (),
                    sender: str = '', kinds: tuple[str, ...] | list[str] = ()) -> list[dict]:
  """按关键词（AND）/ 发送人（子串）/ kind 过滤；`kinds` 为空 = 全部类型。"""
  sender_q = (sender or '').strip().lower()
  kind_set = {k for k in kinds if k}
  out: list[dict] = []
  for row in rows:
    if kind_set and (row.get('kind') or '') not in kind_set:
      continue
    if sender_q:
      # WS 只给 open_id 时 sender 为空，用 sender_id 兜底
      hay = f"{row.get('sender') or ''} {row.get('sender_id') or ''}".lower()
      if sender_q not in hay:
        continue
    if tokens:
      hay = _haystack(row)
      if not all(token in hay for token in tokens):
        continue
    out.append(row)
  return out


def _format_time(value) -> str:
  try:
    ts = int(value)
  except (TypeError, ValueError):
    return ''
  return datetime.fromtimestamp(ts, _TZ).strftime('%Y-%m-%d %H:%M:%S')


def to_table_rows(rows: list[dict], park_by_chat: dict[str, str] | None = None) -> list[dict]:
  """PG 行 → 服务端表格行（列见 `TABLE_COLUMNS`）。时间按北京时间渲染。"""
  parks = park_by_chat or {}
  out: list[dict] = []
  for row in rows:
    content = (row.get('text') or '').strip() or (row.get('title') or '').strip()
    out.append({
      '时间': _format_time(row.get('create_time')),
      '园区': parks.get(row.get('chat_id') or '') or (row.get('cluster') or ''),
      '类型': kind_label(row.get('kind') or ''),
      '发送人': (row.get('sender') or '').strip() or (row.get('sender_id') or '').strip(),
      '内容': content,
    })
  return out

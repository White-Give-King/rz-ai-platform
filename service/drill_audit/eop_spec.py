"""从 EOP 文档里抽出判定用的「标尺」：岗位/时限条款高亮 + §9.3 必需步骤清单。

EOP 正文很长（judge 侧只给 8000 字预算，关键条款常在中后段被挤掉），所以这里做两件事：

1. `highlight_lines` —— 用容错正则抓 §9.1/§9.2/§9.3.x/§10.x 这些带岗位/时限的条款行，
   置顶喂模型，避免被无关正文挤掉；
2. `extract_steps` —— 把 §9.3 拆成一条条必需步骤（动作/执行人/执行位置/时间限定），
   给模型做「步骤完整性」勾对（查漏做）。

两者都是容错解析、解析不到就留空（不编造）；入参是 `[{file_name, content}]`。
"""

import re

# 全角数字/句点 → 半角（只动这两类，其余原文不动），便于统一匹配
_FULLWIDTH = str.maketrans('０１２３４５６７８９．', '0123456789.')

# 行首的 Markdown 装饰（表格竖线、引用、标题井号、列表符号）
_LEAD_JUNK = re.compile(r'^[\s|>#\-*•·]+')
# 目录页的点线：连续点或省略号，命中即视为目录行
_TOC_DOTS = re.compile(r'\.{4,}|…{3,}|(?:\.[ \t]*){5,}')
_NUM_LEAD = re.compile(r'(\d+(?:\.\d+)*)')
_STEP_NUM = re.compile(r'^[\s|>#\-*•·]*(9\.3(?:\.\d+)+)')
# 「执行人：…」这类字段标签，值取到下一个标签/分隔符/行尾（允许内部空格）
_LABEL_ALT = r'(执行人|责任人|执行位置|执行地点|时间限定|时限)'
_CONT_LABEL = re.compile(
  _LABEL_ALT + r'\s*[：:]\s*(.+?)'
  r'(?=' + _LABEL_ALT + r'\s*[：:]|[|｜，,；;]|$)'
)
_DURATION_CELL = re.compile(
  r'^\s*(?:\d+(?:\.\d+)?\s*(?:分钟|小时|分|秒|h|min|s|m)\s*)+$', re.I
)
_EXEC_HINT = re.compile(r'组|值班长|班长')
_LOC_HINT = re.compile(r'室|机房|走廊|大厅|监控')
_LABEL_KEY = {
  '执行人': 'executor', '责任人': 'executor',
  '执行位置': 'location', '执行地点': 'location',
  '时间限定': 'time_limit', '时限': 'time_limit',
}


def _norm(text: str) -> str:
  return (text or '').translate(_FULLWIDTH)


def _target_section(num: str) -> bool:
  """只认 §9.1 / §9.2 / §9.3.x / §10.x；`9.10`/`9.11` 这类不算（会被误当章号）。"""
  parts = num.split('.')
  if not parts or not parts[0].isdigit():
    return False
  if parts[0] == '10':
    return True
  if parts[0] == '9' and len(parts) >= 2:
    return parts[1] in ('1', '2', '3')
  return False


def _doc_lines(eop_docs) -> list[str]:
  lines: list[str] = []
  for doc in eop_docs or []:
    content = str((doc or {}).get('content') or '')
    lines.extend(content.splitlines())
  return lines


def highlight_lines(eop_docs: list[dict], max_chars: int = 2000) -> str:
  """抽 §9.1/§9.2/§9.3.x/§10.x 条款行（去重、截到 max_chars），无命中返回空串。"""
  out: list[str] = []
  seen: set[str] = set()
  used = 0
  for raw in _doc_lines(eop_docs):
    line = _norm(raw).strip()
    if not line or _TOC_DOTS.search(line):
      continue
    match = _NUM_LEAD.match(_LEAD_JUNK.sub('', line))
    if not match or not _target_section(match.group(1)):
      continue
    if line in seen:
      continue
    seen.add(line)
    remaining = max_chars - used
    if remaining <= 0:
      break
    if len(line) + 1 > remaining:
      if not out:
        out.append(line[:remaining])
      break
    out.append(line)
    used += len(line) + 1
  return '\n'.join(out)


def _split_cells(text: str) -> list[str]:
  if '|' not in text and '｜' not in text:
    return []
  return [cell.strip() for cell in re.split(r'[|｜]', text) if cell.strip()]


def _classify(cell: str) -> str:
  """单元格归类：先时长（最确定）→ 位置 → 执行人，都不像就返回空串（当动作）。"""
  if _DURATION_CELL.match(cell):
    return 'time_limit'
  if _LOC_HINT.search(cell):
    return 'location'
  if _EXEC_HINT.search(cell):
    return 'executor'
  return ''


def _fill_step(step: dict, text: str) -> None:
  """把一行（步骤行或续行）解析进 step：标签字段优先，其次表格单元格/多空格分栏。"""
  text = (text or '').strip()
  if not text:
    return
  for m in _CONT_LABEL.finditer(text):
    key = _LABEL_KEY.get(m.group(1))
    if key and not step[key]:
      step[key] = m.group(2).strip()
  body = _CONT_LABEL.sub('', text).strip()
  if not body:
    return
  cells = _split_cells(body)
  if cells:
    for cell in cells:
      kind = _classify(cell)
      if kind:
        if not step[kind]:
          step[kind] = cell
      else:
        step['action'] = f"{step['action']} {cell}".strip()
    return
  tokens = [t for t in re.split(r'\s{2,}|\t+', body) if t]
  if len(tokens) > 1:
    if not step['action']:
      step['action'] = tokens[0]
    for tok in tokens[1:]:
      kind = _classify(tok)
      if kind and not step[kind]:
        step[kind] = tok
    return
  # 单段纯文本：动作本身，或是动作的续行
  step['action'] = (f"{step['action']} {body}".strip()
                    if step['action'] else body)


def extract_steps(eop_docs: list[dict]) -> list[dict]:
  """§9.3 → `[{no, action, executor, location, time_limit}]`；无 §9.3 返回 []。

  容错同一行/分行/表格摊平三种形态；解析不到的字段留空（不编造）；只有编号没有
  内容的行（多半是表头）丢弃。
  """
  cur: dict | None = None
  steps: list[dict] = []
  for line in (_norm(x).strip() for x in _doc_lines(eop_docs)):
    if not line:
      continue
    match = _STEP_NUM.match(line)
    if match:
      cur = {'no': match.group(1), 'action': '',
             'executor': '', 'location': '', 'time_limit': ''}
      steps.append(cur)
      _fill_step(cur, line[match.end():].strip())
      continue
    if cur is None:
      continue
    lead = _NUM_LEAD.match(_LEAD_JUNK.sub('', line))
    if (lead and _target_section(lead.group(1))
            and not lead.group(1).startswith('9.3.')):
      cur = None
      continue
    _fill_step(cur, line)
  return [s for s in steps if s['action']]

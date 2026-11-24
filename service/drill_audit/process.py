"""演练记录 OCR 文本 → 时间轴文字 + 流程图（纯函数，无 IO、可单测）。

演练记录是扫描件表单，OCR 会把合并单元格摊成十几列重复内容。这里做两件事：

1. `normalize_ocr_text` —— 把表格压成逐行文字（相邻重复单元格折叠、分隔行丢弃）；
2. `extract_timeline` / `extract_header` / `build_flow_mermaid` —— 从正文里确定性地
   抽出「演练场景 / 人员分配 / 按时间轴排列的演练步骤」，并按**人员分配里的班组**
   生成泳道并行流程图。零 LLM 调用、不截断步骤。
"""

import html
import re

# 时间戳：`0分11秒`。必须带否定环视 —— 通报卡片里的 `2026年04月09日10时51分0秒`
# 不是演练步骤。
TS = re.compile(r'(?<![\d时])(\d+)\s*分\s*(\d{1,2})\s*秒')

_BR_TAG = re.compile(r'<br\s*/?>', re.I)
_BLOCK_TAG = re.compile(r'</?(?:td|th|tr|table|p|div|h[1-6]|li|ul|ol)\b[^>]*>', re.I)
_ANY_TAG = re.compile(r'</?[a-zA-Z][^>]*>')
_SEPARATOR_ROW = re.compile(r'^[-: ]+$')
_IMAGE_NOISE = re.compile(r'图片（[^）]*）')

_SCENE_RE = re.compile(r'演练场景\s*[：:]\s*')
_ROLES_RE = re.compile(r'人员分配\s*[：:]\s*')
_SCENE_STOPS = (re.compile(r'本班组人员分配'), re.compile(r'人员分配'),
                re.compile(r'演练实际情况'))
_ROLES_STOPS = (re.compile(r'演练实际情况'),)

# 人员分配：`A指挥通报组：郭俊迪 尹朝露 B组(倒闸操作)：…`。班组名可能只有「X组」
# （`A组：…`），也可能在括号里补业务名（`B组(倒闸操作)：…`），两者都收。
# 不加行首锚点：OCR 会把联系人连成 `尹朝露B组(倒闸操作)：张月明`，头就贴在汉字后面。
_LANE_HEAD = re.compile(
  r'([A-H])\s*([\u4e00-\u9fff]{0,10}组)'
  r'\s*(?:[（(]\s*([^）)]{1,12})\s*[）)])?\s*[：:]',
)
# 没有人员分配时的回退泳道（有序，先命中先归类；「漏水/积水」比设备词更具体，排前面）
_LANE_KEYWORDS = (
  ('指挥通报组', re.compile(r'值班长|指挥|通报|通知|上报')),
  ('现场处置', re.compile(r'漏水|积水|物资|机房门|阀门.{0,6}(关闭|隔离)')),
  ('供配电', re.compile(r'高压|市电|配电|UPS|馈线|母联|倒闸|断路器|低压')),
  ('制冷与末端', re.compile(r'制冷|空调|冷冻|冷通道|送风|回风|风机|温度|BA')),
  ('柴发', re.compile(r'柴发|发电机|频发|油机|油位')),
)
# 关键词泳道 → 已解析泳道名（同义词）
_LANE_SYNONYMS = {
  '指挥通报组': ('指挥', '通报', '调度'),
  '供配电': ('高压', '配电', '供电', '电气'),
  '制冷与末端': ('制冷', '冷冻', '空调', '暖通', '末端'),
  '柴发': ('柴发', '油机', '发电'),
  '现场处置': ('现场', '检查', '处置'),
}
_NOTIFY_RE = re.compile(r'通知\s*([^，。；,;]{1,24})')
# 值班长步骤里出现这些动词才算班组实操（纯「发现/通报」仍归指挥泳道）
_OP_RE = re.compile(r'操作|降低|恢复|调整|调取|查看|检查|摆放|关闭|清理|排气|启动|停止|切换|倒闸|投运')
# 「发现…报警」是值班长的监盘动作，即使后半句带「前往查看」也算指挥泳道
_ALARM_RE = re.compile(r'发现|报警')
_SPEAKER_PREFIXES = ('值班长通过动环监控', '值班长通过动环', '值班长通过环', '值班长操作BA',
                     '值班长通知', '值班长发现', '值班长检查', '值班长操作', '值班长')

_TRIM_CHARS = ' \t，,。.;；、:：-—'
_LABEL_KEEP = re.compile(r'[^\u4e00-\u9fffA-Za-z0-9，。、；：（）\-℃%/]')
_EXCERPT_LIMIT = 18


def _clean_line(line: str) -> str:
  """Markdown 表格行 → 单元格文字（相邻重复单元格折叠、分隔行丢弃）。"""
  if line.count('|') < 2:
    return line
  cells = [c.strip() for c in line.strip().strip('|').split('|')]
  cells = [c for c in cells if c]
  if not cells or all(_SEPARATOR_ROW.match(c) for c in cells):
    return ''
  collapsed: list[str] = []
  for cell in cells:
    if not collapsed or collapsed[-1] != cell:
      collapsed.append(cell)
  return ' '.join(collapsed)


def normalize_ocr_text(raw: str) -> str:
  """OCR 原始文本 → 逐行纯文字（无表格、无重复行）。"""
  if not raw:
    return ''
  text = _BR_TAG.sub('\n', raw)
  text = _BLOCK_TAG.sub('\n', text)
  text = _ANY_TAG.sub('', text)
  text = html.unescape(text).replace('\\|', '|').replace('**', '')
  out: list[str] = []
  for line in text.splitlines():
    line = _clean_line(' '.join(line.split()))
    if not line:
      continue
    if out and out[-1] == line:
      continue
    out.append(line)
  return '\n'.join(out)


def _minutes(run: str) -> int:
  """数字串 >2 位时取末两位：OCR 会把告警值粘到下一步时间戳前（25.9506分40秒）。"""
  return int(run[-2:]) if len(run) > 2 else int(run)


def extract_timeline(text: str) -> list[dict]:
  """按时间戳切出演练步骤 → `[{seconds, time, text}]`，按时间稳定排序。"""
  if not text:
    return []
  matches = list(TS.finditer(text))
  steps: list[dict] = []
  for i, match in enumerate(matches):
    end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
    body = text[match.end():end].split('【')[0]
    body = _IMAGE_NOISE.sub('', body).replace('演练照片', '')
    body = ' '.join(body.split()).strip(_TRIM_CHARS)
    if not body:
      continue
    mins = _minutes(match.group(1))
    secs = int(match.group(2))
    # OCR 会写出 `16分60秒` 这类越界秒数，进位到下一分钟
    mins, secs = mins + secs // 60, secs % 60
    steps.append({
      'index': i,
      'seconds': mins * 60 + secs,
      'time': f'{mins}分{secs}秒',
      'text': body,
    })
  steps.sort(key=lambda s: (s['seconds'], s['index']))
  return [{'seconds': s['seconds'], 'time': s['time'], 'text': s['text']}
          for s in steps]


def add_step_metrics(steps: list[dict]) -> list[dict]:
  """原地给每步补 `seq`（1 基）/`at`（累计秒，＝已有 `seconds`）/`delta`（本步耗时秒，
  ＝下一步 `seconds` − 本步；末步 None）。EOP 的时间限定多数是从事件开始的**累计**时间，
  也有按「用时」写的**单步**时间 —— 这里两种口径都给，由判定按 EOP 措辞各取所需。"""
  for i, step in enumerate(steps):
    step['seq'] = i + 1
    step['at'] = step['seconds']
    if i + 1 < len(steps):
      step['delta'] = steps[i + 1]['seconds'] - step['seconds']
    else:
      step['delta'] = None
  return steps


def _timeline_stats(steps: list[dict]) -> dict:
  """时间轴汇总：步数 / 首步累计 / 末步累计（＝全过程时长）/ 本步耗时的极值与均值。"""
  if not steps:
    return {'count': 0, 'first_seconds': 0, 'total_seconds': 0,
            'max_delta': None, 'min_delta': None, 'avg_delta': None}
  deltas = [s['delta'] for s in steps if s.get('delta') is not None]
  return {
    'count': len(steps),
    'first_seconds': steps[0]['seconds'],
    'total_seconds': steps[-1]['seconds'],
    'max_delta': max(deltas) if deltas else None,
    'min_delta': min(deltas) if deltas else None,
    'avg_delta': round(sum(deltas) / len(deltas), 1) if deltas else None,
  }


def _segment(text: str, start_re: re.Pattern, stops) -> str:
  match = start_re.search(text)
  if not match:
    return ''
  start = match.end()
  end = len(text)
  for stop in (*stops, TS):
    found = stop.search(text, start)
    if found:
      end = min(end, found.start())
  return ' '.join(text[start:end].split()).strip(_TRIM_CHARS)


def extract_header(text: str) -> dict:
  """演练场景 / 人员分配，取不到就是空串（不编造）。"""
  return {
    'scene': _segment(text, _SCENE_RE, _SCENE_STOPS),
    'roles': _segment(text, _ROLES_RE, _ROLES_STOPS),
  }


def parse_lanes(roles_text: str) -> list[dict]:
  """人员分配 → `[{key, name, members}]`；解析不到就是空列表（不编造班组）。

  紧跟在字母数字后的 `X组：` 不算班组头（`AB焊接通报组:` 这类 OCR 粘连），
  其余位置一律认，因为上一段的人名可能直接贴在下一个班组头前面。
  """
  text = roles_text or ''
  heads = [m for m in _LANE_HEAD.finditer(text)
           if m.start() == 0 or not re.match(r'[A-Za-z0-9]', text[m.start() - 1])]
  lanes: list[dict] = []
  for i, match in enumerate(heads):
    end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
    members = [m for m in re.split(r'[\s、，,/]+', text[match.end():end]) if m]
    base, paren = match.group(2), match.group(3)
    if paren:
      name = f'{paren}组'
    else:
      name = base if base != '组' else f'{match.group(1)}组'
    lanes.append({'key': match.group(1), 'name': name, 'members': members})
  return lanes


def _commander_name(lanes: list[dict]) -> str:
  """指挥泳道：名字带「指挥 / 通报」的那条，否则第一条；无人员分配时用默认名。"""
  for lane in lanes:
    if '指挥' in lane['name'] or '通报' in lane['name']:
      return lane['name']
  return lanes[0]['name'] if lanes else '指挥通报组'


def _resolve_lane_name(label: str, lanes: list[dict]) -> str:
  """关键词泳道 → 已解析泳道名（同义词），解析不到就用关键词名。"""
  synonyms = _LANE_SYNONYMS.get(label, ())
  for lane in lanes:
    if any(s in lane['name'] for s in synonyms):
      return lane['name']
  return label


def _strip_speaker(text: str) -> str:
  """去掉「值班长通过动环 / 值班长通知…」这类说话人前缀，留下动作本身。"""
  for prefix in _SPEAKER_PREFIXES:
    if text.startswith(prefix):
      return text[len(prefix):]
  return text


def _fallback_lane(text: str, lanes: list[dict]) -> str:
  """关键词回退：跳过指挥类关键词（`通知 / 上报` 会把每个指令都算进指挥泳道）。"""
  for label, pattern in _LANE_KEYWORDS[1:]:
    if pattern.search(text):
      return _resolve_lane_name(label, lanes)
  return ''


def _lane_of(text: str, lanes: list[dict], commander: str) -> str:
  """步骤归属泳道：被通知班组 → 该泳道；值班长 → 指挥泳道（只有明确的操作才落到
  班组泳道）；其余按出现的人名（前两字，容 OCR 错字），再按关键词。"""
  targets = _dispatch_targets(text, lanes, commander)
  if targets:
    return targets[0]
  if text.startswith('值班长'):
    rest = _strip_speaker(text)
    if not _ALARM_RE.search(rest) and _OP_RE.search(rest):
      lane = _fallback_lane(rest, lanes)
      if lane:
        return lane
    return commander
  head = text[:12]
  for lane in lanes:
    for name in lane['members']:
      if len(name) >= 2 and name[:2] in head:
        return lane['name']
  for lane in lanes:
    for name in lane['members']:
      if len(name) >= 2 and name[:2] in text:
        return lane['name']
  return _fallback_lane(text, lanes) or '其他'


def group_lanes(steps: list[dict], roles_text: str) -> tuple[str, list[tuple[str, list[dict]]]]:
  """步骤 → `(指挥泳道名, [(泳道名, 该泳道步骤)])`，指挥泳道排在最前。"""
  lanes = parse_lanes(roles_text)
  commander = _commander_name(lanes)
  buckets: dict[str, list[dict]] = {}
  for step in steps:
    buckets.setdefault(_lane_of(step['text'], lanes, commander), []).append(step)
  names = [commander] + [n for n in buckets if n != commander]
  return commander, [(n, buckets[n]) for n in names if buckets.get(n)]


def _excerpt(text: str, limit: int = _EXCERPT_LIMIT) -> str:
  """节点文案：去掉说话人与「通报值班长」，取第一个短句，超长在字数边界截断。"""
  body = re.sub(r'^[\u4e00-\u9fff]{2,4}(?:通报|汇报|通知)值班长', '', text)
  for prefix in _SPEAKER_PREFIXES:
    if body.startswith(prefix):
      body = body[len(prefix):]
      break
  body = re.sub(r'^(?:通报|汇报|通知)值班长', '', body).strip(_TRIM_CHARS) or text
  clause = re.split(r'[，。；;]', body)[0].strip(_TRIM_CHARS)
  body = clause or body
  if len(body) <= limit:
    return body
  cut = limit
  while cut < len(body) and body[cut].isdigit():
    cut += 1
  return body[:cut].rstrip(_TRIM_CHARS) + '…'


def _node_label(step: dict) -> str:
  # `#` 先换成「号」再清洗：`_LABEL_KEEP` 不含 `#`，先清洗会把它直接删掉（2#→2）
  text = _excerpt(step['text']).replace('#', '号')
  # 时间单独一行：节点宽度只由正文决定，否则每条泳道都被最长的时间前缀撑宽
  return f'{step["time"]}<br/>{_LABEL_KEEP.sub("", text)}'.strip()


def _dispatch_targets(text: str, lanes: list[dict], commander: str) -> list[str]:
  """「通知 X」→ 被通知的泳道名（去重、不含指挥泳道；「各小组」不算具体目标）。"""
  targets: list[str] = []
  for segment in _NOTIFY_RE.findall(text):
    if '各小组' in segment or '各组' in segment:
      continue
    for lane in lanes:
      if lane['name'] == commander:
        continue
      if lane['name'] in segment or any(
        len(m) >= 2 and m[:2] in segment for m in lane['members']
      ):
        targets.append(lane['name'])
  return list(dict.fromkeys(targets))


def _lane_label(name: str) -> str:
  """subgraph 标题：去掉括号（部分 mermaid 版本会在 `[]` 里解析出错）。"""
  return re.sub(r'[（）()]', '', _LABEL_KEEP.sub('', name)).strip() or '其他'


def build_flow_mermaid(steps: list[dict], roles_text: str = '') -> str:
  """按人员分配的班组画泳道图：**一条泳道一竖列**，泳道之间左右并排。

  subgraph 内 `direction TB` 让每条泳道竖排成一列，列内按时间串联。

  **刻意不画跨泳道可见连线**：mermaid 的 dagre 是全局 rank 网格，subgraph 一旦有跨边界的
  边，内部的 `direction` 会被忽略、几条泳道被拉成一条长链 —— 实测 29 步的记录排出 12 列
  rank，「各泳道末节点汇到时间最后一步」那条边穿过 5 个空列，每个泳道框都被撑满整张图
  （内容只占框的一小块）。

  但**完全不相连的 subgraph 也不会并排**：实测 4 条泳道全部落在 x=8 竖着叠成一列
  （346×5227）。所以泳道之间补一条**不可见连线**（`L0 ~~~ L1` …）：它不画线、不承载语义，
  只把几条泳道约束到同一行的 rank 上（加上后 1486×1939，各道 x=8/388/768/1148 并排）。
  并行关系由「一列一条泳道」的版式表达（各列顶部并不对齐），先后顺序由列内连线与时间戳
  表达。少于 2 步返回空串。
  """
  if len(steps) < 2:
    return ''
  _, groups = group_lanes(steps, roles_text)

  lines = ['```mermaid', 'flowchart LR']
  for li, (lane_name, lane_steps) in enumerate(groups):
    lines.append(f'  subgraph L{li}[{_lane_label(lane_name)}]')
    lines.append('    direction TB')
    for si, step in enumerate(lane_steps):
      lines.append(f'    {chr(65 + li)}{si}["{_node_label(step)}"]')
    for si in range(len(lane_steps) - 1):
      lines.append(f'    {chr(65 + li)}{si} --> {chr(65 + li)}{si + 1}')
    lines.append('  end')
  # 泳道之间补隐形连线：没有它，mermaid 会把几条互不相连的竖排 subgraph 叠成一列
  for li in range(len(groups) - 1):
    lines.append(f'  L{li} ~~~ L{li + 1}')
  lines.append('```')
  return '\n'.join(lines)


def analyze_record(raw: str) -> dict:
  """OCR 原始文本 → `{scene, roles, steps, lanes, flow}`。

  `lanes` 是分车道结构化步骤（页面用它画「分车道时间线」，避免把高度差大的多条车道
  塞进一张泳道图导致大片空白）；`flow` 仍是整张泳道图（聊天/附件/全屏查看用）。
  `timeline_stats` 是全过程时长与步耗时汇总（给判定做超时判断用）。
  """
  text = normalize_ocr_text(raw)
  # 先补 seq/at/delta 再分组，泳道里的步骤才带得上这些键
  steps = add_step_metrics(extract_timeline(text))
  header = extract_header(text)
  _, groups = group_lanes(steps, header['roles'])
  lanes = [{'name': name, 'steps': lane_steps} for name, lane_steps in groups]
  return {
    'scene': header['scene'],
    'roles': header['roles'],
    'steps': steps,
    'lanes': lanes,
    'flow': build_flow_mermaid(steps, header['roles']),
    'timeline_stats': _timeline_stats(steps),
  }

"""变更通知报文解析（纯函数：不连库、不调外部接口）。

现场有四种报文形态（实测样本 2026-11-19）：

- `lf`      【变更开始-通知】 / 【变更完成-通知】，竖线或换行分隔 `标签：值`（廊坊）
- `ph`      【…维护通告】开启 / `-开始` / 【…变更通告】-开启，编号列表 `1、标题：…`（平湖AB）
- `bracket` 【变更开始-<标题>】 / 【变更结束-…】，方括号字段 `【变更单号】值`（佛山/重庆）
- `repair`  【维修变更开始】 / 【维修变更完成】，方括号字段（惠州）

**统计纳入 `lf` / `bracket` / `ph`**（见 `_ENABLED_FORMATS`）；`ph` 的「位置」是楼宇字母
（Y楼/Z楼/W），按 `_DC_ALIASES` 映射成数据中心代号。`repair`（维修变更）不算正常变更，
`parse_message` 直接丢弃。
**必须保留【】**：`incident_stats.feishu._norm` 会把【】改成 []，这里不能复用。
噪声（值班名单/天气/@_all 说明）不含变更头，`detect_format` 返回空 → 丢弃。
"""

import re
import unicodedata
from datetime import datetime
from zoneinfo import ZoneInfo

_TZ = ZoneInfo('Asia/Shanghai')

_HTML_BREAK = re.compile(r'<\s*br\s*/?\s*>|<\s*/\s*(?:p|div)\s*>', re.I)
_HTML_TAG = re.compile(r'<[^>]+>')

_RE_PH = re.compile(r'【[^】]{0,30}(?:维护通告|变更通告)】\s*[-－]?\s*(开启|开始|完成|结束|关闭)')
_RE_LF = re.compile(r'【\s*变更\s*(开始|完成|结束)\s*-?\s*通知\s*】')
_RE_REPAIR = re.compile(
  r'【\s*维修变更\s*(开始|完成|结束)\s*】\s*[|｜]?\s*([^【\n|｜]{0,60})')
_RE_BRACKET = re.compile(r'【\s*变更\s*(开始|完成|结束)\s*[-—－]\s*([^】]{1,60})】')

_BRACKET_FIELD = re.compile(r'^【([^】]{1,20})】\s*[:：]?\s*(.*)$')
_NUMBERED = re.compile(r'^\d{1,2}\s*[、.．]\s*(.+)$')
_PLAIN = re.compile(r'^([^：:]{1,10})\s*[：:]\s*(.*)$')

# 标签 → 内部字段键（同义标签按「文本里先出现的先认」合并；负责人除外，见
# `_ACCUMULATE_KEYS` —— 多个负责人标签要累加，否则先出现的电话串会占住整列）
_ALIASES = {
  '变更工单号': 'ticket', '变更工单': 'ticket', '变更单号': 'ticket',
  '故障维修单号': 'ticket',
  '变更地点': 'dc', '位置': 'dc',
  '标题': 'title', '变更内容': 'title', '内容': 'title',
  '变更等级': 'level', '等级': 'level',
  '变更影响范围': 'scope', '变更范围': 'scope', '影响范围': 'scope', '影响': 'scope',
  # 负责人性质标签优先；值班工程师只作兜底（口径：负责人优先、值班兜底）
  '现场负责人': 'contact', '现场联系人': 'contact', '人员': 'contact',
  '相关负责人': 'contact', '变更负责人': 'contact',
  '值班工程师': 'contact_fallback', '值班人员': 'contact_fallback',
  '计划开始时间': 'pstart', '预计开始时间': 'pstart',
  '计划结束时间': 'pend', '预计完成时间': 'pend', '预计结束时间': 'pend',
  '开始时间': 'astart', '结束时间': 'aend',
  '变更描述': 'reason', '变更开始描述': 'reason', '操作原因': 'reason',
  '原由': 'reason', '变更原因': 'reason',
  '进展情况': 'progress', '进度': 'progress',
  '相关告警': 'alarm',
  '时间': 'window',
}

# 已知噪声标签：不取值、也不并入上一条字段（否则会污染「现场负责人」）
_IGNORED_LABELS = frozenset({'发送范围'})

# 需要累加而非首见即定的字段键：负责人分「负责人 / 值班兜底」两档，各自可来自多个标签
_ACCUMULATE_KEYS = frozenset({'contact', 'contact_fallback'})

# 统计正式变更报文：廊坊式 `lf`（`【变更开始-通知】` + 标签：值）、方括号式 `bracket`
# （佛山/重庆 `【变更开始-<标题>】` + 【标签】值）与平湖编号列表 `ph`（`【…维护通告】开启`，
# 无工单号，位置按 `_DC_ALIASES` 映射数据中心）。`repair`（惠州维修变更，无计划时间）
# 不算正常变更，按用户口径丢弃。旧格式的解析代码与正则保留，改回本集合即可回退。
_ENABLED_FORMATS = frozenset({'lf', 'bracket', 'ph'})

# 未登记标签里命中这些词才值得交给模型兜底；否则仍按续行处理
# （`5F-3#电力室:` 这类影响范围的续行没有语义提示词，不能被当成缺失字段）。
_HINT_RE = re.compile(r'时间|描述|负责人|联系人|地点|位置|内容|等级|范围|工单|单号|标题|'
                      r'原因|进展|进度|告警|人员')

# 负责人字段清洗：先合并「秦   昊」这类大字间距，再按分隔符切，剥掉前缀标签与电话/工号。
# 现场把电话、姓名、多个负责人标签堆在同一字段里，故要能从中只挑出姓名。
_CJK_GAP = re.compile(r'(?<=[\u4e00-\u9fa5])[\s\u00a0]{2,}(?=[\u4e00-\u9fa5])')
_NAME_SEP = re.compile(r'[、，,；;|/\\\s\t\n：:（）()【】\[\]]+')
_LABEL_PREFIX = re.compile(
  r'^(?:现场负责人|变更负责人|相关负责人|值班工程师|现场联系人|联系方式|'
  r'负责人|联系人|工程师|电话|手机|值班|人员|姓名)+')
_NAME_OK = re.compile(r'^[\u4e00-\u9fa5·]{2,8}$')
_DIGIT_PUNCT = re.compile(r'[\d\-—+_.]+')

_DT_CN = re.compile(
  r'(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日\s*'
  r'(\d{1,2})\s*[:：]\s*(\d{1,2})(?:\s*[:：]\s*(\d{1,2}))?')
_DT_DASH = re.compile(
  r'(\d{4})\s*[-/.]\s*(\d{1,2})\s*[-/.]\s*(\d{1,2})\s*日?[\s\-T]+'
  r'(\d{1,2})\s*[:：]\s*(\d{1,2})(?:\s*[:：]\s*(\d{1,2}))?')
_RANGE_CN = re.compile(r'(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日')
_RANGE_DASH = re.compile(r'(\d{4})\s*[-/.]\s*(\d{1,2})\s*[-/.]\s*(\d{1,2})')
_TIME_PAIR = re.compile(r'(\d{1,2})\s*[:：]\s*(\d{1,2})')

_DC_TOKEN = re.compile(r'^(?:[AC]\d{1,2}|B\d{1,2}M\d{1,2})$')
_DC_WITH_SPACE = re.compile(r'([A-Za-z]\d{1,2}(?:M\d{1,2})?)\s*数据中心')
# 平湖 ET15 编号列表按楼宇字母写位置（2、位置：Y楼），映射到数据中心代号
_DC_ALIASES = {'Y楼': 'A1', 'Z楼': 'A2', 'W楼': 'M3M4',
               'Y': 'A1', 'Z': 'A2', 'W': 'M3M4'}


def normalize_text(text: str) -> str:
  """HTML 断行 → `\\n`、剥标签、NBSP 归空格、NFKC（**保留【】**）。"""
  value = (text or '').replace('\u00a0', ' ')
  value = _HTML_BREAK.sub('\n', value)
  value = _HTML_TAG.sub('', value)
  return unicodedata.normalize('NFKC', value)


def _status_of(verb: str) -> str:
  return '已完成' if verb in ('完成', '结束', '关闭') else '进行中'


def _is_header_line(line: str) -> bool:
  return any(p.search(line) for p in (_RE_PH, _RE_LF, _RE_REPAIR, _RE_BRACKET))


def detect_format(text: str) -> tuple[str, str, str]:
  """→ `(fmt, 头里带的标题, 执行状态)`；非变更报文返回 `('', '', '')`。"""
  m = _RE_PH.search(text)
  if m:
    return 'ph', '', _status_of(m.group(1))
  m = _RE_LF.search(text)
  if m:
    return 'lf', '', _status_of(m.group(1))
  m = _RE_REPAIR.search(text)
  if m:
    return 'repair', (m.group(2) or '').strip(), _status_of(m.group(1))
  m = _RE_BRACKET.search(text)
  if m:
    return 'bracket', (m.group(2) or '').strip(), _status_of(m.group(1))
  return '', '', ''


def _field_key(label: str) -> str:
  return _ALIASES.get(label.strip(), '')


def _classify_line(line: str) -> tuple[str, str, str]:
  """→ `('field', key, value)` / `('unknown', label, value)` / `('cont', '', text)` /
  `('skip', '', '')`。

  `unknown` = 未登记但带语义提示词的标签（如 `实际开始时间`）→ 交给模型兜底，
  **不并入上一条字段**（原先会污染「现场负责人」）。
  """
  m = _BRACKET_FIELD.match(line)
  if m:
    label = m.group(1).strip()
    if label in _IGNORED_LABELS:
      return 'skip', '', ''
    if _field_key(label):
      return 'field', _field_key(label), m.group(2).strip()
    if _HINT_RE.search(label):
      return 'unknown', label, m.group(2).strip()
  m = _NUMBERED.match(line)
  if m:
    inner = m.group(1).strip()
    for pattern in (_BRACKET_FIELD, _PLAIN):
      m2 = pattern.match(inner)
      if m2 and _field_key(m2.group(1)):
        return 'field', _field_key(m2.group(1)), m2.group(2).strip()
    m2 = _BRACKET_FIELD.match(inner) or _PLAIN.match(inner)
    label = m2.group(1).strip() if m2 else ''
    if label in _IGNORED_LABELS:
      return 'skip', '', ''
    if label and _HINT_RE.search(label):
      return 'unknown', label, m2.group(2).strip()
    return 'skip', '', ''
  m = _PLAIN.match(line)
  if m:
    label = m.group(1).strip()
    if label in _IGNORED_LABELS:
      return 'skip', '', ''
    if _field_key(label):
      return 'field', _field_key(label), m.group(2).strip()
    if _HINT_RE.search(label):
      return 'unknown', label, m.group(2).strip()
  return 'cont', '', line


def _extract_fields(norm: str) -> tuple[dict[str, str], list[str]]:
  """逐行取 `标签：值`，无标签非空行作为上一条值的续行（首见标签优先）。

  负责人两类键（`contact` / `contact_fallback`）**累加**而非首见即定：现场常把
  值班电话、值班工程师、变更负责人堆在多个标签里，先出现的可能是电话串。
  返回 `(fields, unknown_labels)`；未知语义标签只记名字，值交给模型兜底。
  """
  fields: dict[str, str] = {}
  unknown: list[str] = []
  last_key = ''
  for raw in norm.replace('|', '\n').split('\n'):
    line = raw.strip()
    if not line or _is_header_line(line):
      continue
    kind, key, value = _classify_line(line)
    if kind == 'field':
      if key in _ACCUMULATE_KEYS:
        prev = fields.get(key, '')
        if value and value not in prev:
          fields[key] = f'{prev}\n{value}' if prev else value
        elif not prev:
          fields[key] = value
      elif key not in fields or (not fields[key] and value):
        fields[key] = value
      last_key = key
    elif kind == 'unknown':
      if key not in unknown:
        unknown.append(key)
      last_key = ''
    elif kind == 'cont':
      if last_key and line:
        fields[last_key] = f'{fields.get(last_key, "")}\n{line}'.strip('\n')
  return fields, unknown


def contact_names(value: str) -> list[str]:
  """负责人字段 → 姓名列表（去重）。剥号码/工号/标签前缀，认不出就返回 []。

  现场写法示例：`值班电话：023-68174134\n值班工程师：朱传佳：18600886680\n秦   昊：16636732002`
  → `['朱传佳', '秦昊']`。
  """
  names: list[str] = []
  text = _CJK_GAP.sub('', value or '')
  for part in _NAME_SEP.split(text):
    name = _LABEL_PREFIX.sub('', part.strip())
    name = _DIGIT_PUNCT.sub('', name).strip()
    if _NAME_OK.match(name) and name not in names:
      names.append(name)
  return names


def _build(y, mo, d, h, mi, s) -> datetime | None:
  try:
    return datetime(int(y), int(mo), int(d), int(h), int(mi), int(s or 0), tzinfo=_TZ)
  except (TypeError, ValueError):
    return None


def parse_dt(value: str) -> datetime | None:
  """`2026-11-19 08:30` / `2026-11-19-07:30` / `2026年11月17日 13:00` → datetime。

  只有日期没有时分返回 None（不拿 00:00 硬凑）。
  """
  text = (value or '').strip()
  if not text:
    return None
  m = _DT_CN.search(text) or _DT_DASH.search(text)
  if not m:
    return None
  groups = m.groups()
  return _build(groups[0], groups[1], groups[2], groups[3], groups[4],
                groups[5] if len(groups) > 5 else None)


def parse_range(value: str) -> tuple[datetime | None, datetime | None]:
  """平湖 `时间：2026年11月19日 8:40-22:00` → (开始, 结束)。"""
  text = (value or '').strip()
  if not text:
    return None, None
  m = _RANGE_CN.search(text) or _RANGE_DASH.search(text)
  if not m:
    return None, None
  y, mo, d = m.groups()
  pairs = _TIME_PAIR.findall(text[m.end():])
  if not pairs:
    return None, None
  start = _build(y, mo, d, pairs[0][0], pairs[0][1], 0)
  end = _build(y, mo, d, pairs[1][0], pairs[1][1], 0) if len(pairs) > 1 else None
  return start, end


def _dc_of(ticket_no: str, dc_raw: str) -> str:
  """工单号里的数据中心代号优先（`RZKJ-LF-A6-…` → A6），再退回地点文本。"""
  for part in (ticket_no or '').split('-'):
    token = part.strip().upper()
    if _DC_TOKEN.match(token):
      return token
  m = _DC_WITH_SPACE.search(dc_raw or '')
  if m:
    return m.group(1).upper()
  plain = (dc_raw or '').strip()
  return _DC_ALIASES.get(plain, plain if 0 < len(plain) <= 8 else '')


def change_type(level_raw: str) -> str:
  """原等级 → 变更类型分桶（无依据不推断，空即「未标注」）。"""
  text = level_raw or ''
  if '紧急' in text or '临时' in text:
    return '临时新增紧急变更'
  if '高' in text:
    return '高风险变更'
  if '中' in text:
    return '中风险变更'
  if '低' in text:
    return '低风险变更'
  return '未标注'


def parse_message(text: str, create_time: int, park: str = '', sender: str = '') -> dict | None:
  """一条群消息 → 变更事件 dict；不是变更通知返回 None。"""
  norm = normalize_text(text)
  fmt, header_title, status = detect_format(norm)
  if fmt not in _ENABLED_FORMATS:
    return None
  fields, unknown = _extract_fields(norm)

  ticket_no = fields.get('ticket', '')
  title = fields.get('title', '') or header_title
  level_raw = fields.get('level', '')

  planned_start = parse_dt(fields.get('pstart', ''))
  planned_end = parse_dt(fields.get('pend', ''))
  if planned_start is None and planned_end is None and fields.get('window'):
    planned_start, planned_end = parse_range(fields['window'])

  # 「取消」只可能改写未完成的单；已完成的通知不因描述里出现该词被翻转
  if status == '进行中' and '取消' in norm[:200]:
    status = '已取消'

  # 现场负责人 = 负责人性质标签优先，值班工程师兜底；只保留姓名（电话/工号剥掉）
  contact = contact_names(fields.get('contact', '')) or \
            contact_names(fields.get('contact_fallback', ''))

  return {
    'park': park,
    'dc': _dc_of(ticket_no, fields.get('dc', '')),
    'title': title.strip(),
    'status': status,
    'type': change_type(level_raw),
    'level_raw': level_raw.strip(),
    'ticket_no': ticket_no.strip(),
    'planned_start': planned_start,
    'planned_end': planned_end,
    'actual_start': parse_dt(fields.get('astart', '')),
    'actual_end': parse_dt(fields.get('aend', '')),
    'reason': fields.get('reason', '').strip(),
    'scope': fields.get('scope', '').strip(),
    'contact': '、'.join(contact),
    'progress': fields.get('progress', '').strip(),
    'sender': (sender or '').strip(),
    'msg_time': int(create_time or 0),
    'fmt': fmt,
    # 内部键：未识别的语义标签 + 归一后的原文，供 `extract.py` 模型兜底；
    # `build` 只读声明字段，这两个键不进任何输出列。
    'unknown': unknown,
    '_text': norm,
  }

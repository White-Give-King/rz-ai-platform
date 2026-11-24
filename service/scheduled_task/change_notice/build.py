"""事件配对 / 去重 / 分桶 / 表格行渲染（纯函数，不连库）。

配对键：有工单号 → `(园区, 工单号)`；平湖编号通告无工单号 → `(园区, 数据中心, 标题, 日期)`。
**一个工单可对应多次变更**（如 A18 同一单跨班次做两轮），故同一配对键内再按**变更窗口**
（计划开始，缺失时用实际开始）分条：窗口不同各计一条，窗口相同的重发才计一条；
完成通知优先配回窗口一致的开始那条，配不上再并入该工单最近一条未完成的通知。
合并口径：文本取首个非空、时间取最早、状态取最高、消息时间取最早；
状态优先级：已完成 > 已取消 > 进行中。
"""

from datetime import datetime
from zoneinfo import ZoneInfo

from service.scheduled_task.change_notice import parse

_TZ = ZoneInfo('Asia/Shanghai')

COLUMNS = (
  '园区', '数据中心', '变更类型', '维护事项', '执行状态',
  '计划开始', '计划结束', '实际开始', '实际结束',
  '工单号', '变更等级', '现场负责人', '现场负责人等级', '消息时间',
)

TYPE_ORDER = ('临时新增紧急变更', '高风险变更', '中风险变更', '低风险变更', '未标注')
STATUS_ORDER = ('已完成', '进行中', '已取消')
MATCH_ORDER = ('符合', '不符合', '无法判定')
# 页面专用隐藏行键：不写进 COLUMNS，对话 markdown / xlsx 仍 14 列
MATCH_KEY = '__match'
# 页面展示 / 页面导出用的可见列名（紧跟现场负责人等级）
MATCH_COLUMN = '等级匹配'
PAGE_COLUMNS = (*COLUMNS[:COLUMNS.index('现场负责人等级') + 1], MATCH_COLUMN,
                *COLUMNS[COLUMNS.index('现场负责人等级') + 1:])
_STATUS_RANK = {'已完成': 3, '已取消': 2, '进行中': 1}
# 变更类型 → 现场负责人最低职级（中/高/紧急均要求中级；未标注无依据不判定）
_REQUIRED_RANK = {'低风险变更': 1, '中风险变更': 2,
                  '高风险变更': 2, '临时新增紧急变更': 2}
_LEVEL_RANK = {'初级': 1, '中级': 2, '高级': 3}
_TEXT_FIELDS = ('dc', 'title', 'level_raw', 'ticket_no', 'reason', 'scope', 'contact', 'fmt')
_TIME_FIELDS = ('planned_start', 'planned_end', 'actual_start', 'actual_end')


def _date_of(ts: int) -> str:
  return datetime.fromtimestamp(int(ts or 0), _TZ).strftime('%Y-%m-%d')


def _base_key(ev: dict) -> tuple:
  if ev.get('ticket_no'):
    return ('ticket', ev.get('park', ''), ev['ticket_no'])
  return ('adhoc', ev.get('park', ''), ev.get('dc', ''), ev.get('title', ''),
          _date_of(ev.get('msg_time', 0)))


def _anchor(ev: dict):
  """变更窗口起点：计划开始优先，缺计划时用实际开始（A18 只报「开始时间」）。
  归一到一个分钟抵消秒级差异；两者都缺失返回 None（如平湖编号通告）。"""
  value = ev.get('planned_start') or ev.get('actual_start')
  if isinstance(value, datetime):
    return value.replace(second=0, microsecond=0)
  return None


def _pair_bucket(notices: list[dict]) -> list[dict]:
  """同一工单（或无工单同标题）内的多条通知 → 按变更窗口拆成若干条事件。

  开始/取消：同窗口并入已有事件（重发去重），否则新建。
  完成：先配同窗口；配不上并入最近一条未完成事件；都没有则自己成条。
  """
  groups: list[dict] = []
  for ev in sorted(notices, key=lambda e: e.get('msg_time', 0)):
    anchor = _anchor(ev)
    target = next((g for g in groups if g['anchor'] == anchor), None)
    if target is None and ev.get('status') == '已完成':
      target = next((g for g in reversed(groups)
                     if not any(n.get('status') == '已完成' for n in g['notices'])),
                    None)
    if target is None:
      groups.append({'anchor': anchor, 'notices': [ev]})
    else:
      target['notices'].append(ev)
  return [_merge(group['notices']) for group in groups]


def _merge(group: list[dict]) -> dict:
  """同一次变更的多条通知合并成一条：文本取首个非空、时间取最早、状态取最高、消息时间取最早。"""
  group = sorted(group, key=lambda e: e.get('msg_time', 0))
  merged = dict(group[0])
  for key in _TEXT_FIELDS:
    for ev in group:
      if ev.get(key):
        merged[key] = ev[key]
        break
  for key in _TIME_FIELDS:
    stamps = [ev.get(key) for ev in group if ev.get(key)]
    merged[key] = min(stamps) if stamps else None
  merged['status'] = max((ev.get('status', '进行中') for ev in group),
                         key=lambda s: _STATUS_RANK.get(s, 0))
  merged['msg_time'] = min(ev.get('msg_time', 0) for ev in group)
  merged['type'] = parse.change_type(merged.get('level_raw', ''))
  # 逐条通知口径（内部键，不进任何输出列）：开始/完成各自的消息时刻，供合规判定用。
  # 已完成=完成通知；进行中/已取消=发过开始通知（`status` 反推，无需改 parse）。
  starts = [e for e in group if e.get('status') != '已完成']
  ends = [e for e in group if e.get('status') == '已完成']
  merged['has_start'] = bool(starts)
  merged['start_msg_time'] = min((e.get('msg_time', 0) for e in starts), default=None)
  merged['has_end'] = bool(ends)
  merged['end_msg_time'] = min((e.get('msg_time', 0) for e in ends), default=None)
  return merged


def build_events(parsed: list[dict]) -> list[dict]:
  buckets: dict[tuple, list[dict]] = {}
  for ev in parsed:
    buckets.setdefault(_base_key(ev), []).append(ev)
  events: list[dict] = []
  for notices in buckets.values():
    events.extend(_pair_bucket(notices))
  events.sort(key=lambda e: (e.get('msg_time', 0), e.get('park', ''), e.get('dc', '')))
  return events


def _fmt_time(value, with_seconds: bool = False) -> str:
  if not isinstance(value, datetime):
    return ''
  return value.strftime('%Y-%m-%d %H:%M:%S' if with_seconds else '%Y-%m-%d %H:%M')


def match_of(type_: str, contact_level: str) -> str:
  """变更等级 vs 现场负责人等级是否匹配（取最高职级；无等级/无档案不判定）。"""
  required = _REQUIRED_RANK.get(type_ or '')
  if not required:
    return '无法判定'
  text = (contact_level or '').strip()
  if not text or text == '无评级':
    return '无法判定'
  ranks = [_LEVEL_RANK[t] for t in text.split('、') if t in _LEVEL_RANK]
  if not ranks:
    return '无法判定'
  return '符合' if max(ranks) >= required else '不符合'


def to_rows(events: list[dict], *, with_match: bool = False) -> list[dict]:
  rows: list[dict] = []
  for ev in events:
    row = {
      '园区': ev.get('park', ''),
      '数据中心': ev.get('dc', ''),
      '变更类型': ev.get('type', '未标注'),
      '维护事项': ev.get('title', ''),
      '执行状态': ev.get('status', ''),
      '计划开始': _fmt_time(ev.get('planned_start')),
      '计划结束': _fmt_time(ev.get('planned_end')),
      '实际开始': _fmt_time(ev.get('actual_start')),
      '实际结束': _fmt_time(ev.get('actual_end')),
      '工单号': ev.get('ticket_no', ''),
      '变更等级': ev.get('level_raw', ''),
      '现场负责人': ev.get('contact', ''),
      '现场负责人等级': ev.get('contact_level', ''),
      '消息时间': _fmt_time(
        datetime.fromtimestamp(int(ev.get('msg_time', 0) or 0), _TZ), with_seconds=True),
    }
    if with_match:
      row[MATCH_KEY] = match_of(row['变更类型'], row['现场负责人等级'])
    rows.append(row)
  return rows


def overview(events: list[dict]) -> list[dict]:
  """每园区 ×（各变更类型计数）+（各执行状态计数）+（等级匹配计数）+ 合计。"""
  parks: dict[str, dict] = {}
  for ev in events:
    park = ev.get('park', '')
    slot = parks.setdefault(park, {
      'park': park, 'total': 0,
      'by_type': {t: 0 for t in TYPE_ORDER},
      'by_status': {s: 0 for s in STATUS_ORDER},
      'by_match': {m: 0 for m in MATCH_ORDER},
    })
    slot['total'] += 1
    kind = ev.get('type', '未标注')
    slot['by_type'][kind] = slot['by_type'].get(kind, 0) + 1
    match = match_of(kind, ev.get('contact_level', ''))
    slot['by_match'][match] = slot['by_match'].get(match, 0) + 1
    status = ev.get('status', '')
    slot['by_status'][status] = slot['by_status'].get(status, 0) + 1
  return [parks[park] for park in sorted(parks)]


def summary_markdown(overview_rows: list[dict], count_unit: str = '条') -> str:
  """把 `overview()` 的每园区计数渲染成一张小 Markdown 表（纯标准库、不连库）。

  列：园区 / 合计 / 变更类型分布 / 执行状态分布。空列表返回空串。
  供对话工具在 records（>20 条）与 inline 模式给服务端汇总，模型原样输出即可，
  不必再去拉原始群消息自己整理。
  """
  if not overview_rows:
    return ''
  lines = ['| 园区 | 合计 | 变更类型 | 执行状态 |', '|---|---|---|---|']
  for row in overview_rows:
    by_type = '、'.join(f'{k} {v}' for k, v in row.get('by_type', {}).items() if v) or '—'
    by_status = '、'.join(f'{k} {v}' for k, v in row.get('by_status', {}).items() if v) or '—'
    lines.append(f"| {row.get('park', '')} | {row.get('total', 0)}{count_unit} | "
                 f'{by_type} | {by_status} |')
  return '\n'.join(lines)

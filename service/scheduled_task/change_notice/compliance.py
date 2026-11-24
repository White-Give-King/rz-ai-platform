"""变更通知合规判定（纯函数：不连库、不调外部）。

**加规则只往 `RULES` 追加一项**：每条规则声明 key / 标题 / 判定函数，框架负责逐事件跑、
分组统计、拼不合规明细。判定函数返回 `(结果, 明细)`：
- `True`  = 合规
- `False` = 不合规（进附录明细清单）
- `None` = 无法判定（数据不足，不臆造、不计入不合规）

输入是 `build.build_events` 产出的**已配对事件**，依赖它带出的逐条通知口径：
`has_start` / `start_msg_time`（开始通知）、`has_end` / `end_msg_time`（完成通知），
以及 `planned_start` / `actual_end` 等时间字段（datetime，Asia/Shanghai）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Optional
from zoneinfo import ZoneInfo

_TZ = ZoneInfo('Asia/Shanghai')

Verdict = Optional[bool]   # True 合规 / False 不合规 / None 无法判定


@dataclass(frozen=True)
class Cfg:
  late_grace_minutes: int = 0        # 规则① 宽限（计划开始后多少分钟内发仍算合规）
  completion_diff_minutes: int = 30  # 规则③ 完成通知与实际完成的允许偏差
  tz: ZoneInfo = _TZ


@dataclass(frozen=True)
class Rule:
  key: str
  title: str
  fn: Callable[[dict, Cfg], tuple[Verdict, str]]


# ---- 时间格式化 ------------------------------------------------------------

def _fmt_dt(value) -> str:
  return value.strftime('%Y-%m-%d %H:%M') if isinstance(value, datetime) else ''


def _fmt_ts(epoch) -> str:
  if not epoch:
    return ''
  return datetime.fromtimestamp(int(epoch), _TZ).strftime('%Y-%m-%d %H:%M')


def _fmt_delta(minutes: float) -> str:
  m = int(round(abs(minutes)))
  if m < 60:
    return f'{m} 分钟'
  hours, mins = divmod(m, 60)
  return f'{hours} 小时' + (f'{mins} 分钟' if mins else '')


def _ts(value) -> int:
  if isinstance(value, datetime):
    return int(value.timestamp())
  return int(value or 0)


# ---- 三条初始规则 ----------------------------------------------------------

def _late_start(event: dict, cfg: Cfg) -> tuple[Verdict, str]:
  """① 开始通知发送时间晚于计划开始时间。"""
  planned = event.get('planned_start')
  sent = event.get('start_msg_time')
  if not isinstance(planned, datetime) or not sent:
    return None, '缺少计划开始时间或开始通知时间，无法判定'
  delay = (int(sent) - _ts(planned)) / 60.0
  if delay > cfg.late_grace_minutes:
    return False, (f'开始通知发送于 {_fmt_ts(sent)}，晚于计划开始 '
                   f'{_fmt_dt(planned)} 共 {_fmt_delta(delay)}')
  return True, ''


def _missing_start(event: dict, cfg: Cfg) -> tuple[Verdict, str]:
  """② 当日未发送变更开始（只有完成通知，没有开始通知）。"""
  if event.get('has_start'):
    return True, ''
  return False, '当日未发送变更开始通知'


def _completion_mismatch(event: dict, cfg: Cfg) -> tuple[Verdict, str]:
  """③ 完成通知发送时间与实际完成时间不符（差值超过阈值）。"""
  sent = event.get('end_msg_time')
  actual = event.get('actual_end')
  if not sent or not isinstance(actual, datetime):
    return None, '缺少完成通知时间或实际完成时间，无法判定'
  diff = (int(sent) - _ts(actual)) / 60.0
  if abs(diff) > cfg.completion_diff_minutes:
    return False, (f'完成通知发送于 {_fmt_ts(sent)}，实际完成 {_fmt_dt(actual)}，'
                   f'相差 {_fmt_delta(diff)}')
  return True, ''


RULES: tuple[Rule, ...] = (
  Rule('late_start_notice', '发送时间晚于计划开始时间', _late_start),
  Rule('missing_start_notice', '当日未发送变更开始', _missing_start),
  Rule('completion_time_mismatch', '完成通知时间与实际完成时间不符', _completion_mismatch),
)


# ---- 判定引擎 --------------------------------------------------------------

def _detail_row(event: dict, rule: Rule, detail: str) -> dict:
  return {
    '园区': event.get('park', ''),
    '数据中心': event.get('dc', ''),
    '工单号': event.get('ticket_no', ''),
    '维护事项': event.get('title', ''),
    '规则': rule.title,
    '明细': detail,
    '计划开始': _fmt_dt(event.get('planned_start')),
    '实际结束': _fmt_dt(event.get('actual_end')),
    '消息时间': _fmt_ts(event.get('end_msg_time') or event.get('start_msg_time')
                       or event.get('msg_time')),
  }


def evaluate_events(events: list[dict], cfg: Cfg | None = None) -> dict:
  """逐事件跑全部规则 → 统计 + 不合规明细。

  合规率（事件级）＝无任何不合规规则的事件数 / 总事件数；另按规则各给
  `violations` / `undetermined` / `compliant`。**加规则只往 `RULES` 追加**。
  """
  cfg = cfg or Cfg()
  stats = {r.key: {'key': r.key, 'title': r.title, 'violations': 0,
                   'undetermined': 0, 'compliant': 0} for r in RULES}
  by_park: dict[str, dict] = {}
  detail_rows: list[dict] = []
  compliant_events = 0

  for event in events:
    violated = False
    for rule in RULES:
      verdict, detail = rule.fn(event, cfg)
      slot = stats[rule.key]
      if verdict is None:
        slot['undetermined'] += 1
      elif verdict:
        slot['compliant'] += 1
      else:
        slot['violations'] += 1
        violated = True
        detail_rows.append(_detail_row(event, rule, detail))
    park = event.get('park', '') or '未知'
    slot = by_park.setdefault(park, {'park': park, 'compliant': 0, 'violation': 0})
    if violated:
      slot['violation'] += 1
    else:
      slot['compliant'] += 1
      compliant_events += 1

  total = len(events)
  return {
    'total': total,
    'rules': list(stats.values()),
    'by_park': sorted(by_park.values(), key=lambda p: p['compliant'] + p['violation'],
                      reverse=True),
    'detail_rows': detail_rows,
    'compliant_events': compliant_events,
    'compliance_rate': (compliant_events / total) if total else None,
  }

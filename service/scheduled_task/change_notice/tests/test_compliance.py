"""变更通知合规判定自检（纯 assert、不连库）：三条规则的边界、无法判定、合规率、
0 事件降级、规则表可扩展、周报窗口/归属口径。"""

from datetime import datetime
from zoneinfo import ZoneInfo

from service.scheduled_task.change_notice import compliance, weekly_report

_TZ = ZoneInfo('Asia/Shanghai')


def _dt(hour, minute=0, day=20):
  return datetime(2026, 11, day, hour, minute, tzinfo=_TZ)


def _ts(dt):
  return int(dt.timestamp())


def _ev(**over):
  """默认一条「发过开始、有完成」的正常事件，按用例覆盖。"""
  base = {
    'park': '廊坊', 'planned_start': None, 'start_msg_time': None,
    'has_start': True, 'end_msg_time': None, 'actual_end': None,
  }
  base.update(over)
  return base


def assert_equal(actual, expected, label):
  assert actual == expected, f'{label}: {actual!r} != {expected!r}'


def test_late_start():
  """① 开始通知晚于计划开始：差值超过宽限才不合规，相等算合规。"""
  rule = compliance.RULES[0]
  assert_equal(rule.key, 'late_start_notice', '规则① key')

  # 宽限 5 分钟：准时 / 恰好 5 分 / 第 6 分
  cfg = compliance.Cfg(late_grace_minutes=5)
  assert_equal(rule.fn(_ev(planned_start=_dt(10), start_msg_time=_ts(_dt(10))), cfg),
               (True, ''), '准时合规')
  assert_equal(rule.fn(_ev(planned_start=_dt(10), start_msg_time=_ts(_dt(10, 5))), cfg)[0],
               True, '恰好等于宽限 → 合规')
  late = rule.fn(_ev(planned_start=_dt(10), start_msg_time=_ts(_dt(10, 6))), cfg)
  assert_equal(late[0], False, '超出宽限 → 不合规')
  assert '晚于计划开始' in late[1], '明细写明晚发'

  # 缺计划开始或开始通知时刻 → 无法判定（不臆造）
  assert_equal(rule.fn(_ev(planned_start=None, start_msg_time=1), cfg)[0], None,
               '缺计划开始 → 无法判定')
  assert_equal(rule.fn(_ev(planned_start=_dt(10), start_msg_time=None), cfg)[0], None,
               '缺开始通知 → 无法判定')


def test_missing_start():
  """② 当日未发送开始：has_start 为假即不合规（恒可判定）。"""
  rule = compliance.RULES[1]
  assert_equal(rule.key, 'missing_start_notice', '规则② key')
  cfg = compliance.Cfg()
  assert_equal(rule.fn(_ev(has_start=True), cfg)[0], True, '有开始 → 合规')
  miss = rule.fn(_ev(has_start=False), cfg)
  assert_equal(miss[0], False, '缺开始 → 不合规')
  assert '未发送变更开始通知' in miss[1], '明细写明缺开始'


def test_completion_mismatch():
  """③ 完成通知时间与实际完成不符：差值 > 阈值才不合规，等于阈值算合规。"""
  rule = compliance.RULES[2]
  assert_equal(rule.key, 'completion_time_mismatch', '规则③ key')
  cfg = compliance.Cfg(completion_diff_minutes=30)
  sent = _ts(_dt(11))

  assert_equal(rule.fn(_ev(end_msg_time=sent, actual_end=_dt(11)), cfg)[0], True,
               '完全一致 → 合规')
  assert_equal(rule.fn(_ev(end_msg_time=sent, actual_end=_dt(11, 30)), cfg)[0], True,
               '晚 30 分 = 阈值 → 合规')
  assert_equal(rule.fn(_ev(end_msg_time=sent, actual_end=_dt(10, 30)), cfg)[0], True,
               '早 30 分 = 阈值 → 合规')
  early = rule.fn(_ev(end_msg_time=sent, actual_end=_dt(11, 31)), cfg)
  assert_equal(early[0], False, '晚 31 分 → 不合规')
  assert '相差' in early[1], '明细写明偏差'
  assert_equal(rule.fn(_ev(end_msg_time=sent, actual_end=_dt(10, 29)), cfg)[0], False,
               '早 31 分 → 不合规（取绝对值）')

  # 缺完成通知或实际完成时间 → 无法判定
  assert_equal(rule.fn(_ev(end_msg_time=None, actual_end=_dt(11)), cfg)[0], None,
               '缺完成通知 → 无法判定')
  assert_equal(rule.fn(_ev(end_msg_time=sent, actual_end=None), cfg)[0], None,
               '通知未写实际完成 → 无法判定')


def test_evaluate_empty():
  """0 事件：total 0、合规率 None、规则清单完整且全 0、无明细。"""
  result = compliance.evaluate_events([])
  assert_equal(result['total'], 0, '0 事件')
  assert_equal(result['compliance_rate'], None, '合规率 None（分母为 0）')
  assert_equal(result['compliant_events'], 0, '合规事件 0')
  assert_equal(result['detail_rows'], [], '无明细')
  assert_equal(len(result['rules']), len(compliance.RULES), '规则条数与 RULES 一致')
  for slot in result['rules']:
    assert_equal((slot['violations'], slot['undetermined'], slot['compliant']),
                 (0, 0, 0), f'{slot["key"]} 全 0')


def test_evaluate_stats():
  """逐事件跑全部规则 → 统计 + 合规率 + 园区分布 + 明细条数。"""
  cfg = compliance.Cfg(late_grace_minutes=0, completion_diff_minutes=30)
  events = [
    # 事件1：晚发 + 完成偏差 → 两条不合规 → 整体不合规
    _ev(park='廊坊', planned_start=_dt(10), start_msg_time=_ts(_dt(10, 30)),
        end_msg_time=_ts(_dt(12)), actual_end=_dt(11)),
    # 事件2：缺开始 → 不合规
    _ev(park='平湖', has_start=False, end_msg_time=_ts(_dt(9)), actual_end=_dt(9)),
    # 事件3：完全合规
    _ev(park='廊坊', planned_start=_dt(10), start_msg_time=_ts(_dt(10)),
        end_msg_time=_ts(_dt(12)), actual_end=_dt(12)),
  ]
  result = compliance.evaluate_events(events, cfg)
  assert_equal(result['total'], 3, '总事件数')
  assert_equal(result['compliant_events'], 1, '仅事件3合规')
  assert abs(result['compliance_rate'] - 1 / 3) < 1e-9, '合规率 = 1/3'

  rules = {r['key']: r for r in result['rules']}
  assert_equal(rules['late_start_notice']['violations'], 1, '规则① 违规 1（事件1）')
  assert_equal(rules['late_start_notice']['compliant'], 1, '规则① 合规 1（事件3）')
  assert_equal(rules['late_start_notice']['undetermined'], 1, '规则① 无法判定 1（事件2）')
  assert_equal(rules['missing_start_notice']['violations'], 1, '规则② 违规 1（事件2）')
  assert_equal(rules['completion_time_mismatch']['violations'], 1, '规则③ 违规 1（事件1）')

  # 详情：事件1 命中 2 条规则 + 事件2 命中 1 条 = 3 行
  assert_equal(len(result['detail_rows']), 3, '不合规明细行数')
  assert all({'园区', '数据中心', '工单号', '维护事项', '规则', '明细',
              '计划开始', '实际结束', '消息时间'} <= set(row) for row in result['detail_rows']), \
    '明细行列齐全'

  # 园区分布按总量降序
  parks = [p['park'] for p in result['by_park']]
  assert_equal(parks[0], '廊坊', '园区按总量降序（廊坊 2 > 平湖 1）')
  by_park = {p['park']: p for p in result['by_park']}
  assert_equal((by_park['廊坊']['compliant'], by_park['廊坊']['violation']), (1, 1),
               '廊坊 合规1/不合规1')


def test_rule_table_extensible():
  """规则表是唯一扩展点：key 唯一、标题非空、evaluate 按 RULES 全量跑。"""
  keys = [r.key for r in compliance.RULES]
  assert_equal(len(keys), len(set(keys)), '规则 key 唯一')
  assert all(r.title for r in compliance.RULES), '每条规则都有标题'
  # 临时追加一条“永远合规”的规则 → 统计里自动多出这一项（零代码改动验证）
  extra = compliance.Rule('__demo__', '示例规则', lambda ev, cfg: (True, ''))
  original = compliance.RULES
  compliance.RULES = original + (extra,)
  try:
    result = compliance.evaluate_events([_ev()])
    assert '__demo__' in {r['key'] for r in result['rules']}, '追加规则自动进入汇总'
  finally:
    compliance.RULES = original


def test_week_window():
  """周报窗口 = 上一个完整自然周（7 天）。"""
  win = weekly_report.week_window()
  assert_equal((win['end_s'] - win['start_s']) // 86400, 7, '恰好 7 天')
  # 起点是周一 00:00 Asia/Shanghai
  start = datetime.fromtimestamp(win['start_s'], _TZ)
  assert start.weekday() == 0 and (start.hour, start.minute) == (0, 0), '起点周一 00:00'


def test_attribution_ts():
  """事件归属时刻优先级：完成通知 > 开始通知 > 消息时间。"""
  assert_equal(weekly_report._attribution_ts(
    {'end_msg_time': 300, 'start_msg_time': 200, 'msg_time': 100}), 300, '完成通知优先')
  assert_equal(weekly_report._attribution_ts(
    {'end_msg_time': None, 'start_msg_time': 200, 'msg_time': 100}), 200, '退开始通知')
  assert_equal(weekly_report._attribution_ts({'msg_time': 100}), 100, '再退消息时间')
  assert_equal(weekly_report._attribution_ts({}), 0, '全空 → 0')


def main():
  test_late_start()
  test_missing_start()
  test_completion_mismatch()
  test_evaluate_empty()
  test_evaluate_stats()
  test_rule_table_extensible()
  test_week_window()
  test_attribution_ts()
  print('变更通知合规判定自检通过')


if __name__ == '__main__':
  main()

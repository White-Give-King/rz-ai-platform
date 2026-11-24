"""每周任务纯函数自检：不联网、不连库、不调模型。

运行：`python -m service.drill_audit.tests.test_weekly`
"""

import asyncio
from datetime import date, datetime
from zoneinfo import ZoneInfo

from service.drill_audit import weekly

_TZ = ZoneInfo('Asia/Shanghai')


def t_parse_day():
  assert weekly.parse_day(None) is None
  assert weekly.parse_day('') is None
  assert weekly.parse_day('   ') is None
  assert weekly.parse_day('不是日期') is None
  assert weekly.parse_day('2026-04-09') == date(2026, 4, 9)
  assert weekly.parse_day('2026-04-09 10:51:00') == date(2026, 4, 9)
  assert weekly.parse_day('2026/04/09') == date(2026, 4, 9)
  assert weekly.parse_day('2026-04-09T10:51:00Z') == date(2026, 4, 9)
  # 秒 / 毫秒时间戳（按 Asia/Shanghai 归日）
  ts = datetime(2026, 4, 9, 12, 0, tzinfo=_TZ).timestamp()
  assert weekly.parse_day(str(int(ts))) == date(2026, 4, 9)
  assert weekly.parse_day(str(int(ts * 1000))) == date(2026, 4, 9)


def _week(**over):
  base = {'id': 'w1', 'week': '3', 'status': '0', 'fileStatus': '有',
          'stime': '2026-04-06 00:00:00', 'etime': '2026-04-09 10:51:00'}
  base.update(over)
  return base


def t_is_due():
  today = date(2026, 4, 9)
  assert weekly.is_due(_week(), today, 14) is True
  # 窗口下沿：今天 − 14 天内，边界含当天
  assert weekly.is_due(_week(etime='2026-03-26'), today, 14) is True
  assert weekly.is_due(_week(etime='2026-03-25'), today, 14) is False
  # 未来周 / 超出今天都不审
  assert weekly.is_due(_week(etime='2026-04-10'), today, 14) is False
  # 未安排 / 没有记录文件
  assert weekly.is_due(_week(status='1'), today, 14) is False
  assert weekly.is_due(_week(fileStatus=''), today, 14) is False
  # etime 解析不到
  assert weekly.is_due(_week(etime=''), today, 14) is False


def t_period_of():
  assert weekly.period_of(date(2026, 4, 9)) == ('2026-W15', '2026 第 15 周')
  # 跨年 ISO 周：2025-12-29（周一）已属 2026 年第 1 周
  assert weekly.period_of(date(2025, 12, 29)) == ('2026-W01', '2026 第 1 周')
  assert weekly.period_of(date(2026, 1, 5)) == ('2026-W02', '2026 第 2 周')


def t_years_to_scan():
  assert weekly.years_to_scan(date(2026, 6, 1)) == [2026]
  assert weekly.years_to_scan(date(2026, 1, 20)) == [2026]
  assert weekly.years_to_scan(date(2026, 2, 1)) == [2026]
  # 1 月上旬补上一年（跨年的演练周仍在上一年计划里）
  assert weekly.years_to_scan(date(2026, 1, 5)) == [2026, 2025]
  assert weekly.years_to_scan(date(2026, 1, 14)) == [2026, 2025]


def t_week_key():
  assert weekly.week_key(2026, '45') == '2026-W45'
  assert weekly.week_key(2026, 3) == '2026-W3'
  assert weekly.week_key(2026, '03') == '2026-W3'   # 与前端 Number() 同口径
  assert weekly.week_key(2025, 1) == '2025-W1'
  # 脏输入不抛、返回空串（前端会退回按日期做 key）
  assert weekly.week_key(2026, '') == ''
  assert weekly.week_key(2026, None) == ''
  assert weekly.week_key('', '3') == ''
  assert weekly.week_key('abc', '3') == ''
  assert weekly.week_key(0, '3') == ''


def t_plan_roster():
  dept = {'dept_id': 'd1', 'dept_name': 'A1', 'company_name': '廊坊'}
  plan = {
    'id': 'p1', 'name': '制冷单元故障', 'drillSpecialityId': '285gLxiBnkb',
    'weeks': [
      _week(id='w1', week='3', finishStatus='3', fileStatus='A,B'),
      _week(id='w2', week='4', status='1'),          # 未安排，剔除
      _week(id='', week='5'),                        # 缺 week_id，剔除
      _week(id='w6', week=''),                       # 缺周号，剔除
      _week(id='w9', week='9', etime='2026-12-31'),  # 未来周仍算排期
    ],
  }
  row = weekly.plan_roster(plan, dept, 2026)
  assert row is not None
  assert row['plan_id'] == 'p1'
  assert row['dept_id'] == 'd1' and row['dept_name'] == 'A1'
  assert row['company_name'] == '廊坊'
  assert row['speciality'] == '制冷'
  assert row['year'] == 2026
  keys = [w['key'] for w in row['weeks']]
  assert keys == ['2026-W3', '2026-W9'], keys
  assert row['weeks'][0]['week_id'] == 'w1'
  assert row['weeks'][0]['stime'].startswith('2026-04-06')
  # 执行状态与已交记录班组合集原样带出（看板据此展示状态 + 决定能否单周执行）
  assert row['weeks'][0]['finish_status'] == '3'
  assert row['weeks'][0]['file_status'] == 'A,B'
  assert row['weeks'][1]['finish_status'] == ''
  assert row['weeks'][1]['file_status'] == '有'
  # 无计划 id / 脏输入
  assert weekly.plan_roster({'name': 'x'}, dept, 2026) is None
  assert weekly.plan_roster(None, dept, 2026) is None
  assert weekly.plan_roster({'id': 'p2'}, None, 2026) is not None
  assert weekly.plan_roster({'id': 'p2'}, None, 2026)['weeks'] == []


def t_week_candidates():
  today = date(2026, 4, 9)
  plans = [
    {
      'id': 'p1', 'name': '制冷单元故障', 'dept_id': 'd1', 'dept_name': 'A1',
      'year': 2026,
      'weeks': [
        _week(id='w2', week='4', etime='2026-04-08 10:00:00'),
        _week(id='w1', week='3', etime='2026-04-09 10:00:00'),
        _week(id='w1', week='3', etime='2026-04-09 10:00:00'),   # 重复 week_id
        _week(id='w9', week='9', etime='2026-05-01 10:00:00'),    # 未来周，剔除
      ],
    },
    {
      'id': 'p2', 'name': '电动阀门故障', 'dept_id': 'd1', 'dept_name': 'A1',
      'weeks': [_week(id='w3', week='5', etime='2026-04-07 10:00:00',
                      status='1')],                              # 未安排，剔除
    },
  ]
  out = weekly.week_candidates(plans, today, 14)
  ids = [c['week_id'] for c in out]
  assert ids == ['w2', 'w1'], ids       # 去重 + 按 etime 升序
  assert out[0]['plan_name'] == '制冷单元故障'
  assert out[0]['dept_name'] == 'A1'
  assert out[0]['year'] == 2026
  for key in ('plan_id', 'plan_name', 'dept_id', 'dept_name', 'speciality',
              'week_id', 'week', 'stime', 'etime', 'year'):
    assert key in out[0], key
  # 空 / 脏输入不抛
  assert weekly.week_candidates(None, today, 14) == []
  assert weekly.week_candidates([{}], today, 14) == []


def t_summarize():
  assert weekly.summarize([]) == '本轮没有需要审查的演练周'
  records = [
    {'plan_name': '制冷单元故障', 'dept_name': 'A1', 'week': '3',
     'summary': 'A班组 2 项问题', 'issues': [{'severity': '高'}, {'severity': '低'}]},
    {'plan_name': '电动阀门故障', 'dept_name': 'A2', 'week': '4',
     'summary': '未发现与 EOP 不符的环节', 'issues': []},
  ]
  text = weekly.summarize(records)
  assert text.startswith('本轮审查 2 个演练周，共 2 项问题'), text
  assert '- A1 · 制冷单元故障 · 第 3 周：A班组 2 项问题' in text, text
  assert '- A2 · 电动阀门故障 · 第 4 周：未发现与 EOP 不符的环节' in text, text


def t_run_selected_weeks():
  calls = []

  async def fake_run(**kwargs):
    calls.append(kwargs)
    if kwargs['week_id'] == 'bad':
      raise RuntimeError('boom')
    return {'plan_name': '制冷单元故障', 'dept_name': 'A1', 'week': '3',
            'summary': 'A班组 1 项问题', 'issues': [{'severity': '高'}]}

  out = asyncio.run(weekly.run_selected_weeks(
    [
      {'week_id': 'w1', 'dept_id': 'd1', 'year': 2026},
      {'week_id': 'bad', 'dept_id': 'd1', 'year': 2026},   # 失败不中断
      {'week_id': 'w2'},                                   # 不带 dept_id/year
      {'week_id': 'w1'},                                   # 重复 week_id，跳过
      {'week_id': ''},                                     # 空 week_id，跳过
      'not-a-dict',                                        # 脏输入，跳过
    ],
    refresh=True, run_one=fake_run,
  ))
  assert [c['week_id'] for c in calls] == ['w1', 'bad', 'w2'], calls
  # 统一走手动来源；refresh 透传
  assert all(c['audit'] is True and c['audit_source'] == 'manual' for c in calls)
  assert all(c['refresh'] is True for c in calls)
  assert calls[0]['dept_id'] == 'd1' and calls[0]['year'] == 2026
  # 不带 dept_id/year 的项不传这两个 kwarg（后端按 week_id 自行解析）
  assert 'dept_id' not in calls[2] and 'year' not in calls[2], calls[2]
  assert out['weeks'] == 3 and out['audited'] == 2
  assert out['issues_total'] == 2
  assert len(out['errors']) == 1 and 'bad' in out['errors'][0], out['errors']
  assert out['summary'].startswith('本轮审查 2 个演练周'), out['summary']
  # 空清单
  empty = asyncio.run(weekly.run_selected_weeks([], run_one=fake_run))
  assert empty['weeks'] == 0 and empty['audited'] == 0
  assert empty['summary'] == '本轮没有需要审查的演练周'


def main():
  checks = [t_parse_day, t_is_due, t_period_of, t_years_to_scan,
            t_week_key, t_plan_roster, t_week_candidates, t_summarize,
            t_run_selected_weeks]
  for fn in checks:
    fn()
    print(f'ok {fn.__name__}')
  print(f'\n{len(checks)} 项自检全部通过')


if __name__ == '__main__':
  main()

"""期号解析与目录形态判定自检（纯函数、不连库不联网）。

跑法：`python -m service.doc_update.tests.test_period`
"""

from datetime import datetime, timedelta, timezone

from service.doc_update import period as p

NOW = datetime(2026, 11, 3, tzinfo=timezone.utc)


def _file(segments, create_time=None):
  return {'segments': segments, 'create_time': create_time}


def _epoch(year, month=6, day=1):
  return int(datetime(year, month, day, tzinfo=timezone.utc).timestamp() * 1000)


def test_year_month_variants():
  assert p.parse_period(['2026', '9月']) == '2026-09'
  assert p.parse_period(['2026年9月']) == '2026-09'
  assert p.parse_period(['2026-09']) == '2026-09'
  assert p.parse_period(['2026/09']) == '2026-09'
  assert p.parse_period(['2026_9']) == '2026-09'
  assert p.parse_period(['2026.9']) == '2026-09'


def test_deepest_period_wins():
  assert p.parse_period(['2026', '9月', '2026-11']) == '2026-11'
  assert p.parse_period(['2026-08', '10月']) == '2026-10'


def test_month_only_uses_fallback_year():
  assert p.parse_period(['9月'], 2025) == '2025-09'
  assert p.parse_period(['9月']) == '', '没有年份也没有 fallback 时解析不到'
  assert p.parse_period(['2025', '9月'], 2024) == '2025-09', '目录里的年份优先于 fallback'


def test_embedded_title_month_dir():
  """部门共享树里月份目录常带业务标题：…【2025年10月总第116期】。"""
  assert p.parse_period(['2025', '润泽国际信息港A-1能源报告【2025年10月总第116期】']) == '2025-10'
  assert p.parse_period(['能源报告（2026年9月）']) == '2026-09'
  assert p.parse_period(['2026-11更新']) == '', '横杠写法不做模糊匹配（文件名坑）'


def test_year_inside_container_name():
  """年份写在业务目录名里：`2026年考勤` / `A9-卫生清洁记录表（2026）`。"""
  assert p.parse_period(['2026年考勤', '10月']) == '2026-10'
  assert p.parse_period(['2026年度文件', '9月']) == '2026-09'
  assert p.parse_period(['A9-卫生清洁记录表（2026）', '10月']) == '2026-10'
  assert p.parse_period(['2026年隐患排查记录表', '10月']) == '2026-10'
  assert p.parse_period(['2026年10月报表']) == '2026-10', \
    'embedded 先取走月份，年份容器分支不能只取年'
  assert p.parse_period(['2.3.4.6值班巡检记录表（部长 工程师 班长）', '10月']) == '', \
    '括号里不是四位数年份，不能当容器'


def test_year_suffix_container_name():
  """年份写在业务目录名末尾：`卫生清洁记录2026`（廊坊 A1 当年的记录目录）。"""
  assert p.parse_period(['卫生清洁记录2026', '10月']) == '2026-10'
  assert p.parse_period(['卫生清洁记录2027', '1月']) == '2027-01'
  assert p.parse_period(['卫生清洁记录2026']) == '', '只有年份不是月份级期号'
  assert p.parse_period(['区域2101机房', '10月']) == '', '四位房间代号不当年份'


def test_year_only_is_not_month_level():
  assert p.parse_period(['2026']) == ''
  assert p.parse_period(['2026年']) == ''
  assert p.parse_period([]) == ''
  assert p.parse_period(['其他目录']) == ''


def test_invalid_month_rejected():
  assert p.parse_period(['2026-13']) == ''
  assert p.parse_period(['13月'], 2026) == ''
  assert p.parse_period(['1999-09']) == '', '年份超出 2000~2100 不认'


def test_period_of_file_uses_create_year():
  file = _file(['9月'], _epoch(2025))
  assert p.period_of_file(file) == '2025-09'
  assert p.period_of_file(_file(['2026', '9月'], _epoch(2024))) == '2026-09'


def test_detect_layout():
  assert p.detect_layout([]) == p.LAYOUT_FLAT
  assert p.detect_layout([_file(['2026']), _file(['线上更新'])]) == p.LAYOUT_FLAT
  assert p.detect_layout([_file(['线上更新']), _file(['2026', '9月'])]) == p.LAYOUT_PERIOD


def test_pick_latest_period():
  files = [_file(['2026', '8月']), _file(['2026', '10月']), _file(['线上更新'])]
  assert p.pick_latest_period(files) == '2026-10'
  assert p.pick_latest_period([_file(['线上更新'])]) == ''


def test_period_only_uses_file_name():
  """4.1.5 四条：目录只到年份，期号只写在文件名里（A18 实测的真实写法）。"""
  y2025 = ['A18-隐患排查记录表-2025']
  y2026 = ['A18-隐患排查记录表-2026（月更）']
  f = {'segments': y2025, 'create_time': None,
       'topic': 'A18数据中心-10月部门级隐患排查表_供配电.pdf'}
  assert p.period_of_file(f, from_name=True) == '2025-10'
  f = {'segments': y2026, 'create_time': None,
       'topic': 'A18-2026.9月部门级隐患排查记录表_供配电.pdf'}
  assert p.period_of_file(f, from_name=True) == '2026-09'
  f = {'segments': y2026, 'create_time': None,
       'topic': 'A18数据中心-1月部门级隐患排查表_供配电.pdf'}
  assert p.period_of_file(f, from_name=True) == '2026-01'
  assert p.period_of_file(f) == '', '不传 from_name 时只看目录，解析不到'
  latest = p.pick_latest_period([
    {'segments': y2025, 'topic': 'A18数据中心-12月部门级隐患排查表_供配电.pdf'},
    {'segments': y2026, 'topic': 'A18-2026.9月部门级隐患排查记录表_供配电.pdf'},
  ], from_name=True)
  assert latest == '2026-09', '年份取目录，不能拿 2025 目录的 12 月当最新'


def test_flat_new_item_uses_file_name():
  """扁平查新增项（目录只到年份、月份写在文件名里）的真实写法，A18 实测。"""
  y2026 = ['A18-视频监控系统运行报告-2026（月更）']
  assert p.period_of_file(
    {'segments': y2026, 'topic': 'A18-2026.10视频监控系统运行报告.pdf'},
    from_name=True) == '2026-10'
  assert p.period_of_file(
    {'segments': y2026, 'topic': '202512机房巡检记录.pdf'},
    from_name=True) == '2025-12'
  assert p.period_of_file(
    {'segments': y2026, 'topic': 'A18-10月值班记录表.pdf'},
    from_name=True) == '2026-10', '文件名的 10月 没写年份，取目录年份'
  assert p.period_of_file(
    {'segments': ['A18-培训记录'], 'topic': '2025年度（10月）培训记录.pdf'},
    from_name=True) == '2025-10'
  assert p.period_of_file(
    {'segments': y2026, 'topic': 'A18-2026.10视频监控系统运行报告.pdf'}) == '', \
    '目录只有年份时，不传 from_name 解析不到（查更新项不得拿文件名当新一期）'


def test_chinese_numeral_month():
  """中文数字月份：部门把月份写在文件名里（`（十月）`），年份取目录、不猜 create_time。"""
  assert p.name_period('视频监控录像查询记录单（十月）.pdf', 2026) == '2026-10'
  assert p.name_period('视频监控录像查询记录单（十一月）.pdf', 2026) == '2026-11'
  assert p.name_period('视频监控录像查询记录单（十二月）.pdf', 2026) == '2026-12'
  assert p.name_period('视频监控录像查询记录单（一月）.pdf', 2026) == '2026-01'
  assert p.name_period('视频监控录像查询记录单（十月）.pdf') == '', \
    '没有年份也没有 fallback 时解析不到'
  assert p.parse_period(['2026年', '十月']) == '2026-10'
  assert p.parse_period(['2026年十月报表']) == '2026-10'
  assert p.parse_period(['十月'], 2026) == '2026-10'
  # 同一文件名多个中文月份取最后一个（表示「更新到某月」）
  assert p.name_period(
    '视频录像监控查询单（一月）（二月）（三月）（四月）（五月）（六月）'
    '（七月）（九月）（十月）.pdf', 2026) == '2026-10'
  # 阿拉伯写法优先，中文月份只在前面都没命中时才用
  assert p.name_period('2026.9月记录（十月）.pdf', 2026) == '2026-09'
  # 反例：不是月份
  assert p.name_period('第一周记录.pdf', 2026) == ''
  assert p.name_period('统一月报.pdf', 2026) == ''
  assert p.name_period('十月度总结.pdf', 2026) == ''


def test_relevant():
  """页面展示裁剪：本期/更年全留；没有就只留最新一期；模板可保留。"""
  rows = [
    {'fid': 'a', 'period': '2025-08'},
    {'fid': 'b', 'period': '2025-10'},
    {'fid': 'tpl', 'period': ''},
    {'fid': 'c', 'period': '2026-10'},
  ]
  kept = p.relevant(rows, '2026-10')
  assert [r['fid'] for r in kept] == ['c'], kept
  kept = p.relevant(rows, '2026-11')
  assert [r['fid'] for r in kept] == ['c'], '一条都没有时只留最新一期'
  assert [r['fid'] for r in p.relevant(rows, '')] == ['c'], \
    'expected 为空时只留最新一期'
  kept = p.relevant(rows, '2026-11', keep_undated=True)
  assert [r['fid'] for r in kept] == ['tpl', 'c'], '保留模板并保持原顺序'
  kept = p.relevant(rows, '2026-10', keep_undated=True)
  assert [r['fid'] for r in kept] == ['tpl', 'c'], kept
  undated = [{'fid': 'x', 'period': ''}, {'fid': 'y'}]
  assert p.relevant(undated, '2026-10') == undated, '全部无期号时原样返回'
  assert p.relevant([], '2026-10') == []


def test_cross_year_2027():
  """跨年：年份容器换成 2027 后照常识别（运行逻辑不写死年份）。"""
  assert p.parse_period(['2027年考勤', '1月']) == '2027-01'
  assert p.parse_period(['2027年度文件', '12月']) == '2027-12'
  assert p.parse_period(['A9-卫生清洁记录表（2027）', '1月']) == '2027-01'
  assert p.parse_period(['2027', '1月']) == '2027-01'
  assert p.parse_period(['2027年1月报表']) == '2027-01'
  assert p.parse_period(['1月'], 2027) == '2027-01'
  assert p.name_period('A18-2027.1月部门级隐患排查记录表_供配电.pdf', 2027) == '2027-01'
  file = {'segments': ['A18-隐患排查记录表-2027'], 'create_time': None,
          'topic': 'A18-2027.1月部门级隐患排查记录表_供配电.pdf'}
  assert p.period_of_file(file, from_name=True) == '2027-01'
  assert p.detect_layout([_file(['2027年考勤', '1月'])]) == p.LAYOUT_PERIOD
  assert p.pick_latest_period([_file(['2026年考勤', '12月']),
                               _file(['2027年考勤', '1月'])]) == '2027-01'
  assert p.expected_period(datetime(2027, 1, 15)) == '2027-01'


def test_recover_period():
  """读侧用「目录链年份 + 文件名月份」补旧快照的空期号（确定性、不猜）。"""
  path = ('2.运行维护管理 / 2.2运行管理 / 2.2.8视频监控录像查询记录单'
          ' / 2026年输出物')
  assert p.recover_period(
    '视频录像监控查询单（一月）（二月）（三月）（四月）（五月）（六月）'
    '（七月）（九月）（十月）.pdf', path) == '2026-10'
  assert p.recover_period('视频监控录像查询记录单（十月）.pdf', path) == '2026-10'
  # 目录链与文件名都没有年份 → 解析不到（保持空，走 fid/基线口径）
  assert p.recover_period('视频监控录像查询记录单（十月）.pdf',
                          '2.2.8视频监控录像查询记录单') == ''
  # 文件名自带年份时以文件名为准（不回归既有 name_period 优先级）
  assert p.recover_period('A18-2026.10视频监控系统运行报告.pdf',
                          '视频监控系统运行报告 / 2026（月更）') == '2026-10'
  assert p.recover_period('', path) == ''
  assert p.recover_period('视频监控录像查询记录单（十月）.pdf', '') == ''


def test_mixed_year_layout_falls_back_to_name():
  """同一清单项跨年形态不一致：旧年有月份目录、新年只有文件名月份（A6/A10/A11 实测）。"""
  files = [
    {'segments': ['4.1.6.5电伴热安全检查', '2025年', '12月'],
     'topic': '电伴热安全检查表2025-12-20.pdf'},
    {'segments': ['4.1.6.5电伴热安全检查', '2026年'],
     'topic': '电伴热检查-3月.pdf'},
  ]
  assert p.detect_layout(files) == p.LAYOUT_PERIOD, '旧年文件即可把布局判成按期'
  assert p.pick_latest_period(files) == '2025-12', '只看目录会停在旧年'
  assert p.pick_latest_period(files, from_name=True) == '2026-03', '年份取目录、月份取文件名'
  # 月目录之后再套目录同样取最深层月份
  assert p.parse_period(['2026年', '中间层', '10月', '供配电']) == '2026-10'


def test_mixed_year_flat_file_name_real_samples():
  """A10/A11 实测写法：目录只到年份，月份只在文件名。"""
  a10 = {'segments': ['3.8门禁权限管理', '2026年', '货物运维通道使用登记表'],
         'topic': '10月份货运通道使用登记表.pdf'}
  assert p.period_of_file(a10, from_name=True) == '2026-10'
  assert p.period_of_file(a10) == '', '不传 from_name 时只看目录，解析不到'
  a11 = {'segments': ['2.3.3交接班管理', '交接班签字表', '2026年'],
         'topic': '11月交接班签字表.pdf'}
  assert p.period_of_file(a11, from_name=True) == '2026-11'


def test_auto_flip_by_department_shape():
  """线上更新项按部门形态自动改判：按月新交 vs 原地更新。"""
  a6 = [
    {'segments': ['6.3人员工时管理', '人员排班计划表-2026'],
     'topic': '6.3.1润泽科技-廊坊-A6-人员排班计划表（10月）.xlsx'},
    {'segments': ['6.3人员工时管理', '人员排班计划表-2026'],
     'topic': '6.3.1润泽科技-廊坊-A6-人员排班计划表（9月）.xlsx'},
  ]
  assert p.auto_flip(a6) is True
  # 原地更新：名字里只有年份、没有月份 → 不翻，仍按 sha256 判
  a5 = [
    {'segments': ['6.3人员工时管理', '人员排班计划表'],
     'topic': '6.3.1润泽科技-廊坊-A5-人员排班计划表-2025年.xlsx'},
    {'segments': ['6.3人员工时管理', '人员排班计划表'],
     'topic': '6.3.1润泽科技-廊坊-A5-人员排班计划表-2026年.xlsx'},
  ]
  assert p.auto_flip(a5) is False
  # 只有一条「名字带日期」的文件不翻：原地覆盖时会被旧月份误判成「未更新」
  one = [{'segments': ['人员排班计划表'], 'topic': '人员排班计划表-2026-10更新.xlsx'}]
  assert p.auto_flip(one) is False
  assert p.auto_flip([{'segments': ['2026年', '10月'], 'topic': 'x.xlsx'}]) is True
  assert p.auto_flip([]) is False


def test_month_clue_without_year():
  """带月份线索但没有年份锚 → 待人工确认（不交模型、不改状态）。"""
  assert p.month_clue_no_period(
    '人员排班计划表（10月）.xlsx', '6.3人员工时管理 / 人员排班计划表') is True
  assert p.month_clue_no_period(
    '视频监控录像查询记录单（十月）.pdf', '2.2.8视频监控录像查询记录单') is True
  assert p.month_clue_no_period(
    '人员排班计划表（10月）.xlsx', '6.3人员工时管理 / 人员排班计划表-2026') is False
  assert p.month_clue_no_period('人员排班计划表模板.xlsx', '') is False
  assert p.month_clue_no_period('2026.9月记录.xlsx', '') is False


def test_expected_period_grace():
  assert p.expected_period(NOW) == '2026-11', '默认就查当月（期间即应到期）'
  assert p.expected_period(NOW, grace_months=1) == '2026-10'
  assert p.expected_period(NOW, grace_months=2) == '2026-09'
  assert p.expected_period(NOW - timedelta(days=300), grace_months=1) == '2025-12'
  assert p.expected_period(datetime(2027, 1, 15), grace_months=1) == '2026-12'


def test_shift_period():
  """字符串版月份平移：查新增项的接受下限回退用它（不碰期间/快照键）。"""
  assert p.shift_period('2026-11', -1) == '2026-10'
  assert p.shift_period('2026-01', -1) == '2025-12'
  assert p.shift_period('2026-11', 0) == '2026-11'
  assert p.shift_period('', -1) == ''
  assert p.shift_period('bad', -1) == 'bad'


def test_current_period():
  """判定侧「本期」口径：本地当前月（补跑出来的旧期永不等于它）。"""
  assert p.current_period(datetime(2026, 11, 3)) == '2026-11'
  assert p.current_period(datetime(2026, 1, 1)) == '2026-01'
  assert p.current_period(datetime(2027, 12, 31)) == '2027-12'


def test_check_target_period():
  """补跑期校验：格式 / 未来期 / 已存在（force 只绕过最后一条）。"""
  assert p.check_target_period('2026-10', '2026-11', False) == ''
  assert p.check_target_period('2026-11', '2026-11', False) == '', '当期可重跑'
  assert '已有快照' in p.check_target_period('2026-11', '2026-11', True)
  assert '已有快照' in p.check_target_period('2026-10', '2026-11', True)
  assert '未来' in p.check_target_period('2026-12', '2026-11', False)
  assert '格式' in p.check_target_period('2026-13', '2026-11', False)
  assert '格式' in p.check_target_period('10月', '2026-11', False)
  assert '格式' in p.check_target_period('', '2026-11', False)


def main() -> int:
  tests = [v for k, v in sorted(globals().items()) if k.startswith('test_')]
  for fn in tests:
    fn()
    print(f'  ok  {fn.__name__}')
  print(f'全部通过：{len(tests)} 项')
  return 0


if __name__ == '__main__':
  raise SystemExit(main())

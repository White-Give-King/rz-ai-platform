"""每日变更统计自检（纯 assert、不连库）：三格式解析（lf/bracket/ph，repair 丢弃）、
ph 楼宇映射、配对去重、类型分桶、负责人姓名归一（剥电话/负责人优先值班兜底）、
等级匹配、页面导出 xlsx（等级匹配列 + 重点列底色）、结果缓存/快照编解码/键、抽取有界并发。"""

import json
from datetime import datetime
from zoneinfo import ZoneInfo

from service.scheduled_task.change_notice import build, parse

_TZ = ZoneInfo('Asia/Shanghai')
_T0 = int(datetime(2026, 11, 19, 7, 49, tzinfo=_TZ).timestamp())

_LF_START = (
  '【变更开始-通知】 | 变更工单号：RZKJ-LF-A1-ZL-BG-20261026-13 | 变更地点：A1数据中心 | '
  '变更内容：冷塔季度维护 | 变更等级：中级 | 变更影响范围：M3模组 | 现场负责人：崔海朋 | '
  '计划开始时间:2026-11-19 08:30 | 计划结束时间:2026-11-19 18:30 | '
  '变更描述：根据2026年制冷设备维护计划，对A1数据中心M3模组3#冷塔进行季度维护。')
_LF_DONE = (
  '【变更完成-通知】 | 变更工单号：RZKJ-LF-A1-ZL-BG-20261026-13 | 变更地点：A1数据中心 | '
  '变更内容：冷塔季度维护 | 变更等级：中级 | 变更范围：M3模组 | 现场负责人：崔海朋 | '
  '开始时间:2026-11-19 08:35 | 结束时间:2026-11-19 17:02 | 变更描述：季度维护完成。')

# 2026-11-24 A18 实测：同一工单跨班次做两轮，两轮窗口与负责人均不同 → 应算两次变更。
_A18_START1 = (
  '【变更开始-通知】 | 变更工单号：RZKJ-LF-A18-GPD-BG-20261111-3 | 变更地点：A18数据中心 | '
  '变更内容：UPS 电池组更换 | 变更等级：中级 | 现场负责人：姚天毅、崔浩 | '
  '开始时间:2026-11-24 06:30 | 结束时间:2026-11-24 08:30')
_A18_DONE1 = (
  '【变更完成-通知】 | 变更工单号：RZKJ-LF-A18-GPD-BG-20261111-3 | 变更地点：A18数据中心 | '
  '变更内容：UPS 电池组更换 | 变更等级：中级 | 现场负责人：姚天毅、崔浩 | '
  '开始时间:2026-11-24 06:30 | 结束时间:2026-11-24 07:45')
_A18_START2 = (
  '【变更开始-通知】 | 变更工单号：RZKJ-LF-A18-GPD-BG-20261111-3 | 变更地点：A18数据中心 | '
  '变更内容：UPS 电池组更换 | 变更等级：中级 | 现场负责人：王新恒、张秀峰 | '
  '开始时间:2026-11-24 08:45 | 结束时间:2026-11-24 18:00')

_PH = (
  '【ET15机房Y楼-维护通告】开启 | 1、标题：低压柜季度预防性维护 | 2、位置：Y楼 | '
  '3、内容：低压柜季度预防性维护 | 4、时间：2026年11月19日  8:40-22：00 | '
  '5、原由：按照2026年度维护计划对低压柜进行季度预防性维护 | 6、人员：张作霖、朱冯汶 | '
  '7、影响：基础设施无影响， IT供电无影响 | 8、通报：已告知值班人员，驻场人员，IT人员 | '
  '9、进度：今日维护工作前准备工作已完成')

_BRACKET = (
  '【变更开始-A1-M2-403机房机柜加电阈值设置】 | 【变更单号】RZKJ-FS-A1-ZNH-YZ-20261117-1 | '
  '【变更等级】高 | 【计划开始时间】2026年11月17日 13:00 | '
  '【计划结束时间】2026年11月17日 14:30 | 【操作原因】A1-M2-403机房机柜加电，需进行阈值设置； | '
  '【影响范围】模组11-403机房-10#列头柜； | 【相关告警】阈值设置期间会产生单路列头柜支路开关状态报警； | '
  '【进展情况】人员已准备就绪； | 【变更描述】按照客户上电工单需求，对A1数据中心-M2模组： | '
  '403机房：10-4；')

_CQ_HTML = (
  '<p>【变更开始-A1-M1-AB系统A路三相相电压阈值】</p>'
  '<p>【变更单号】RZKJ-CQ-A1-ZNH-YZ-20261119-1</p><p>【变更等级】高风险</p>'
  '<p>【计划开始时间】2026-11-19 09:22</p><p>【计划结束时间】2026-11-19 10:15</p>'
  '<p>【操作原因】市电电压波动，现调整AB系统A路三相相电压阈值</p>'
  '<p>【影响范围】AB系统A路中压柜；</p><p>【进展情况】人员已准备就绪；</p>')

_REPAIR = (
  '【维修变更开始】空调漏水维修 | 【故障维修单号】RZKJ-HZ-A1-ZL-WX-20261119-1 | 【变更等级】中 | '
  '【计划开始时间】2026-11-19 10:00 | 【计划结束时间】2026-11-19 12:00 | '
  '【操作原因】空调漏水维修 | 【影响范围】A1-M1-101机房 | '
  '【现场联系人】值班电话：0752-8883801 值班工程师：孟欣 15130286145 | '
  '【相关负责人】孟欣 15130286145 | 【发送范围】润泽&腾讯客户沟通群')

# 重庆：`【值班工程师】` 单独成行（原先不在别名表里 → 当成上一条续行，把负责人
# 污染成带【】的原文片段）
_CQ_CONTACT = (
  '【变更开始-A2-M1-低压柜检修】\n'
  '【变更单号】RZKJ-CQ-A2-ZNH-YZ-20261119-2\n'
  '【变更等级】中级\n'
  '【计划开始时间】2026-11-19 14:00\n'
  '【计划结束时间】2026-11-19 16:00\n'
  '【影响范围】A2-M1 配电区域\n'
  '【值班工程师】孟欣 15130286145\n')

_DUTY = 'A-1 11月19日 白班值班人员： | 供配电: 尹朝露 张月明 白峻豪 | 制冷: 郭俊迪 董阔'

# 廊坊实测（2026-11-19）：`实际开始时间 / 实际结束时间 / 完成时间 / 变更完成描述`
# 不在别名表里。原先被当成上一条字段的续行 → 丢实际时间、污染「现场负责人」；
# 现记为 `unknown` 交模型兜底，且不再并入前一条字段。
_LF_UNKNOWN = (
  '【变更完成-通知】 | 变更工单号：RZKJ-LF-A5-ZL-BG-20261119-7 | 变更地点：A5数据中心 | '
  '变更内容：UPS 季度维护 | 变更等级：中级 | 变更影响范围：M1模组 | 现场负责人：李四 | '
  '实际开始时间:2026-11-19 16:07 | 实际结束时间:2026-11-19 17:26 | '
  '计划开始时间:2026-11-19 16:00 | 计划结束时间:2026-11-19 18:00 | '
  '完成时间:2026-11-19 17:26 | 变更完成描述：UPS 季度维护完成，运行正常。')

# 影响范围内的续行（无语义提示词）仍按续行，不能记成缺失字段。
_LF_SCOPE_CONT = (
  '【变更开始-通知】\n'
  '变更工单号：RZKJ-LF-A5-ZL-BG-20261119-8\n'
  '变更地点：A5数据中心\n'
  '变更内容：配电检修\n'
  '变更等级：中级\n'
  '变更影响范围：M1 M2\n'
  '5F-3#电力室:\n'
  '现场负责人：王五\n'
  '计划开始时间：2026-11-19 08:00\n'
  '计划结束时间：2026-11-19 12:00\n')


# 重庆生产原文（2026-11-22 11:42，工单 RZKJ-CQ-A2-ZL-GZ-20261121-1）：联系人、值班工程师、
# 变更负责人三个标签堆在一起。「现场负责人」列按「负责人优先、值班兜底」只取变更负责人（朱传佳），
# 值班工程师里的秦昊不进该列；号码一律剥掉。
_CQ_LIVE = (
  '<p>【变更开始-A2-M1-4#二次泵维修】</p>'
  '<p>【变更单号】RZKJ-CQ-A2-ZL-GZ-20261121-1</p>'
  '<p>【变更等级】中级</p>'
  '<p>【计划开始时间】2026-11-22 11:00</p>'
  '<p>【计划结束时间】2026-11-22 13:00</p>'
  '<p>【现场联系人】值班电话：023-68174134/023-68174135</p>'
  '<p>【值班工程师】朱传佳：18600886680</p>'
  '<p>秦   昊：16636732002</p>'
  '<p>【变更负责人】朱传佳：18600886680</p>')

# 只有值班电话、无任何姓名：负责人列留空，交模型兜底（needs_llm=True）。
_CQ_PHONE_ONLY = (
  '<p>【变更开始-A2-M1-某检修】</p>'
  '<p>【变更单号】RZKJ-CQ-A2-ZL-GZ-20261121-2</p>'
  '<p>【现场联系人】值班电话：023-68174134/023-68174135</p>')


def assert_equal(actual, expected, label):
  assert actual == expected, f'{label}: {actual!r} != {expected!r}'


def test_formats():
  lf = parse.parse_message(_LF_START, _T0, park='廊坊', sender='崔海朋')
  assert lf is not None and lf['fmt'] == 'lf'
  assert_equal(lf['ticket_no'], 'RZKJ-LF-A1-ZL-BG-20261026-13', 'lf 工单号')
  assert_equal(lf['dc'], 'A1', 'lf 数据中心')
  assert_equal(lf['title'], '冷塔季度维护', 'lf 维护事项')
  assert_equal(lf['type'], '中风险变更', 'lf 变更类型')
  assert_equal(lf['status'], '进行中', 'lf 状态')
  assert_equal(lf['planned_start'].strftime('%m-%d %H:%M'), '11-19 08:30', 'lf 计划开始')
  assert_equal(lf['contact'], '崔海朋', 'lf 负责人')

  done = parse.parse_message(_LF_DONE, _T0 + 4000, park='廊坊')
  assert_equal(done['status'], '已完成', 'lf 完成状态')
  assert_equal(done['actual_end'].strftime('%m-%d %H:%M'), '11-19 17:02', 'lf 实际结束')

  fs = parse.parse_message(_BRACKET, _T0, park='佛山')
  assert fs is not None and fs['fmt'] == 'bracket'
  assert_equal(fs['ticket_no'], 'RZKJ-FS-A1-ZNH-YZ-20261117-1', 'bracket 工单号')
  assert_equal(fs['dc'], 'A1', 'bracket 数据中心')
  assert_equal(fs['type'], '高风险变更', 'bracket 变更类型')
  assert_equal(fs['planned_start'].strftime('%Y-%m-%d %H:%M'), '2026-11-17 13:00',
               'bracket 计划开始')

  cq = parse.parse_message(_CQ_HTML, _T0, park='重庆')
  assert cq is not None and cq['fmt'] == 'bracket'
  assert_equal(cq['ticket_no'], 'RZKJ-CQ-A1-ZNH-YZ-20261119-1', '重庆 HTML 工单号')
  assert_equal(cq['title'], 'A1-M1-AB系统A路三相相电压阈值', '重庆 HTML 标题')
  assert_equal(cq['type'], '高风险变更', '重庆变更类型')

  cq_contact = parse.parse_message(_CQ_CONTACT, _T0, park='重庆')
  assert cq_contact is not None and cq_contact['fmt'] == 'bracket'
  assert_equal(cq_contact['contact'], '孟欣', '值班工程师单独成行、兜底只取姓名')
  assert '15130286145' not in cq_contact['contact'], '电话被剥掉'

  # 噪声（值班名单/天气）不含变更头 → 丢弃
  assert parse.parse_message(_DUTY, _T0, park='廊坊') is None


def test_contact_names():
  """负责人字段 → 姓名列表：剥电话/工号/标签前缀、合并大字间距、保序去重。"""
  cn = parse.contact_names
  assert_equal(cn('崔海朋'), ['崔海朋'], '单姓名')
  assert_equal(cn('张作霖、朱冯汶'), ['张作霖', '朱冯汶'], '顿号分隔两名')
  assert_equal(cn('值班电话：0752-8883801 值班工程师：孟欣 15130286145'),
               ['孟欣'], '电话/标签词剥掉，只留姓名')
  assert_equal(cn('0752-8883801 15130286145'), [], '纯号码串 → 空')
  assert_equal(cn('张三 李四'), ['张三', '李四'], '空格分隔两名')
  assert_equal(cn('秦   昊'), ['秦昊'], '大字间距合并')


def test_cq_contact():
  """重庆生产原文：负责人优先、值班兜底，号码剥掉，模型兜底闸放宽。"""
  from service.scheduled_task.change_notice import extract

  ev = parse.parse_message(_CQ_LIVE, _T0, park='重庆')
  assert ev is not None and ev['fmt'] == 'bracket'
  assert_equal(ev['contact'], '朱传佳', '只取变更负责人，值班工程师不入列')
  assert '秦昊' not in ev['contact'], '值班工程师不进负责人列'
  assert '18600886680' not in ev['contact'], '电话被剥掉'
  assert_equal(ev['unknown'], [], '所有标签都已登记')
  assert extract.needs_llm(ev) is False, '已解析出姓名 → 不调模型'

  # 只有值班电话、无姓名：负责人留空，触发模型兜底
  phone_only = parse.parse_message(_CQ_PHONE_ONLY, _T0, park='重庆')
  assert_equal(phone_only['contact'], '', '无姓名 → 负责人留空')
  assert extract.needs_llm(phone_only) is True, '有负责人标签却无姓名 → 兜底'


def test_source_table():
  """变更群消息只读写自己的表，绝不回落到应急通报表。"""
  from service.scheduled_task.change_notice import source

  assert source.TABLE == 'dcim_change_reports'
  sql = ' '.join([source._MESSAGES_SQL, source._UPSERT_SQL, source._DELETE_SQL])
  assert 'dcim_change_reports' in sql
  assert 'dcim_incident_reports' not in sql, '变更统计不得读写应急通报表'


def test_pairing():
  start = parse.parse_message(_LF_START, _T0, park='廊坊')
  done = parse.parse_message(_LF_DONE, _T0 + 4000, park='廊坊')
  events = build.build_events([start, done])
  assert_equal(len(events), 1, '开始+完成配对成一条')
  assert_equal(events[0]['status'], '已完成', '合并后状态')
  assert_equal(events[0]['planned_start'].strftime('%H:%M'), '08:30', '保留计划开始')
  assert_equal(events[0]['actual_end'].strftime('%H:%M'), '17:02', '补上实际结束')

  # 同一工单、计划窗口不同 → 是两次变更（2026-11-24 A18 口径），各自成条
  shifted = parse.parse_message(
    _LF_START.replace('计划开始时间:2026-11-19 08:30', '计划开始时间:2026-11-19 09:20'),
    _T0 + 5000, park='廊坊')
  events = build.build_events([start, shifted])
  assert_equal(len(events), 2, '窗口不同 → 两条变更')
  assert_equal({e['planned_start'].strftime('%H:%M') for e in events}, {'08:30', '09:20'},
               '两条各带自己的窗口')

  # 同窗口重发 → 仍只计一条
  dup = parse.parse_message(_LF_START, _T0 + 5000, park='廊坊')
  events = build.build_events([start, dup])
  assert_equal(len(events), 1, '同窗口重发只计一条')


def test_pairing_split_by_window():
  """一个工单可对应多次变更（2026-11-24 A18 实测）：同单两轮窗口 → 拆两条。"""
  msgs = [
    _A18_START1, _A18_DONE1, _A18_START2,
  ]
  parsed = [parse.parse_message(m, _T0 + i * 60, park='廊坊')
            for i, m in enumerate(msgs)]
  assert all(parsed), '三条通知都要解析成功'
  events = build.build_events(parsed)
  assert_equal(len(events), 2, '同单两轮变更拆成两条')

  by_status = {e['status']: e for e in events}
  assert_equal(by_status['已完成']['actual_start'].strftime('%H:%M'), '06:30',
               '第一轮：完成通知配回 06:30 那条')
  assert_equal(by_status['已完成']['actual_end'].strftime('%H:%M'), '07:45',
               '第一轮实际结束')
  assert_equal(by_status['已完成']['contact'], '姚天毅、崔浩', '第一轮负责人')
  assert_equal(by_status['进行中']['actual_start'].strftime('%H:%M'), '08:45',
               '第二轮独立成条')
  assert_equal(by_status['进行中']['contact'], '王新恒、张秀峰', '第二轮负责人')


def test_merge_notice_keys():
  """_merge 带出逐条通知口径（内部键，不进明细列），供合规判定用。"""
  def ev(msg_time, status):
    return {'park': '廊坊', 'ticket_no': 'T1', 'msg_time': msg_time, 'status': status}

  # 开始 + 完成 → 两路都有，各自取最早
  merged = build.build_events([ev(1000, '进行中'), ev(2000, '已完成')])[0]
  assert_equal(merged['has_start'], True, '有开始通知')
  assert_equal(merged['start_msg_time'], 1000, '开始通知时刻')
  assert_equal(merged['has_end'], True, '有完成通知')
  assert_equal(merged['end_msg_time'], 2000, '完成通知时刻')

  # 只有开始（进行中 / 已取消都算发过开始）→ 没有完成通知
  for status in ('进行中', '已取消'):
    only_start = build.build_events([ev(1000, status)])[0]
    assert_equal(only_start['has_start'], True, f'{status} 算发过开始')
    assert_equal(only_start['start_msg_time'], 1000, f'{status} 开始时刻')
    assert_equal(only_start['has_end'], False, f'{status} 无完成通知')
    assert_equal(only_start['end_msg_time'], None, f'{status} 完成时刻空')

  # 只有完成（漏发开始）→ has_start 假，用于规则②
  only_done = build.build_events([ev(2000, '已完成')])[0]
  assert_equal(only_done['has_start'], False, '只有完成 → 无开始通知')
  assert_equal(only_done['has_end'], True, '有完成通知')

  # 多条完成通知取最早；内部键不进输出明细
  merged2 = build.build_events([ev(1000, '进行中'), ev(2000, '已完成'),
                                ev(3000, '已完成')])[0]
  assert_equal(merged2['end_msg_time'], 2000, '多条完成取最早')
  assert 'has_start' not in build.to_rows([merged2])[0], '内部键不进明细列'


def test_dropped_formats():
  """repair（惠州维修，无计划时间）直接丢弃；bracket 保留。"""
  assert parse.parse_message(_REPAIR, _T0, park='惠州') is None, 'repair 不纳入统计'
  assert parse.parse_message(_BRACKET, _T0, park='佛山')['fmt'] == 'bracket', 'bracket 保留'


def test_ph_format():
  """平湖 ET15 编号列表（ph）：位置楼宇字母映射数据中心，无工单号按 adhoc 配对。"""
  ev = parse.parse_message(_PH, _T0, park='平湖AB')
  assert ev is not None and ev['fmt'] == 'ph'
  assert_equal(ev['dc'], 'A1', 'Y楼 → A1')
  assert_equal(ev['title'], '低压柜季度预防性维护', '维护事项')
  assert_equal(ev['ticket_no'], '', 'ph 无工单号')
  assert_equal(ev['status'], '进行中', '开启 → 进行中')
  assert_equal(ev['contact'], '张作霖、朱冯汶', '现场负责人')
  assert_equal(ev['type'], '未标注', '无等级 → 未标注')
  assert_equal(ev['planned_start'].strftime('%H:%M'), '08:40', '计划开始')
  assert_equal(ev['planned_end'].strftime('%H:%M'), '22:00', '计划结束')
  assert_equal(ev['unknown'], [], '8、通报 无提示词 → 跳过而非未知标签')

  for letter, code in (('Z楼', 'A2'), ('W', 'M3M4'), ('W楼', 'M3M4')):
    moved = parse.parse_message(_PH.replace('2、位置：Y楼', f'2、位置：{letter}'),
                                _T0, park='平湖AB')
    assert_equal(moved['dc'], code, f'{letter} → {code}')

  done = parse.parse_message(_PH.replace('】开启', '】完成'), _T0 + 4000, park='平湖AB')
  assert_equal(done['status'], '已完成', '完成 → 已完成')


def test_ph_pairing():
  """ph 开始+完成按（园区+数据中心+标题+日期）配对；lf 与 ph 不去重，各自成行。"""
  start = parse.parse_message(_PH, _T0, park='平湖AB')
  done = parse.parse_message(_PH.replace('】开启', '】完成'), _T0 + 4000, park='平湖AB')
  events = build.build_events([start, done])
  assert_equal(len(events), 1, 'ph 开始+完成配对成一条')
  assert_equal(events[0]['status'], '已完成', '合并后状态已完成')

  # 同一园区/数据中心/标题的 lf 与 ph：不是重复，不做跨格式去重（用户口径）
  lf_same = (
    '【变更开始-通知】 | 变更工单号：RZKJ-LF-A1-ZL-BG-20261119-99 | 变更地点：A1数据中心 | '
    '变更内容：低压柜季度预防性维护 | 变更等级：中级 | 现场负责人：张三 | '
    '计划开始时间:2026-11-19 08:40 | 计划结束时间:2026-11-19 22:00')
  lf_ev = parse.parse_message(lf_same, _T0, park='平湖AB')
  events = build.build_events([start, lf_ev])
  assert_equal(len(events), 2, 'lf 与 ph 各自成行（不去重）')


def test_rows_and_overview():
  start = parse.parse_message(_LF_START, _T0, park='廊坊')
  cq = parse.parse_message(_CQ_HTML, _T0, park='重庆')
  events = build.build_events([start, cq])
  rows = build.to_rows(events)
  assert_equal(tuple(rows[0].keys()), build.COLUMNS, '列声明与行键一致')
  by_park = {r['园区']: r for r in rows}
  assert_equal(by_park['廊坊']['变更等级'], '中级', '变更等级取报文原值')
  assert_equal(by_park['廊坊']['现场负责人等级'], '', '无职级时该列留空')

  groups = build.overview(events)
  assert_equal(len(groups), 2, '两个园区')
  by_park = {g['park']: g for g in groups}
  assert_equal(by_park['廊坊']['by_type']['中风险变更'], 1, '廊坊中风险计数')
  assert_equal(by_park['重庆']['by_type']['高风险变更'], 1, '重庆高风险计数')
  assert_equal(by_park['廊坊']['by_status']['进行中'], 1, '廊坊进行中计数')

  assert_equal(build.build_events([]), [], '空窗口')
  assert_equal(build.overview([]), [], '空概览')


def test_time_parse():
  assert parse.parse_dt('2026-11-19-07:30').strftime('%H:%M') == '07:30'
  assert parse.parse_dt('2026-11-19  8:30').strftime('%H:%M') == '08:30'
  assert parse.parse_dt('2026年11月17日 13:00').strftime('%H:%M') == '13:00'
  assert parse.parse_dt('2026-11-19') is None, '只有日期没有时分 → None'
  start, end = parse.parse_range('2026年11月19日  8:40-22：00')
  assert start.strftime('%H:%M') == '08:40' and end.strftime('%H:%M') == '22:00'


def test_unknown_labels():
  """未登记但带语义提示词的标签 → 记为 unknown，且不并入前一条字段。"""
  ev = parse.parse_message(_LF_UNKNOWN, _T0, park='廊坊')
  assert ev is not None and ev['fmt'] == 'lf'
  assert_equal(ev['contact'], '李四', '未知标签不再污染负责人')
  assert ev['actual_start'] is None and ev['actual_end'] is None, '实际时间确定性解析为空'
  assert_equal(ev['unknown'], ['实际开始时间', '实际结束时间', '完成时间', '变更完成描述'],
               '未知语义标签按出现顺序记录')

  cont = parse.parse_message(_LF_SCOPE_CONT, _T0, park='廊坊')
  assert_equal(cont['unknown'], [], '5F-3#电力室 不算未知语义标签')
  assert '5F-3#电力室' in cont['scope'], '影响范围续行仍并入 scope'
  assert_equal(cont['contact'], '王五', '续行不影响负责人')

  plain = parse.parse_message(_LF_START, _T0, park='廊坊')
  assert_equal(plain['unknown'], [], '正常报文无未知标签')


def test_extract_fallback():
  """模型兜底：只填空字段、越界时间丢弃、dc/等级重算（不调模型）。"""
  from service.scheduled_task.change_notice import extract

  assert extract.needs_llm(parse.parse_message(_LF_UNKNOWN, _T0, park='廊坊')) is True
  assert extract.needs_llm(parse.parse_message(_LF_START, _T0, park='廊坊')) is False

  # 只填空字段：已有 planned_start / contact 不被覆盖
  ev = parse.parse_message(_LF_UNKNOWN, _T0, park='廊坊')
  norm = extract.normalize(ev, {
    'actual_start': '2026-11-19 16:07', 'actual_end': '2026-11-19 17:26',
    'planned_start': '2026-11-19 09:00', 'contact': '张三',
  })
  assert norm['actual_start'] > 0 and norm['actual_end'] > 0
  assert_equal(norm['planned_end'], 0, '抽不到的计划结束存 0')
  assert extract.apply(ev, norm) is True
  assert ev['actual_start'].strftime('%H:%M') == '16:07'
  assert ev['actual_end'].strftime('%H:%M') == '17:26'
  assert ev['planned_start'].strftime('%H:%M') == '16:00', '已有计划时间不被覆盖'
  assert_equal(ev['contact'], '李四', '已有负责人不被覆盖')

  # 越界时间丢弃（实际时间偏离消息时间 >7 天）
  bad = extract.normalize(parse.parse_message(_LF_UNKNOWN, _T0, park='廊坊'),
                          {'actual_start': '2026-11-01 09:00'})
  assert_equal(bad['actual_start'], 0, '越界时间丢弃')

  # level 填充后 type 重算；dc 按工单号重算
  ev3 = {'msg_time': _T0, 'ticket_no': '', 'dc': '', 'level_raw': '', 'type': '未标注',
         'planned_start': None, 'planned_end': None, 'actual_start': None,
         'actual_end': None, 'contact': '', 'reason': '', 'title': '',
         'scope': '', 'progress': ''}
  norm3 = extract.normalize(ev3, {'level': '高风险', 'ticket_no': 'RZKJ-LF-A2-ZNH-1',
                                  'dc': '某地点', 'actual_start': '2026-11-19 09:00'})
  assert_equal(norm3['dc'], 'A2', 'dc 按工单号重算')
  extract.apply(ev3, norm3)
  assert_equal(ev3['level_raw'], '高风险', '等级填充')
  assert_equal(ev3['type'], '高风险变更', 'type 随等级重算')
  assert ev3['actual_start'].strftime('%H:%M') == '09:00'


def test_contact_level():
  """负责人职级：姓名抽取、等级解码、列声明与 attach_levels（注入 resolver，不联网）。"""
  import asyncio

  from config.settings import settings
  from service.scheduled_task.change_notice import people

  assert_equal(people.extract_names('崔海朋'), ['崔海朋'], '单姓名')
  assert_equal(people.extract_names('张三、李四，张三'), ['张三', '李四'], '切分去重保序')
  assert_equal(people.extract_names('值班电话：0752-8883801 值班工程师：孟欣 15130286145'),
               ['孟欣'], '只留姓名，电话/标签词剥掉')

  assert_equal(people.decode_level('中级'), '中级', '职级原样')
  assert_equal(people.decode_level('2'), '中级', '数字档位映射')
  assert_equal(people.decode_level('无'), '', '无 → 空')
  assert_equal(people.decode_level(''), '', '空 → 空')

  assert_equal(len(build.COLUMNS), 14, '明细 14 列')
  assert_equal(build.COLUMNS.index('现场负责人等级'),
               build.COLUMNS.index('现场负责人') + 1, '等级紧跟负责人')

  async def fake(name, park):
    table = {
      ('张三', '廊坊'): ('中级', 'ok'),
      ('李四', '廊坊'): ('高级', 'ok'),
      ('王五', '廊坊'): ('', 'none'),
      ('赵六', '廊坊'): ('', 'unknown'),
    }
    if name == '炸':
      raise RuntimeError('boom')
    return table.get((name, park), ('', 'unknown'))

  events = [
    {'park': '廊坊', 'contact': '张三'},
    {'park': '廊坊', 'contact': '张三、李四'},
    {'park': '廊坊', 'contact': '王五'},
    {'park': '廊坊', 'contact': '赵六'},
    {'park': '廊坊', 'contact': '炸'},
    {'park': '廊坊', 'contact': '张三、炸'},
  ]
  enabled, mgmt = (settings.CHANGE_NOTICE_CONTACT_LEVEL_ENABLED, settings.MGMT_ENABLED)
  settings.CHANGE_NOTICE_CONTACT_LEVEL_ENABLED = True
  settings.MGMT_ENABLED = True
  try:
    asyncio.run(people.attach_levels(events, resolver=fake))
  finally:
    settings.CHANGE_NOTICE_CONTACT_LEVEL_ENABLED = enabled
    settings.MGMT_ENABLED = mgmt

  assert_equal(events[0]['contact_level'], '中级', '单人等级')
  assert_equal(events[1]['contact_level'], '中级、高级', '多人拼接')
  assert_equal(events[2]['contact_level'], '无评级', '档案确无等级')
  assert_equal(events[3]['contact_level'], '', '查不出留空')
  assert_equal(events[4]['contact_level'], '', '异常不影响出表')
  assert_equal(events[5]['contact_level'], '中级', '一人失败另一人照填')

  assert_equal(people.resolve([{'level': '中级', 'level_known': True}], '廊坊'),
               ('中级', 'ok'), '单一等级')
  conflict = [{'level': '中级', 'level_known': True, 'dept_name': '廊坊A1'},
              {'level': '初级', 'level_known': True, 'dept_name': '平湖A1'}]
  assert_equal(people.resolve(conflict, '廊坊'), ('中级', 'ok'), '重名按园区消歧')
  assert_equal(people.resolve(conflict, ''), ('', 'unknown'), '分不出不猜')
  assert_equal(people.resolve([{'level': '', 'level_known': True}], '廊坊'),
               ('', 'none'), '档案无等级')
  assert_equal(people.resolve([], '廊坊'), ('', 'unknown'), '无候选人')


def test_match_matrix():
  """等级匹配：变更类型 vs 现场负责人等级（逐级对应、取最高、无等级不判定）。"""
  cases = [
    ('低风险变更', '初级', '符合'),
    ('低风险变更', '', '无法判定'),
    ('低风险变更', '无评级', '无法判定'),
    ('中风险变更', '初级', '不符合'),
    ('中风险变更', '中级', '符合'),
    ('中风险变更', '高级', '符合'),
    ('中风险变更', '初级、高级', '符合'),
    ('高风险变更', '初级', '不符合'),
    ('高风险变更', '中级', '符合'),
    ('高风险变更', '高级', '符合'),
    ('临时新增紧急变更', '初级', '不符合'),
    ('临时新增紧急变更', '中级', '符合'),
    ('临时新增紧急变更', '高级', '符合'),
    ('未标注', '高级', '无法判定'),
    ('中风险变更', '其他', '无法判定'),
  ]
  for type_, level, want in cases:
    assert_equal(build.match_of(type_, level), want, f'{type_} × {level or "空"}')

  for m in build.MATCH_ORDER:
    assert m not in build.COLUMNS, '匹配值不进明细列'
  assert build.MATCH_KEY not in build.COLUMNS, '隐藏行键不进列'

  events = [
    {'park': '廊坊', 'type': '中风险变更', 'status': '已完成', 'contact_level': '中级'},
    {'park': '廊坊', 'type': '高风险变更', 'status': '已完成', 'contact_level': '中级'},
  ]
  rows = build.to_rows(events, with_match=True)
  assert_equal(rows[0][build.MATCH_KEY], '符合', '达标')
  assert_equal(rows[1][build.MATCH_KEY], '符合', '中级可做高风险')
  assert build.MATCH_KEY not in build.to_rows(events)[0], '默认不带隐藏行键'

  groups = build.overview(events)
  assert_equal(tuple(groups[0]['by_match'].keys()), build.MATCH_ORDER, '匹配键与顺序一致')
  assert_equal(groups[0]['by_match']['符合'], 2, '符合计数')
  assert_equal(groups[0]['by_match']['不符合'], 0, '不符合计数')
  assert_equal(groups[0]['by_match']['无法判定'], 0, '无法判定计数')


def test_xlsx_export():
  """页面导出：多一列等级匹配 + 重点列底色；默认路径仍 14 列无底色。"""
  import io

  from openpyxl import load_workbook

  from service.scheduled_task.change_notice import xlsx

  events = [
    {'park': '廊坊', 'dc': 'A1', 'type': '高风险变更', 'status': '已完成',
     'contact_level': '初级'},
    {'park': '廊坊', 'dc': 'A2', 'type': '未标注', 'status': '已取消',
     'contact_level': ''},
  ]
  rows = build.to_rows(events, with_match=True)
  groups = build.overview(events)

  ws = load_workbook(io.BytesIO(xlsx.to_bytes(rows, groups, page=True)))[xlsx.DETAIL_SHEET]
  header = [c.value for c in ws[1]]
  assert_equal(len(header), len(build.COLUMNS) + 1, '页面导出 15 列')
  assert_equal(header.index(xlsx.MATCH_COLUMN),
               header.index('现场负责人等级') + 1, '等级匹配紧跟负责人等级')
  assert_equal(xlsx.PAGE_COLUMNS, build.PAGE_COLUMNS, '页面 records / 导出列同源')
  page_rows = build.to_rows(events, with_match=True)
  for row in page_rows:
    row[build.MATCH_COLUMN] = row[build.MATCH_KEY]
  assert_equal([r[build.MATCH_COLUMN] for r in page_rows], ['不符合', '无法判定'],
               'records 可见列取值同判定')
  col = {name: i for i, name in enumerate(header, 1)}

  def fill_of(row, name):
    cell = ws.cell(row=row, column=col[name])
    return cell.value, (cell.fill.fgColor.rgb or '').upper()

  value, rgb = fill_of(2, '变更类型')
  assert_equal(value, '高风险变更', '首行变更类型')
  assert rgb.endswith('FDF0E2'), f'高风险底色与页面一致，实测 {rgb}'
  value, rgb = fill_of(2, '执行状态')
  assert rgb.endswith('E6F4EC'), f'已完成底色 = 成功色，实测 {rgb}'
  value, rgb = fill_of(2, xlsx.MATCH_COLUMN)
  assert_equal(value, '不符合', '高风险 + 初级 → 不符合')
  assert rgb.endswith('FBE3E4'), f'不符合底色 = 危险色，实测 {rgb}'
  assert_equal(fill_of(3, xlsx.MATCH_COLUMN)[0], '无法判定', '未标注 → 无法判定')
  assert fill_of(3, '变更类型')[1].endswith('F1F3F5'), '未标注灰底'
  assert ws.cell(row=2, column=col['维护事项']).fill.patternType is None, '普通列无底色'

  ws = load_workbook(io.BytesIO(xlsx.to_bytes(rows, groups)))[xlsx.DETAIL_SHEET]
  plain = [c.value for c in ws[1]]
  assert_equal(len(plain), len(build.COLUMNS), '默认路径仍 14 列')
  assert xlsx.MATCH_COLUMN not in plain, '默认路径不含等级匹配'
  assert ws.cell(row=2, column=plain.index('变更类型') + 1).fill.patternType is None, \
    '默认路径无底色'

  wb = load_workbook(io.BytesIO(xlsx.to_bytes([], [], page=True)))
  assert_equal(len([c.value for c in wb[xlsx.DETAIL_SHEET][1]]), len(xlsx.PAGE_COLUMNS),
               '0 行明细表头完整')
  overview = [c.value for c in wb[xlsx.OVERVIEW_SHEET][1]]
  assert_equal(len(overview), 2 + len(build.TYPE_ORDER) + len(build.STATUS_ORDER),
               '0 行概览表头完整（园区 + 类型 + 状态 + 合计）')


def test_cache_codec():
  """结果缓存/快照编解码：datetime 原样还原（`build._fmt_time` 只认 datetime 实例）。"""
  from service.scheduled_task.change_notice import store

  when = datetime(2026, 11, 19, 8, 30, tzinfo=_TZ)
  events = [{
    'park': '廊坊', 'msg_time': _T0, 'planned_start': when,
    'planned_end': None, 'tags': ['a', {'deep': when}],
  }]
  round_trip = store.decode(json.loads(json.dumps(store.encode(events))))
  assert isinstance(round_trip[0]['planned_start'], datetime), '时间还原成 datetime'
  assert round_trip[0]['planned_start'] == when, '时间值不变'
  assert_equal(round_trip[0]['planned_end'], None, 'None 不变')
  assert_equal(round_trip[0]['msg_time'], _T0, '整数不变')
  assert round_trip[0]['tags'][1]['deep'] == when, '嵌套时间也能还原'


def test_cache_key():
  """缓存键带数据水位：同（日、园区、count、max_id）同键，任一变化即换键。"""
  from service.scheduled_task.change_notice import workflow

  k1 = workflow._events_key('2026-11-19', None, 5, 100)
  assert_equal(k1, workflow._events_key('2026-11-19', [], 5, 100), '空园区与 None 同键')
  assert_equal(workflow._parks_key(['佛山', '廊坊']), workflow._parks_key(['廊坊', '佛山']),
               '园区顺序无关')
  assert k1 != workflow._events_key('2026-11-19', None, 6, 100), '新消息 → 换键'
  assert k1 != workflow._events_key('2026-11-19', None, 5, 101), '删行/换行 → 换键'
  assert k1 != workflow._events_key('2026-11-20', None, 5, 100), '换日期 → 换键'
  assert k1 != workflow._events_key('2026-11-19', ['廊坊'], 5, 100), '园区子集不同键'


def test_extract_concurrency():
  """抽取有界并发：在飞不超过配置并发数且确实并发；预算仍按前 N 条裁。"""
  import asyncio

  from config.settings import settings
  from service.scheduled_task.change_notice import extract

  events = [{'msg_time': _T0 + i, 'unknown': ['标签'], '_text': 'x'} for i in range(9)]
  state = {'in_flight': 0, 'peak': 0, 'calls': 0}
  original = extract._one

  async def fake(event):
    state['in_flight'] += 1
    state['calls'] += 1
    state['peak'] = max(state['peak'], state['in_flight'])
    await asyncio.sleep(0.02)
    state['in_flight'] -= 1
    return True

  enabled = settings.CHANGE_NOTICE_EXTRACT_LLM_ENABLED
  conc = settings.CHANGE_NOTICE_EXTRACT_LLM_CONCURRENCY
  settings.CHANGE_NOTICE_EXTRACT_LLM_ENABLED = True
  settings.CHANGE_NOTICE_EXTRACT_LLM_CONCURRENCY = 3
  extract._one = fake
  try:
    filled = asyncio.run(extract.enrich(events, max_calls=6))
  finally:
    extract._one = original
    settings.CHANGE_NOTICE_EXTRACT_LLM_ENABLED = enabled
    settings.CHANGE_NOTICE_EXTRACT_LLM_CONCURRENCY = conc

  assert_equal(state['calls'], 6, '预算裁到前 6 条')
  assert_equal(filled, 6, '全部补全')
  assert state['peak'] <= 3, f'在飞不应超过并发数 3，实测 {state["peak"]}'
  assert state['peak'] > 1, f'应确实并发，实测峰值 {state["peak"]}'


def main():
  test_formats()
  test_contact_names()
  test_cq_contact()
  test_dropped_formats()
  test_ph_format()
  test_ph_pairing()
  test_pairing()
  test_pairing_split_by_window()
  test_merge_notice_keys()
  test_rows_and_overview()
  test_time_parse()
  test_source_table()
  test_unknown_labels()
  test_extract_fallback()
  test_contact_level()
  test_match_matrix()
  test_xlsx_export()
  test_cache_codec()
  test_cache_key()
  test_extract_concurrency()
  print('每日变更统计自检通过')


if __name__ == '__main__':
  main()

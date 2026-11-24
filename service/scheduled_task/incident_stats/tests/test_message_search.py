"""群消息检索口径自检（**不依赖测试框架**，纯 assert + 非零退出码）。

    python -m service.scheduled_task.incident_stats.tests.test_message_search

锁住五条：
  1. 关键词切分（中英文逗号/顿号/分号/空白）与 AND 语义（忽略大小写）
  2. kind 过滤：空 = 全部；单值只取该类型；中文别名归一
  3. 发送人子串匹配，`sender` 为空时回退 `sender_id`
  4. `load_messages` 的 SQL **不带** `kind IN`（否则普通消息又只写不读），窗口左闭右开
  5. 表格行：列序、类型中文化、北京时间、`title` 兜底、园区映射
  6. 窗口解析到秒：只给时刻 = 该时刻起 1 分钟（不能退回整天），日期/班次文案逐字不变
"""

import inspect
from datetime import datetime
from zoneinfo import ZoneInfo

from service.scheduled_task.incident_stats import messages, source

_TZ = ZoneInfo('Asia/Shanghai')
_T0 = int(datetime(2026, 11, 12, 9, 12, 0, tzinfo=_TZ).timestamp())


def _row(text='', kind='other', sender='', sender_id='ou_1', title='', chat_id='oc_a',
         cluster='廊坊A', ts=_T0):
  return {'text': text, 'kind': kind, 'sender': sender, 'sender_id': sender_id,
          'title': title, 'chat_id': chat_id, 'cluster': cluster, 'create_time': ts}


# ── 1 ──
def test_split_tokens():
  tokens = messages.split_tokens('UPS 告警,配电、温度;湿度  ups')
  assert tokens == ['ups', '告警', '配电', '温度', '湿度'], tokens
  assert messages.split_tokens('') == []
  assert messages.split_tokens('  ，、 ') == []


def test_filter_keywords_and_semantics():
  rows = [_row('UPS 告警：A1'), _row('UPS 恢复正常'), _row('湿度 偏低')]
  assert len(messages.filter_messages(rows, tokens=['ups'])) == 2
  # 全部 token 命中（AND），不是任一命中
  hit = messages.filter_messages(rows, tokens=[t.lower() for t in messages.split_tokens('UPS 告警')])
  assert len(hit) == 1 and hit[0]['text'] == 'UPS 告警：A1', hit
  # 大小写不敏感 + title 也参与匹配
  assert len(messages.filter_messages(rows, tokens=['a1'])) == 1
  titled = [_row('', title='配电室巡检')]
  assert len(messages.filter_messages(titled, tokens=['巡检'])) == 1


# ── 2 ──
def test_kind_filter():
  rows = [_row('x', kind='sos'), _row('y', kind='first'), _row('z', kind='other')]
  assert len(messages.filter_messages(rows, kinds=())) == 3
  assert [r['kind'] for r in messages.filter_messages(rows, kinds=('other',))] == ['other']
  assert len(messages.filter_messages(rows, kinds=('sos', 'first'))) == 2

  assert messages.kind_from_alias('') == ''
  assert messages.kind_from_alias('全部') == ''
  assert messages.kind_from_alias('速报') == 'sos'
  assert messages.kind_from_alias('通报') == 'first'
  assert messages.kind_from_alias('OTHER') == 'other'
  assert messages.kind_from_alias('weird') == 'weird'   # 非法值由工具层拦


# ── 3 ──
def test_sender_filter():
  rows = [_row('a', sender='张三', sender_id='ou_z'), _row('b', sender='', sender_id='ou_l')]
  assert len(messages.filter_messages(rows, sender='张')) == 1
  # sender 为空（WS 只给 open_id）时用 sender_id 兜底
  assert len(messages.filter_messages(rows, sender='ou_l')) == 1
  assert messages.filter_messages(rows, sender='无此人') == []


# ── 4 ──
def test_load_messages_sql():
  sql = source._MESSAGES_SQL
  assert 'kind IN' not in sql, f'消息检索不能带 kind 过滤：{sql}'
  assert 'create_time >= %s AND create_time < %s' in sql, sql
  assert 'sender_id' in sql and 'ORDER BY' not in sql
  # 事件出表链路必须保持原样（只认速报/首报）
  assert "kind IN ('sos', 'first')" in inspect.getsource(source.load_reports)
  assert 'ORDER BY create_time' in inspect.getsource(source.load_messages)


# ── 5 ──
def test_table_rows():
  rows = [_row('需求：麻烦看下 UPS', kind='first', sender='李四', chat_id='oc_a'),
          _row('', kind='other', sender='', sender_id='ou_x', title='卡片标题',
               chat_id='oc_b', cluster='平湖A'),
          _row('', sender='王五', chat_id='oc_a')]
  table = messages.to_table_rows(rows, {'oc_a': '廊坊'})
  assert tuple(table[0]) == messages.TABLE_COLUMNS, tuple(table[0])
  assert table[0]['时间'] == '2026-11-12 09:12:00', table[0]['时间']
  assert table[0]['园区'] == '廊坊' and table[0]['类型'] == '通报', table[0]
  assert table[0]['发送人'] == '李四'
  # 正文为空时用 title 兜底；未映射的群回退 cluster；sender 空则显示 open_id
  assert table[1]['内容'] == '卡片标题' and table[1]['园区'] == '平湖A'
  assert table[1]['发送人'] == 'ou_x' and table[1]['类型'] == '消息'

  # 服务端表格：列序与空值占位（模型不得自选列）
  from tools.incident_tools import _markdown_table
  markdown = _markdown_table(list(messages.TABLE_COLUMNS), table)
  assert markdown.splitlines()[0] == '| ' + ' | '.join(messages.TABLE_COLUMNS) + ' |'
  assert table[2]['内容'] == ''
  assert '—' in markdown     # 空内容由服务端渲染成占位符，不是空单元格


# ── 6 ──
def test_window_time_precision():
  """用户给「12 时 55 分 00 秒」必须能查该时刻，不能退回整天。"""
  from tools.incident_tools import _event_window, _window

  def _at(epoch):
    return datetime.fromtimestamp(epoch, _TZ)

  start_s, end_s, label = _event_window('', '2026-11-13 12:55:00', '')
  assert _at(start_s).strftime('%H:%M:%S') == '12:55:00', label
  assert end_s - start_s == 60, (start_s, end_s)
  assert label == '2026-11-13 12:55:00 ~ 2026-11-13 12:56:00', label

  # 只到分钟：同为 1 分钟窗口，但文案保持分钟精度（与改造前逐字一致）
  m0, m1, mlabel = _event_window('', '2026-11-13 12:55', '')
  assert (m0, m1) == (start_s, end_s), (m0, m1)
  assert mlabel == '2026-11-13 12:55 ~ 2026-11-13 12:56', mlabel

  # ISO `T` 与秒级区间
  s2, e2, label2 = _event_window('', '2026-11-13T12:55:10', '2026-11-13T12:55:20')
  assert (s2, e2) == (start_s + 10, start_s + 20), (s2, e2)
  assert label2 == '2026-11-13 12:55:10 ~ 2026-11-13 12:55:20', label2

  # 日期与班次窗口逐字不变
  d0, d1, dlabel = _event_window('2026-11-13', '', '')
  assert d1 - d0 == 86400
  assert dlabel == '2026-11-13 00:00 ~ 2026-11-14 00:00', dlabel
  assert _event_window('', '2026-11-13', '2026-11-14')[2] == \
      '2026-11-13 00:00 ~ 2026-11-15 00:00'
  assert _window('', '', 'day', '2026-11-13')[2] == \
      '白班 2026-11-13 08:30 ~ 2026-11-13 17:30'
  assert _window('2026-11-13 12:55:00', '', '', '')[2] == \
      '2026-11-13 12:55:00 ~ 2026-11-13 12:56:00'


if __name__ == '__main__':
  for name, fn in sorted(globals().items()):
    if name.startswith('test_') and callable(fn):
      fn()
  print('群消息检索自检通过（6 项）')

"""模型兜底补期号自检（纯 assert、不连网不连 Redis）。

跑法：`python -m service.doc_update.tests.test_period_llm`
"""

import asyncio
import json

from service.doc_update import period_llm as pl


def _record(fid, name, period='', path=''):
  return {'fid': fid, 'file_name': name, 'period': period, 'path': path}


def test_valid_period_accepts_real_forms():
  expected = '2026-11'
  cases = [
    ('2026-10', 'A18-2026.10视频监控系统运行报告.pdf', '2026.10'),
    ('2025-12', '202512机房巡检记录.pdf', '202512'),
    ('2026-10', 'A18-2026年10月值班记录表.pdf', '2026年10月'),
    ('2025-10', '2025年度（10月）培训记录.pdf', '10月'),
    ('2026-10', 'Monthly report Oct-2026.pdf', 'Oct-2026'),
    ('2026-10', 'A18-2026年十月报表.pdf', '2026年十月'),
  ]
  for period, name, evidence in cases:
    assert pl._valid_period(period, name, evidence, expected) == period, name


def test_valid_period_rejects_bad_output():
  expected = '2026-11'
  # 证据不在文件名里
  assert pl._valid_period('2026-10', 'A18-报表.pdf', '2026.10', expected) == ''
  # 证据里看不到年月
  assert pl._valid_period('2026-10', 'A18-10月报表.pdf', 'A18', expected) == ''
  # 裸 `10月` 的证据、文件名也没有年份 → 无法锚定，丢弃
  assert pl._valid_period('2026-10', 'A18-10月报表.pdf', '10月', expected) == ''
  # 裸中文月份同理：目录也没有年份 → 无法锚定，丢弃（有目录年份时见下一条用例）
  assert pl._valid_period('2026-10', 'A18-视频（十月）报表.pdf', '十月', expected) == ''
  # 不允许未来月份 / 超出应到期
  assert pl._valid_period('2026-12', 'A18-2026.12报表.pdf', '2026.12', expected) == ''
  # 文件名自带年份与期号年份不一致
  assert pl._valid_period('2024-10', '2025年度（10月）报表.pdf', '10月', expected) == ''
  # 非法月份 / 超范围
  assert pl._valid_period('2026-13', 'A18-2026.13报表.pdf', '2026.13', expected) == ''
  assert pl._valid_period('1999-10', 'A18-1999.10报表.pdf', '1999.10', expected) == ''
  # 期号格式不对
  assert pl._valid_period('2026/10', 'A18-2026.10报表.pdf', '2026.10', expected) == ''
  assert pl._valid_period(None, 'A18-2026.10报表.pdf', '2026.10', expected) == ''


def test_valid_period_directory_year():
  """目录链提供年份、文件名提供月份（裸 `（十月）`）——A3 seq12 的真实形态。"""
  expected = '2026-11'
  path = ('2.运行维护管理 / 2.2运行管理 / 2.2.8视频监控录像查询记录单 / 2026年输出物')
  name = '视频录像监控查询单（一月）（二月）（三月）（四月）（五月）（六月）（九月）（十月）.pdf'
  assert pl._valid_period('2026-10', name, '（十月）', expected, path) == '2026-10'
  assert pl._valid_period(
    '2026-10', name, ['2026年输出物', '（十月）'], expected, path) == '2026-10'
  # 证据是十月、声称十一月 → 月份对不上
  assert pl._valid_period(
    '2026-11', name, ['2026年输出物', '（十月）'], expected, path) == ''
  # 目录也没有年份 → 无法锚定
  assert pl._valid_period(
    '2026-10', name, '（十月）', expected, '2.2.8视频监控录像查询记录单') == ''
  # 目录同时有多个年份时取最后一个（与 period._segments_year 同口径）
  assert pl._valid_period(
    '2026-10', name, '（十月）', expected, '2025 / 2026年输出物') == '2026-10'
  assert pl._valid_period(
    '2025-10', name, ['2025', '（十月）'], expected, '2025 / 2026年输出物') == ''
  # 文件名自带年份时以文件名为准，目录不能覆盖
  assert pl._valid_period(
    '2025-10', 'A18-2026.10报表.pdf', '2026.10', expected, '2025年') == ''
  # 证据不是目录链/文件名的原文
  assert pl._valid_period(
    '2026-10', name, '2026年输出物', expected, '2.2.8视频监控录像查询记录单') == ''
  # 「十月度」不是月份
  assert pl._valid_period(
    '2026-10', '十月度总结.pdf', '十月度总结', expected, '2026年') == ''


def _patch_cache():
  pl._cache_read = lambda payload: None
  pl._cache_write = lambda payload, mapping: None


def _run(coro):
  return asyncio.run(coro)


def test_fill_periods_only_unparsed():
  _patch_cache()
  seen = []

  async def fake_chat(messages, **kwargs):
    seen.append(messages)
    return json.dumps({'files': [
      {'file': 'A18-2026年10月视频监控运行报告.pdf',
       'period': '2026-10', 'evidence': '2026年10月'},
      {'file': 'A18-模板_V1.docx', 'period': None, 'evidence': None},
    ]}, ensure_ascii=False)

  pl._chat = fake_chat
  records = [
    _record('f1', 'A18-2026.9月报表.pdf', '2026-09'),   # 确定性已解析，不该发给模型
    _record('f2', 'A18-2026年10月视频监控运行报告.pdf',
            path='4.1.6视频监控系统运行报告 / 2026年'),
    _record('f3', 'A18-模板_V1.docx'),
  ]
  filled = _run(pl.fill_periods('4.1.6', '视频监控系统运行报告', records, '2026-11'))
  assert filled == 1, filled
  assert records[0]['period'] == '2026-09'
  assert records[1]['period'] == '2026-10'
  assert records[2]['period'] == '', '模型返回 null 时保持空'
  payload = json.loads(seen[0][1]['content'])
  assert payload['files'] == [
    {'file': 'A18-2026年10月视频监控运行报告.pdf',
     'path': '4.1.6视频监控系统运行报告 / 2026年'},
    {'file': 'A18-模板_V1.docx', 'path': ''},
  ], payload
  assert payload['expected'] == '2026-11'


def test_fill_periods_uses_directory_year():
  """端到端：记录的 path 要真的随载荷下发并被校验用上。"""
  _patch_cache()

  async def fake_chat(messages, **kwargs):
    return json.dumps({'files': [
      {'file': '视频监控录像查询记录单（十月）.pdf', 'period': '2026-10',
       'evidence': ['2026年输出物', '（十月）']},
    ]}, ensure_ascii=False)

  pl._chat = fake_chat
  records = [_record('f1', '视频监控录像查询记录单（十月）.pdf',
                     path='2.2.8视频监控录像查询记录单 / 2026年输出物')]
  filled = _run(pl.fill_periods('2.2.8', '视频监控录像查询记录单', records, '2026-11'))
  assert filled == 1, filled
  assert records[0]['period'] == '2026-10'


def test_fill_periods_drops_invalid_entries():
  _patch_cache()

  async def fake_chat(messages, **kwargs):
    return json.dumps({'files': [
      {'file': '不在输入里的文件.pdf', 'period': '2026-10', 'evidence': '2026.10'},
      {'file': 'A18-2026.10报表.pdf', 'period': '2026-12', 'evidence': '2026.10'},
      {'file': 'A18-2026.10报表.pdf', 'period': '2026-10', 'evidence': '2026.10'},
    ]}, ensure_ascii=False)

  pl._chat = fake_chat
  records = [_record('f1', 'A18-2026.10报表.pdf'), _record('f2', '模板_AX.xlsx')]
  filled = _run(pl.fill_periods('4.1.6', '报表', records, '2026-11'))
  assert filled == 1, filled
  assert records[0]['period'] == '2026-10'
  assert records[1]['period'] == ''


def test_fill_periods_failure_is_silent():
  _patch_cache()
  writes = []
  pl._cache_write = lambda payload, mapping: writes.append(mapping)

  async def boom(messages, **kwargs):
    raise RuntimeError('upstream down')

  pl._chat = boom
  records = [_record('f1', 'A18-2026.10报表.pdf')]
  assert _run(pl.fill_periods('4.1.6', '报表', records, '2026-11')) == 0
  assert records[0]['period'] == ''
  assert writes == [], '调用失败不缓存，下一轮还要再试'

  async def garbage(messages, **kwargs):
    return '不是 JSON'

  pl._chat = garbage
  assert _run(pl.fill_periods('4.1.6', '报表', records, '2026-11')) == 0
  assert writes == []


def test_fill_periods_nothing_pending():
  _patch_cache()

  async def should_not_call(messages, **kwargs):
    raise AssertionError('没有未解析文件时不该调模型')

  pl._chat = should_not_call
  records = [_record('f1', 'A18-2026.10报表.pdf', '2026-10')]
  assert _run(pl.fill_periods('4.1.6', '报表', records, '2026-11')) == 0


def main() -> int:
  tests = [v for k, v in sorted(globals().items()) if k.startswith('test_')]
  for fn in tests:
    fn()
    print(f'  ok  {fn.__name__}')
  print(f'全部通过：{len(tests)} 项')
  return 0


if __name__ == '__main__':
  raise SystemExit(main())

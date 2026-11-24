"""快照比对自检（纯 assert、不连库）：状态判定 / 关键词消歧 / 增删识别。

跑法：`python -m service.doc_update.tests.test_verdict`
"""

from datetime import datetime, timedelta, timezone

from service.doc_update.catalog import MONTHLY_ITEMS, DocItem
from service.doc_update.fetch import match_item
from service.doc_update.verdict import (
  MODE_BASELINE,
  MODE_CROSS,
  STATUS_BASELINE,
  STATUS_FAILED,
  STATUS_MISSING,
  STATUS_REVIEW,
  STATUS_STALE,
  STATUS_UPDATED,
  evaluate,
  summary_text,
)

NOW = datetime(2026, 10, 30, tzinfo=timezone.utc)


def _file(fid, sha, warn='', days_ago=5):
  return {
    'fid': fid, 'file_name': f'{fid}.pdf', 'path': 'x', 'size': 100,
    'download_size': 100, 'version': '', 'update_time': '', 'upload_time': '',
    'sha256': sha, 'error': '' if sha else 'boom', 'warn': warn,
    'last_changed_at': NOW - timedelta(days=days_ago) if sha else '',
  }


def _snap(period, items):
  return {'period_key': period, 'period_label': period, 'taken_at': NOW,
          'items': items}


def _item(number, name, files, error=''):
  return {'seq': 1, 'number': number, 'name': name, 'cadence': '月度',
          'note': '', 'files': files, 'error': error}


def test_baseline():
  latest = _snap('2026-10', [_item('1.2.2', 'A', [_file('f1', 'aaa')])])
  v = evaluate(latest, None, NOW)
  assert v['baseline'] is True
  assert v['items'][0]['status'] == STATUS_BASELINE
  assert v['counts'][STATUS_BASELINE] == 1


def test_updated_and_stale():
  prev = _snap('2026-09', [_item('1.2.2', 'A', [_file('f1', 'old')]),
                           _item('1.4.5', 'B', [_file('f2', 'same')])])
  latest = _snap('2026-10', [_item('1.2.2', 'A', [_file('f1', 'new')]),
                             _item('1.4.5', 'B', [_file('f2', 'same')])])
  v = evaluate(latest, prev, NOW)
  assert v['baseline'] is False
  status = {i['number']: i['status'] for i in v['items']}
  assert status['1.2.2'] == STATUS_UPDATED, status
  assert status['1.4.5'] == STATUS_STALE, status
  assert len(v['changes']['changed']) == 1
  assert v['changes']['changed'][0]['file_name'] == 'f1.pdf'


def test_missing_vs_failed():
  prev = _snap('2026-09', [_item('2.3.2.6', 'M', [])])
  latest = _snap('2026-10', [_item('2.3.2.6', 'M', []),
                             _item('1.2.2', 'A', [_file('f1', '')])])
  v = evaluate(latest, prev, NOW)
  status = {i['number']: i['status'] for i in v['items']}
  assert status['2.3.2.6'] == STATUS_MISSING, status
  assert status['1.2.2'] == STATUS_FAILED, status
  # 下载失败/未找到都不得计入「未更新」
  assert v['counts'][STATUS_STALE] == 0


def test_partial_failure_still_counts_updated():
  prev = _snap('2026-09', [_item('1.6.6.1', 'X', [_file('f1', 'old'),
                                                  _file('f2', 'old2')])])
  latest = _snap('2026-10', [_item('1.6.6.1', 'X', [_file('f1', 'new'),
                                                    _file('f2', '')])])
  v = evaluate(latest, prev, NOW)
  assert v['items'][0]['status'] == STATUS_UPDATED


def test_warnings_collected():
  latest = _snap('2026-10', [_item('1.2.2', 'A', [_file('f1', 'aaa', warn='大小不符')])])
  v = evaluate(latest, None, NOW)
  assert v['warnings'] and v['warnings'][0]['warn'] == '大小不符'


def test_added_and_removed_fids():
  prev = _snap('2026-09', [_item('A', 'A', [_file('gone', 'x')])])
  latest = _snap('2026-10', [_item('A', 'A', [_file('fresh', 'y')])])
  v = evaluate(latest, prev, NOW)
  assert v['changes']['added_fids'] == ['fresh']
  assert v['changes']['removed_fids'] == ['gone']
  # 新增 fid 没有上期 sha，按「已更新」处理
  assert v['items'][0]['status'] == STATUS_UPDATED


def test_changed_days_ago():
  latest = _snap('2026-10', [_item('A', 'A', [_file('f1', 'aaa', days_ago=12)])])
  v = evaluate(latest, None, NOW)
  assert v['items'][0]['changed_days_ago'] == 12


def test_415_keyword_disambiguation():
  files = [
    {'fid': '1', 'topic': '4.1.5 供配电隐患排查表', 'parent_id': 'p', 'path': ''},
    {'fid': '2', 'topic': '4.1.5 制冷隐患排查表', 'parent_id': 'p', 'path': ''},
    {'fid': '3', 'topic': '4.1.5 智能化隐患排查表', 'parent_id': 'p', 'path': ''},
    {'fid': '4', 'topic': '4.1.5 班组级隐患排查记录表', 'parent_id': 'p', 'path': ''},
    {'fid': '5', 'topic': '4.1.6 电伴热安全检查表', 'parent_id': 'p', 'path': ''},
  ]
  item = DocItem(29, ('4.1.5',), '供配电隐患排查表', '月度', 'PDF', ('供配电',))
  hits = match_item(item, files)
  assert [h['fid'] for h in hits] == ['1'], hits


def test_number_on_directory():
  """部门共享树里编号在目录上，文件名是业务标题（不带编号）。"""
  files = [
    {'fid': '1', 'topic': '润泽国际信息港A-1能源报告【2025年10月总第116期】.pdf',
     'parent_id': 'p', 'path': '', 'segments': ['1.4精益化能源管理', '1.4.5能源管理报告（能源月报同步）']},
    {'fid': '2', 'topic': '别的表.xlsx', 'parent_id': 'p', 'path': '',
     'segments': ['1.4精益化能源管理', '1.4.4能源监视']},
  ]
  item = DocItem(2, ('1.4.5',), '能源月报', '月度', 'PPT', ('能源',))
  assert [h['fid'] for h in match_item(item, files)] == ['1']


def test_actual_folder_names_match_same_item():
  """清单名是模糊叫法：同一份月报在真实树里有三处写法，全部算这一项。"""
  item = next(i for i in MONTHLY_ITEMS if i.seq == 2)
  files = [
    {'fid': 'a', 'topic': '润泽国际信息港A-1能源报告【2026年9月总第128期】.pdf',
     'parent_id': 'p', 'path': '', 'segments': [
       '1.规划管理', '1.4精益化能源管理', '1.4.5能源管理报告（能源月报同步）']},
    {'fid': 'b', 'topic': '容量管理报告.pdf', 'parent_id': 'p', 'path': '',
     'segments': ['1.规划管理', '1.2容量管理', '1.2.3容量管理报告（能源月报同步）']},
    {'fid': 'c', 'topic': '能源管理月报2026-08.xlsx', 'parent_id': 'p', 'path': '',
     'segments': ['8.报告表管理', '8.1报告管理', '8.1.2能源管理类报告', '8.1.2.3能源管理月报']},
    {'fid': 'd', 'topic': '耗电量周报.xlsx', 'parent_id': 'p', 'path': '',
     'segments': ['8.报告表管理', '8.1报告管理', '8.1.2能源管理类报告', '8.1.2.1耗电量周报']},
  ]
  hits = match_item(item, files)
  assert [h['fid'] for h in hits] == ['a', 'b', 'c'], hits
  assert [h['match_dir'] for h in hits] == [
    '1.4.5能源管理报告（能源月报同步）',
    '1.2.3容量管理报告（能源月报同步）',
    '8.1.2.3能源管理月报',
  ], hits  # 取最深层命中的目录，便于页面写「实际目录」


def test_match_dir_empty_when_only_file_name_matches():
  item = next(i for i in MONTHLY_ITEMS if i.seq == 6)
  files = [{'fid': '1', 'topic': '1.6.6.2供配电程序文档.pdf', 'parent_id': 'p',
            'path': '', 'segments': ['1.6.6供配电程序文档目录']}]
  hits = match_item(item, files)
  assert [h['fid'] for h in hits] == ['1']
  assert hits[0]['match_dir'] == ''


def test_directory_number_still_respects_prefix_rule():
  files = [
    {'fid': '1', 'topic': 'x.xlsx', 'parent_id': 'p', 'path': '',
     'segments': ['1.6.6.10别的目录']},
    {'fid': '2', 'topic': 'x.xlsx', 'parent_id': 'p', 'path': '',
     'segments': ['1.6.6.1供配电程序文档目录']},
  ]
  item = DocItem(6, ('1.6.6.1',), '供配电程序文档目录', '月度')
  assert [h['fid'] for h in match_item(item, files)] == ['2']


def test_number_prefix_not_confused():
  files = [{'fid': '1', 'topic': '1.6.6.10 别的表', 'parent_id': 'p', 'path': ''}]
  item = DocItem(6, ('1.6.6.1',), '供配电程序文档目录', '月度')
  assert match_item(item, files) == []


def test_same_month_recheck_still_updated():
  """同月重跑：参照始终是上期，文件已更新就一直显示已更新，不翻成未更新。"""
  prev = _snap('2026-09', [_item('1.2.2', 'A', [_file('f1', 'old')])])
  first = _snap('2026-10', [_item('1.2.2', 'A', [_file('f1', 'new')])])
  again = _snap('2026-10', [_item('1.2.2', 'A', [_file('f1', 'new')])])
  for snap in (first, again):
    v = evaluate(snap, prev, NOW)
    assert v['mode'] == MODE_CROSS
    assert v['items'][0]['status'] == STATUS_UPDATED
    assert v['counts'][STATUS_STALE] == 0


def test_no_reference_is_baseline():
  latest = _snap('2026-10', [_item('1.2.2', 'A', [_file('f1', 'aaa')])])
  v = evaluate(latest, None, NOW)
  assert v['mode'] == MODE_BASELINE
  assert v['baseline'] is True
  text = summary_text(v)
  # 首期只说基线，不能说「未更新」
  assert '未更新' not in text, text
  assert '已建立基线' in text


def _ditem(number, files, judge='new', layout='flat', period_only=False,
           latest_period='', expected='2026-10', period_source='name',
           review_files=(), pending_files=()):
  return {'seq': 1, 'number': number, 'name': 'X', 'cadence': '月度', 'note': '',
          'judge': judge, 'layout': layout, 'period_only': period_only,
          'expected_period': expected, 'latest_period': latest_period,
          'period_source': period_source, 'matched_count': len(files),
          'review_files': list(review_files),
          'pending_files': list(pending_files), 'files': files, 'error': ''}


def _dept_snap(period, items, dept_id='d1', taken_at=NOW):
  return {'period_key': period, 'period_label': period, 'taken_at': taken_at,
          'depts': [{'company_id': 'c1', 'company_name': '廊坊',
                     'dept_id': dept_id, 'dept_name': 'A18', 'items': items}]}


def _pfile(fid, period):
  return {'fid': fid, 'file_name': f'{fid}.pdf', 'path': '', 'match_dir': '',
          'period': period, 'size': 100, 'download_size': 0, 'sha256': '',
          'error': '', 'warn': '', 'last_changed_at': ''}


def test_dept_flat_period_stale():
  """扁平查新增项：文件名解析出期号即按期判 —— 首期也不该是基线。"""
  latest = _dept_snap('2026-11', [_ditem(
    '4.1.6', [_pfile('f1', '2026-10')], latest_period='2026-10')])
  v = evaluate(latest, None, NOW)
  item = v['items'][0]
  assert item['status'] == STATUS_STALE, item
  assert item['latest_period'] == '2026-10'
  assert item['period_source'] == 'name'
  assert v['counts'][STATUS_BASELINE] == 0
  assert item['files'][0]['status'] == STATUS_STALE


def test_dept_flat_period_updated():
  latest = _dept_snap('2026-10', [_ditem(
    '4.1.6', [_pfile('f1', '2026-09'), _pfile('f2', '2026-10')],
    latest_period='2026-10')])
  v = evaluate(latest, None, NOW)
  item = v['items'][0]
  assert item['status'] == STATUS_UPDATED, item
  # 文件级各按自己期号判；页面列表只留本期/最新一期（更早的 f1 由 matched_count 提示）
  status = {f['fid']: f['status'] for f in item['files']}
  assert status == {'f2': STATUS_UPDATED}, status
  assert item['file_count'] == 1 and item['matched_count'] == 2, item


def test_dept_flat_cancels_undated_when_dated_present():
  """扁平查新增项：只要有文件解析出期号，无期号文件（模板/年度汇总）被取消匹配。"""
  files = [_pfile('f1', '2026-10'),
           {'fid': 't1', 'file_name': '模板_V1.xlsx', 'path': '', 'period': ''}]
  latest = _dept_snap('2026-10', [_ditem(
    '4.1.6', files, latest_period='2026-10')])
  item = evaluate(latest, None, NOW)['items'][0]
  assert item['status'] == STATUS_UPDATED, item
  # 无期号的模板被取消：列表只留带期号的文件，匹配总数仍如实反映
  assert [f['fid'] for f in item['files']] == ['f1'], item['files']
  assert item['file_count'] == 1 and item['matched_count'] == 2, item


def test_dept_flat_no_period_falls_back_to_fid():
  """期号全部解析不出（模型兜底也没成）：维持原 fid / 基线口径。"""
  files = [{'fid': 'f1', 'file_name': 'a.pdf', 'period': ''}]
  # 有上期且 fid 是新的 → 已更新
  prev = _dept_snap('2026-09', [_ditem('4.1.6', [
    {'fid': 'old', 'file_name': 'old.pdf', 'period': ''}])])
  v = evaluate(_dept_snap('2026-10', [_ditem('4.1.6', files)]), prev, NOW)
  assert v['items'][0]['status'] == STATUS_UPDATED, v['items'][0]
  # 无上期 → 基线（无期号时才允许）
  v = evaluate(_dept_snap('2026-10', [_ditem('4.1.6', files)]), None, NOW)
  assert v['items'][0]['status'] == STATUS_BASELINE, v['items'][0]


def test_dept_flat_read_side_period_recover():
  """旧快照的空期号在读侧按「目录年份 + 文件名月份」补算（不重跑即可自愈）。"""
  path = '2.2.8视频监控录像查询记录单 / 2026年输出物'
  raw = {'fid': 'f1', 'file_name': '视频监控录像查询记录单（十月）.pdf',
         'path': path, 'period': ''}
  # 期间 2026-10：十月文件算本期已交（老快照 period_source 为空 → 读侧标 'name'）
  latest = _dept_snap('2026-10', [_ditem('2.2.8', [dict(raw)], period_source='')])
  item = evaluate(latest, None, NOW)['items'][0]
  assert item['status'] == STATUS_UPDATED, item
  assert item['latest_period'] == '2026-10', item
  assert item['period_source'] == 'name', item
  assert item['files'][0]['period'] == '2026-10', item['files']
  assert item['files'][0]['status'] == STATUS_UPDATED, item['files']
  # 期间 2026-11：仍是未更新，但最新期号能读出来了
  latest = _dept_snap('2026-11', [_ditem('2.2.8', [dict(raw)], period_source='')])
  item = evaluate(latest, None, NOW)['items'][0]
  assert item['status'] == STATUS_STALE, item
  assert item['latest_period'] == '2026-10', item
  assert item['files'][0]['status'] == STATUS_STALE, item['files']
  # 目录链没有年份 → 补不出来，维持原 fid/基线口径
  no_year = dict(raw, path='2.2.8视频监控录像查询记录单')
  item = evaluate(_dept_snap(
    '2026-10', [_ditem('2.2.8', [no_year], period_source='')]),
    None, NOW)['items'][0]
  assert item['status'] == STATUS_BASELINE, item
  assert item['period_source'] == '', item
  assert item['files'][0]['period'] == '', item['files']
  # 查更新项不受影响：期号为空也照旧按 sha256 判
  prev = _dept_snap('2026-09', [_ditem('6.3.1', [dict(raw, sha256='old')],
                                       judge='update', layout='flat')])
  cur = _dept_snap('2026-10', [_ditem('6.3.1', [dict(raw, sha256='new')],
                                     judge='update', layout='flat')])
  item = evaluate(cur, prev, NOW)['items'][0]
  assert item['status'] == STATUS_UPDATED, item
  assert item['files'][0]['period'] == '', item['files']


def test_dept_flat_period_from_llm():
  latest = _dept_snap('2026-11', [_ditem(
    '4.1.6', [_pfile('f1', '2026-10')], latest_period='2026-10',
    period_source='llm')])
  v = evaluate(latest, None, NOW)
  assert v['items'][0]['period_source'] == 'llm'
  assert v['items'][0]['status'] == STATUS_STALE


def test_dept_update_item_still_hash_compared():
  """查更新项不受影响：照旧按 sha256 比上一期。"""
  prev = _dept_snap('2026-09', [_ditem('6.3.1', [_file('f1', 'old')],
                                       judge='update', layout='flat')])
  latest = _dept_snap('2026-10', [_ditem('6.3.1', [_file('f1', 'new')],
                                         judge='update', layout='flat')])
  v = evaluate(latest, prev, NOW)
  assert v['items'][0]['status'] == STATUS_UPDATED, v['items'][0]


def test_dept_flipped_update_judged_by_period():
  """线上更新项按部门改判为查新增：快照存 judge=new + layout=flat + 期号 → 纯按期判。"""
  latest = _dept_snap('2026-11', [_ditem(
    '6.3.1', [_pfile('f1', '2026-10'), _pfile('f2', '2026-09')],
    judge='new', layout='flat', latest_period='2026-10', expected='2026-11',
    pending_files=['人员排班计划表（10月）.xlsx'])])
  item = evaluate(latest, None, NOW)['items'][0]
  assert item['status'] == STATUS_STALE, item
  assert item['judge'] == 'new', item
  assert item['latest_period'] == '2026-10', item
  # pending_files 只展示，不改状态；去重保序透出
  assert item['pending_files'] == ['人员排班计划表（10月）.xlsx'], item


def test_dept_pending_files_empty_for_old_snapshots():
  """老快照没有 pending_files 字段 → 空数组，不报错。"""
  item = {'seq': 1, 'number': '6.3.1', 'name': 'X', 'cadence': '月度', 'note': '',
          'judge': 'update', 'layout': 'flat', 'expected_period': '2026-10',
          'latest_period': '', 'period_source': '', 'matched_count': 1,
          'files': [_file('f1', 'aaa')], 'error': ''}
  got = evaluate(_dept_snap('2026-10', [item]), None, NOW)['items'][0]
  assert got['pending_files'] == [], got


def test_dept_review_when_uncertain_candidates():
  """确定性匹配为空 + 模型无法确定归属 → 待复核（不当已命中，file_count 仍 0）。"""
  review = [{'fid': 'r1', 'topic': '字节IT库房应急卡领取登记记录表.pdf'}]
  item = _ditem('2.3.2.6', [], review_files=review)
  v = evaluate(_dept_snap('2026-11', [item]), None, NOW)
  got = v['items'][0]
  assert got['status'] == STATUS_REVIEW, got
  assert got['status_label'] == '待复核'
  assert got['file_count'] == 0, got
  # 老快照存文件对象 → 读取时归一成文件名（前端契约是 string[]）
  assert got['review_files'] == ['字节IT库房应急卡领取登记记录表.pdf'], got
  assert got['error'] and '待人工确认' in got['error'], got
  assert v['counts'][STATUS_REVIEW] == 1
  assert v['counts'][STATUS_UPDATED] == 0


def test_dept_review_accepts_name_list():
  """新快照直接存文件名：原样透出（strip、去重），不重复、不变对象。"""
  review = [' 疑似.pdf ', '疑似.pdf', '', '另一份.pdf']
  v = evaluate(_dept_snap('2026-11', [_ditem('2.3.2.6', [], review_files=review)]), None, NOW)
  got = v['items'][0]
  assert got['status'] == STATUS_REVIEW, got
  assert got['review_files'] == ['疑似.pdf', '另一份.pdf'], got


def test_dept_missing_without_review_files():
  """没有疑似文件仍判「未找到」，不硬凑待复核。"""
  v = evaluate(_dept_snap('2026-11', [_ditem('2.3.2.6', [])]), None, NOW)
  got = v['items'][0]
  assert got['status'] == STATUS_MISSING, got
  assert got['review_files'] == [], got
  assert v['counts'][STATUS_REVIEW] == 0


def test_summary_mentions_review_only_when_present():
  review = [{'fid': 'r1', 'topic': '疑似.pdf'}]
  v = evaluate(_dept_snap('2026-11', [_ditem('2.3.2.6', [], review_files=review)]), None, NOW)
  assert '待复核 1' in summary_text(v), summary_text(v)
  v2 = evaluate(_dept_snap('2026-11', [_ditem('2.3.2.6', [])]), None, NOW)
  assert '待复核' not in summary_text(v2), summary_text(v2)


def test_dept_cross_year_2027():
  """跨年：1 月期间不能被 2026-12 的文件蒙混成已交。"""
  now = datetime(2027, 1, 5, tzinfo=timezone.utc)
  latest = _dept_snap('2027-01', [_ditem(
    '4.1.6', [_pfile('f1', '2026-12'), _pfile('f2', '2027-01')],
    latest_period='2027-01', expected='2027-01')], taken_at=now)
  item = evaluate(latest, None, now)['items'][0]
  assert item['status'] == STATUS_UPDATED, item
  # 上年 12 月的 f1 不列出（本期/最新一期裁剪），但匹配数仍含它
  status = {f['fid']: f['status'] for f in item['files']}
  assert status == {'f2': STATUS_UPDATED}, status
  assert item['file_count'] == 1 and item['matched_count'] == 2, item


def test_dept_cross_year_2027_stale():
  """只有上年 12 月文件时判未更新；应到期按快照期间重算，不读库里旧值。"""
  now = datetime(2027, 1, 5, tzinfo=timezone.utc)
  latest = _dept_snap('2027-01', [_ditem(
    '4.1.6', [_pfile('f1', '2026-12')], latest_period='2026-12',
    expected='2026-12')], taken_at=now)
  item = evaluate(latest, None, now)['items'][0]
  assert item['status'] == STATUS_STALE, item
  assert item['expected_period'] == '2027-01', item['expected_period']


def main() -> int:
  tests = [v for k, v in sorted(globals().items()) if k.startswith('test_')]
  for fn in tests:
    fn()
    print(f'  ok  {fn.__name__}')
  print(f'全部通过：{len(tests)} 项')
  return 0


if __name__ == '__main__':
  raise SystemExit(main())

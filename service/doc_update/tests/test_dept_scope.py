"""部门口径判定自检（纯 assert、不连库）：查更新 / 查新增两种判据。

判据由清单项决定（`DocItem.judge`，即 PDF 第三列）：线上更新→查更新（sha256），
其余→查新增（月份期号 / 新 fid）；与目录形态无关。

跑法：`python -m service.doc_update.tests.test_dept_scope`
"""

from datetime import datetime, timedelta, timezone

from service.doc_update.fetch import merge_snapshot
from service.doc_update.verdict import (
  STATUS_BASELINE,
  STATUS_FAILED,
  STATUS_MISSING,
  STATUS_STALE,
  STATUS_UPDATED,
  evaluate,
  summary_text,
)

NOW = datetime(2026, 11, 3, tzinfo=timezone.utc)
EXPECTED = '2026-10'


def _period_file(fid, period, path='2026/9月'):
  return {
    'fid': fid, 'file_name': f'{fid}.pdf', 'path': path, 'period': period,
    'size': 100, 'download_size': 0, 'sha256': '', 'error': '', 'warn': '',
    'last_changed_at': '',
  }


def _flat_file(fid, sha, days_ago=5):
  return {
    'fid': fid, 'file_name': f'{fid}.pdf', 'path': '线上更新', 'period': '',
    'size': 100, 'download_size': 100, 'sha256': sha,
    'error': '' if sha else 'boom', 'warn': '',
    'last_changed_at': NOW - timedelta(days=days_ago) if sha else '',
  }


def _item(seq, number, name, layout, files, latest='', error='', actual_dirs=None,
          period_only=False, judge=''):
  return {
    'seq': seq, 'number': number, 'name': name, 'cadence': '月度', 'note': '',
    'judge': judge, 'layout': layout, 'period_only': period_only,
    'expected_period': EXPECTED,
    'latest_period': latest, 'actual_dirs': actual_dirs or [], 'files': files,
    'error': error,
  }


def _dept(dept_id, name, items, error='', company_id='', company_name=''):
  return {'company_id': company_id, 'company_name': company_name,
          'dept_id': dept_id, 'dept_name': name, 'root_fid': 'root',
          'error': error, 'items': items}


def _snap(depts, period=EXPECTED, backfill=False):
  doc = {'period_key': period, 'period_label': period, 'taken_at': NOW,
         'scope': 'dept', 'depts': depts, 'items': []}
  if backfill:
    doc['backfill'] = True
  return doc


def _status(verdict, dept_name, seq, company_id=None):
  for item in verdict['items']:
    if (item['dept_name'] == dept_name and item['seq'] == seq
        and (company_id is None or item.get('company_id') == company_id)):
      return item['status']
  raise AssertionError(f'没找到 {company_id or ""}/{dept_name}/{seq}')


def test_period_item_updated_when_expected_present():
  snap = _snap([_dept('d1', 'A1', [
    _item(2, '1.4.5', '能源月报', 'period',
          [_period_file('f1', EXPECTED)], latest=EXPECTED),
  ])])
  verdict = evaluate(snap, None)
  assert _status(verdict, 'A1', 2) == STATUS_UPDATED
  assert verdict['items'][0]['latest_period'] == EXPECTED


def test_period_item_updated_when_newer_than_expected():
  snap = _snap([_dept('d1', 'A1', [
    _item(2, '1.4.5', '能源月报', 'period',
          [_period_file('f1', '2026-11')], latest='2026-11'),
  ])])
  assert _status(evaluate(snap, None), 'A1', 2) == STATUS_UPDATED


def test_expected_period_follows_snapshot_period():
  """应到期＝该快照的期间：选 11 月查 11 月，不沿用写库时存的上月值。"""
  snap = _snap([_dept('d1', 'A1', [
    _item(2, '1.4.5', '能源月报', 'period',
          [_period_file('f1', '2026-10')], latest='2026-10'),
  ])], period='2026-11')
  item = evaluate(snap, None)['items'][0]
  assert item['expected_period'] == '2026-11', item['expected_period']
  assert item['status'] == STATUS_STALE, '11 月期间只有 10 月文件，不能算已更新'


def test_new_items_accept_previous_month_when_configured():
  """过渡期：查新增项可接受上月文件（11 月检查认 10 月文件），但仍按 11 月存快照。"""
  from config.settings import settings

  from service.doc_update import period as period_mod

  cur = period_mod.current_period()
  prev_month = period_mod.shift_period(cur, -1)
  old = settings.DOC_UPDATE_NEW_ACCEPT_PREV_MONTHS
  settings.DOC_UPDATE_NEW_ACCEPT_PREV_MONTHS = 1
  try:
    snap = _snap([_dept('d1', 'A1', [
      _item(2, '1.4.5', '能源月报', 'period',
            [_period_file('f1', prev_month)], latest=prev_month, judge='new'),
    ])], period=cur)
    item = evaluate(snap, None)['items'][0]
    assert item['status'] == STATUS_UPDATED, item['status']
    assert item['expected_period'] == prev_month, item['expected_period']
    assert item['files'][0]['status'] == STATUS_UPDATED
  finally:
    settings.DOC_UPDATE_NEW_ACCEPT_PREV_MONTHS = old


def test_accept_prev_not_applied_to_old_period():
  """放宽只对本期生效：补跑出来的旧期快照不能去接受更早一个月的文件。"""
  from config.settings import settings

  from service.doc_update import period as period_mod

  cur = period_mod.current_period()
  old_period = period_mod.shift_period(cur, -1)
  older = period_mod.shift_period(cur, -2)
  saved = settings.DOC_UPDATE_NEW_ACCEPT_PREV_MONTHS
  settings.DOC_UPDATE_NEW_ACCEPT_PREV_MONTHS = 1
  try:
    snap = _snap([_dept('d1', 'A1', [
      _item(2, '1.4.5', '能源月报', 'period',
            [_period_file('f1', older)], latest=older, judge='new'),
    ])], period=old_period)
    item = evaluate(snap, None)['items'][0]
    assert item['expected_period'] == old_period, item['expected_period']
    assert item['status'] == STATUS_STALE, item['status']
  finally:
    settings.DOC_UPDATE_NEW_ACCEPT_PREV_MONTHS = saved


def test_backfill_ignores_future_period_files():
  """补跑＝用当前共享盘重放那一期：晚于该期的文件不算那一期已交。"""
  def files():
    return [_period_file('f1', '2026-09'), _period_file('f2', '2026-11')]

  snap = _snap([_dept('d1', 'A1', [
    _item(2, '1.4.5', '能源月报', 'period', files(), latest='2026-11', judge='new'),
  ])], period='2026-10', backfill=True)
  item = evaluate(snap, None)['items'][0]
  assert item['status'] == STATUS_STALE, item['status']
  assert item['latest_period'] == '2026-09', item['latest_period']
  assert [f['fid'] for f in item['files']] == ['f1'], item['files']

  # 对照组：非补跑快照不看未来文件，2026-11 那条照常算已交
  plain = _snap([_dept('d1', 'A1', [
    _item(2, '1.4.5', '能源月报', 'period', files(), latest='2026-11', judge='new'),
  ])], period='2026-10')
  assert _status(evaluate(plain, None), 'A1', 2) == STATUS_UPDATED


def test_update_items_ignore_accept_previous_month():
  """放宽只作用于查新增：查更新项的应到期仍是本期（它只比 sha256）。"""
  from config.settings import settings

  from service.doc_update import period as period_mod

  cur = period_mod.current_period()
  old = settings.DOC_UPDATE_NEW_ACCEPT_PREV_MONTHS
  settings.DOC_UPDATE_NEW_ACCEPT_PREV_MONTHS = 1
  try:
    snap = _snap([_dept('d1', 'A1', [
      _item(1, '1.2.2', '负载管理统计分析表', 'flat',
            [_flat_file('g1', 'sha-a')], judge='update'),
    ])], period=cur)
    item = evaluate(snap, None)['items'][0]
    assert item['expected_period'] == cur, item['expected_period']
  finally:
    settings.DOC_UPDATE_NEW_ACCEPT_PREV_MONTHS = old


def test_relevant_records_keep_files_from_accept_floor():
  """抓取裁剪也按放宽后的下限：10 月 + 11 月都保留（9 月及更早仍裁掉）。"""
  from service.doc_update import fetch

  recs = [{'fid': 'a', 'period': '2026-11'}, {'fid': 'b', 'period': '2026-10'},
          {'fid': 'c', 'period': '2026-09'}]
  kept = [r['fid'] for r in
          fetch._relevant_records(recs, '2026-10', 'new', 'period')]
  assert kept == ['a', 'b'], kept


def test_flat_new_item_display_trimmed_to_expected():
  """扁平查新增项：页面只列本期/最新一期 + 模板（2026-11 实测：十几个旧文件全列、
  2026-10 的文件反而被判未更新）。判定仍看全量文件。"""
  files = [_period_file(f'p{m}', f'2025-{m:02d}') for m in range(8, 13)]
  files.append(_period_file('p202610', '2026-10'))
  files.append(_period_file('tpl', ''))
  snap = _snap([_dept('d1', 'A1', [
    _item(12, '3.8.7', '视频监控录像查询记录单', 'flat', files, judge='new'),
  ])])
  item = evaluate(snap, None)['items'][0]
  assert item['status'] == STATUS_UPDATED, item['status']
  assert [f['fid'] for f in item['files']] == ['p202610', 'tpl'], item['files']
  assert item['file_count'] == 2, item['file_count']
  assert item['matched_count'] == 7, '另有 5 个历史文件未列出'

  snap = _snap([_dept('d1', 'A1', [
    _item(12, '3.8.7', '视频监控录像查询记录单', 'flat', files, judge='new'),
  ])], period='2026-11')
  item = evaluate(snap, None)['items'][0]
  assert item['status'] == STATUS_STALE, item['status']
  assert item['latest_period'] == '2026-10', item['latest_period']
  assert [f['fid'] for f in item['files']] == ['p202610', 'tpl']


def test_flat_new_item_accept_previous_month_when_configured():
  """放宽整条链：当前月检查认上月文件 → 已更新，列表仍只留该月文件。"""
  from config.settings import settings

  from service.doc_update import period as period_mod

  cur = period_mod.current_period()
  prev_month = period_mod.shift_period(cur, -1)
  saved = settings.DOC_UPDATE_NEW_ACCEPT_PREV_MONTHS
  settings.DOC_UPDATE_NEW_ACCEPT_PREV_MONTHS = 1
  try:
    snap = _snap([_dept('d1', 'A1', [
      _item(12, '3.8.7', '视频监控录像查询记录单', 'flat',
            [_period_file('p_prev', prev_month), _period_file('tpl', '')],
            judge='new'),
    ])], period=cur)
    item = evaluate(snap, None)['items'][0]
    assert item['status'] == STATUS_UPDATED, item['status']
    assert item['expected_period'] == prev_month, item['expected_period']
  finally:
    settings.DOC_UPDATE_NEW_ACCEPT_PREV_MONTHS = saved


def test_flat_new_item_without_periods_lists_all_and_uses_fid():
  """回归：扁平查新增项一个期号都解析不出时，仍全量列出、按 fid 判。"""
  snap = _snap([_dept('d1', 'A1', [
    _item(5, '1.6.3.2', '整定表', 'flat',
          [_period_file('f1', ''), _period_file('f2', '')], judge='new'),
  ])])
  item = evaluate(snap, None)['items'][0]
  assert item['status'] == STATUS_BASELINE, item['status']
  assert [f['fid'] for f in item['files']] == ['f1', 'f2'], '无期号时原样全列'
  assert item['file_count'] == 2 and item['matched_count'] == 2

  prev = _snap([_dept('d1', 'A1', [
    _item(5, '1.6.3.2', '整定表', 'flat', [_period_file('f1', '')], judge='new'),
  ])], period='2026-09')
  cur = _snap([_dept('d1', 'A1', [
    _item(5, '1.6.3.2', '整定表', 'flat',
          [_period_file('f1', ''), _period_file('f2', '')], judge='new'),
  ])])
  item = evaluate(cur, prev)['items'][0]
  assert item['status'] == STATUS_UPDATED, '出现上期没有的 fid 即算已交'
  assert [f['fid'] for f in item['files']] == ['f1', 'f2']


def test_period_item_stale_when_only_older_period():
  snap = _snap([_dept('d1', 'A1', [
    _item(2, '1.4.5', '能源月报', 'period',
          [_period_file('f1', '2026-09')], latest='2026-09'),
  ])])
  verdict = evaluate(snap, None)
  assert _status(verdict, 'A1', 2) == STATUS_STALE
  assert verdict['items'][0]['latest_period'] == '2026-09', '页面要能写「最新为 2026-09」'


def test_period_item_never_baseline():
  # 查新增（路径有月份期号）不依赖上期快照：首轮就能给真实结论
  snap = _snap([_dept('d1', 'A1', [
    _item(2, '1.4.5', '能源月报', 'period', [_period_file('f1', EXPECTED)]),
  ])])
  verdict = evaluate(snap, None)
  assert verdict['baseline'] is True
  assert _status(verdict, 'A1', 2) == STATUS_UPDATED


def test_period_item_missing_when_no_files():
  snap = _snap([_dept('d1', 'A1', [
    _item(2, '1.4.5', '能源月报', 'period', [], error='未查询到相关文件'),
  ])])
  assert _status(evaluate(snap, None), 'A1', 2) == STATUS_MISSING


def test_flat_item_baseline_without_previous():
  snap = _snap([_dept('d1', 'A1', [
    _item(5, '1.6.3.2', '整定表', 'flat', [_flat_file('f9', 'abc')]),
  ])])
  assert _status(evaluate(snap, None), 'A1', 5) == STATUS_BASELINE


def test_flat_item_updated_and_stale_against_previous():
  cur = _snap([_dept('d1', 'A1', [
    _item(5, '1.6.3.2', '整定表', 'flat', [_flat_file('f9', 'new')]),
  ])])
  prev = _snap([_dept('d1', 'A1', [
    _item(5, '1.6.3.2', '整定表', 'flat', [_flat_file('f9', 'old')]),
  ])], period='2026-10')
  verdict = evaluate(cur, prev)
  assert verdict['mode'] == 'cross'
  assert _status(verdict, 'A1', 5) == STATUS_UPDATED

  same = _snap([_dept('d1', 'A1', [
    _item(5, '1.6.3.2', '整定表', 'flat', [_flat_file('f9', 'old')]),
  ])])
  assert _status(evaluate(same, prev), 'A1', 5) == STATUS_STALE


def test_flat_new_file_counts_as_updated():
  cur = _snap([_dept('d1', 'A1', [
    _item(5, '1.6.3.2', '整定表', 'flat', [_flat_file('f_new', 'abc')]),
  ])])
  prev = _snap([_dept('d1', 'A1', [
    _item(5, '1.6.3.2', '整定表', 'flat', [_flat_file('f_old', 'abc')]),
  ])], period='2026-10')
  assert _status(evaluate(cur, prev), 'A1', 5) == STATUS_UPDATED


def test_flat_download_failure_is_failed_not_stale():
  snap = _snap([_dept('d1', 'A1', [
    _item(5, '1.6.3.2', '整定表', 'flat', [_flat_file('f9', '')]),
  ])])
  assert _status(evaluate(snap, None), 'A1', 5) == STATUS_FAILED


def test_dept_error_marks_items_failed():
  snap = _snap([_dept('d1', 'A1', [
    _item(2, '1.4.5', '能源月报', 'period', [], error='部门详情获取失败'),
  ], error='部门详情获取失败：TimeoutError')])
  verdict = evaluate(snap, None)
  assert _status(verdict, 'A1', 2) == STATUS_FAILED
  assert 'TimeoutError' in verdict['items'][0]['error']
  assert verdict['depts'][0]['error']


def test_legacy_tree_snapshot_is_not_a_baseline_reference():
  # 上期是旧单树快照（没有 depts），查更新项不能拿它比 → 仍算基线
  legacy = {'period_key': '2026-10', 'period_label': '2026年10月',
            'taken_at': NOW, 'items': [
              {'seq': 5, 'number': '1.6.3.2', 'name': '整定表',
               'files': [{'fid': 'f9', 'sha256': 'old'}]},
            ]}
  cur = _snap([_dept('d1', 'A1', [
    _item(5, '1.6.3.2', '整定表', 'flat', [_flat_file('f9', 'new')]),
  ])])
  assert _status(evaluate(cur, legacy), 'A1', 5) == STATUS_BASELINE


def test_new_dept_is_baseline_not_updated():
  """上期没有的部门（新公司/新部门）：查更新项记基线，不能误报「已更新」。"""
  cur = _snap([
    _dept('d1', 'A1', [_item(5, '1.6.3.2', '整定表', 'flat', [_flat_file('f9', 'new')])],
          company_id='c1', company_name='廊坊'),
    _dept('p1', 'A1', [_item(5, '1.6.3.2', '整定表', 'flat', [_flat_file('f9', 'new')])],
          company_id='c2', company_name='平湖'),
  ])
  prev = _snap([
    _dept('d1', 'A1', [_item(5, '1.6.3.2', '整定表', 'flat', [_flat_file('f9', 'old')])],
          company_id='c1', company_name='廊坊'),
  ], period='2026-10')
  verdict = evaluate(cur, prev)
  assert _status(verdict, 'A1', 5, 'c1') == STATUS_UPDATED
  assert _status(verdict, 'A1', 5, 'c2') == STATUS_BASELINE, '同名的另一家公司是新部门'
  by_company = {d['company_id']: d for d in verdict['depts']}
  assert by_company['c2']['baseline'] is True


def test_new_dept_period_item_still_judged():
  cur = _snap([
    _dept('d1', 'A1', [_item(2, '1.4.5', '能源月报', 'period',
                            [_period_file('f3', EXPECTED)])],
          company_id='c1', company_name='廊坊'),
    _dept('p1', 'A1', [_item(2, '1.4.5', '能源月报', 'period',
                            [_period_file('f1', EXPECTED)])],
          company_id='c2', company_name='平湖'),
  ])
  prev = _snap([_dept('d1', 'A1', [
    _item(2, '1.4.5', '能源月报', 'period', [_period_file('f2', '2026-09')]),
  ], company_id='c1', company_name='廊坊')], period='2026-10')
  verdict = evaluate(cur, prev)
  assert _status(verdict, 'A1', 2, 'c2') == STATUS_UPDATED, '查新增（有月份期号）不依赖上期'
  text = summary_text(verdict)
  assert '2 家公司' in text


def test_dept_summary_and_text():
  snap = _snap([
    _dept('d1', 'A1', [
      _item(2, '1.4.5', '能源月报', 'period', [_period_file('f1', EXPECTED)]),
      _item(5, '1.6.3.2', '整定表', 'flat', [_flat_file('f9', 'abc')]),
    ]),
    _dept('d2', 'A2', [
      _item(2, '1.4.5', '能源月报', 'period', [], error='未查询到相关文件'),
    ]),
  ])
  verdict = evaluate(snap, None)
  assert verdict['total'] == 3 and verdict['dept_count'] == 2
  by_name = {d['dept_name']: d for d in verdict['depts']}
  assert by_name['A1']['counts'][STATUS_UPDATED] == 1
  assert by_name['A1']['counts'][STATUS_BASELINE] == 1
  assert by_name['A2']['counts'][STATUS_MISSING] == 1
  text = summary_text(verdict)
  assert '2 个部门' in text
  assert '已更新 1' in text and '未找到 1' in text and '基线 1' in text


def test_subset_verdict_keeps_dept_dimension():
  from service.doc_update.workflow import subset_verdict

  snap = _snap([
    _dept('d1', 'A1', [
      _item(2, '1.4.5', '能源月报', 'period', [_period_file('f1', EXPECTED)]),
      _item(5, '1.6.3.2', '整定表', 'flat', [_flat_file('f9', 'abc')]),
    ]),
    _dept('d2', 'A2', [
      _item(2, '1.4.5', '能源月报', 'period', [], error='未查询到相关文件'),
    ]),
  ])
  subset = subset_verdict(evaluate(snap, None), {2})
  assert subset['total'] == 2
  assert {d['dept_name'] for d in subset['depts']} == {'A1', 'A2'}
  assert all(i['seq'] == 2 for i in subset['items'])


def test_merge_dept_snapshot_by_dept_and_seq():
  base = _snap([
    _dept('d1', 'A1', [
      _item(2, '1.4.5', '能源月报', 'period', [_period_file('f1', EXPECTED)]),
      _item(5, '1.6.3.2', '整定表', 'flat', [_flat_file('f9', 'old')]),
    ]),
    _dept('d2', 'A2', [_item(2, '1.4.5', '能源月报', 'period', [])]),
  ])
  patch = _snap([_dept('d1', 'A1', [
    _item(2, '1.4.5', '能源月报', 'period', [_period_file('f2', '2026-11')]),
  ])])
  out = merge_snapshot(base, patch)
  by_dept = {d['dept_name']: d for d in out['depts']}
  assert by_dept['A1']['items'][0]['files'][0]['fid'] == 'f2', '命中项整项替换'
  assert by_dept['A1']['items'][1]['files'][0]['sha256'] == 'old', '其余项不动'
  assert 'A2' in by_dept, '未刷新的部门原样保留'


def test_actual_dirs_passthrough():
  """实际命中的目录名要带到判定结果（页面「实际目录」用）。"""
  snap = _snap([_dept('d1', 'A1', [
    _item(2, '1.4.5', '能源月报', 'period',
          [_period_file('f1', EXPECTED)], latest=EXPECTED,
          actual_dirs=['1.4.5能源管理报告（能源月报同步）', '8.1.2.3能源管理月报']),
  ])])
  verdict = evaluate(snap, None)
  assert verdict['items'][0]['actual_dirs'] == [
    '1.4.5能源管理报告（能源月报同步）', '8.1.2.3能源管理月报',
  ]


def test_known_numbers_cover_actual_folder_numbers():
  """剪枝目标 = 清单编号 ∪ 实际目录名里的编号，否则 8.1.2.3 走不到。"""
  from service.doc_update import share

  numbers = share.known_numbers()
  assert {'1.4.5', '1.2.3', '8.1.2.3'} <= set(numbers)
  assert share._compatible('8', numbers)
  assert share._compatible('8.1.2', numbers)
  assert not share._compatible('8.1.2.1', numbers)
  assert not share._compatible('9.9', numbers)


def test_year_folder_not_pruned_and_matched():
  """四位年份归档目录不参与剪枝；能源月报在 …/能源月报/2026年/10月 下必须能命中。"""
  from service.doc_update import catalog, fetch, period, share

  assert share._is_period_dir('2026')
  assert share._is_period_dir('2026.10')
  assert not share._is_period_dir('1.4')
  assert not share._is_period_dir('8.1.2')
  assert not share._is_period_dir('9.9')

  item = next(i for i in catalog.MONTHLY_ITEMS if i.seq == 2)
  base = {
    'fid': 'f1', 'topic': '能源报告2026年10月.pptx', 'parent_id': 'dir',
    'path': '1.规划管理 / 1.4精益化能源管理 / 1.4.5能源管理报告（能源月报同步）'
            ' / 能源月报 / 2026年 / 10月',
    'segments': ['1.规划管理', '1.4精益化能源管理',
                 '1.4.5能源管理报告（能源月报同步）', '能源月报', '2026年', '10月'],
    'create_time': 0, 'size': 100,
  }
  hits = fetch.match_item(item, [base])
  assert len(hits) == 1, hits
  assert hits[0]['match_dir'] == '能源月报', hits[0]['match_dir']
  assert period.detect_layout(hits) == period.LAYOUT_PERIOD

  # 通用别名：目录不带编号写法也能命中
  bare = dict(base, fid='f2', segments=['能源月报', '2026年', '9月'])
  assert len(fetch.match_item(item, [bare])) == 1


def test_energy_name_period_formats():
  """能源月报期号常只写在文件名里：覆盖实测到的各部门写法。"""
  from service.doc_update import period

  cases = {
    '能源报告【2026年10月总第128期】.pptx': '2026-10',
    '2026年10月度能源报告.xlsx': '2026-10',
    '润泽国际信息港A-3能源报告-2026-10.pdf': '2026-10',
    '廊坊A1能源报告-2026-10月（新版）.pdf': '2026-10',
    '能源报告（20260930)-9月.pdf': '2026-09',
    '能源报告【20261031】.pdf': '2026-10',
    '能源报告【202603】.pdf': '2026-03',
    '能源报告【202604】.xlsx': '2026-04',
    '能源报告2018-202604.xlsx': '2026-04',
    '10、2026年10月能源报告.xlsx': '2026-10',
    '2026年A18能源报告4.30-4月份.pdf': '2026-04',
    '2025年A6能源报告1.31-1月份.pdf': '2025-01',
    '(2026年10月）.pdf': '2026-10',
    '2026年9月惠州电量数据.pdf': '2026-09',
  }
  for name, want in cases.items():
    got = period.name_period(name)
    assert got == want, (name, got, want)
  assert period.name_period('能源报告-2025年.xlsx') == ''
  assert period.name_period('1、一月') == ''
  # 纯月份靠目录年份；`A-2 月度` 不能被当成 2 月
  assert period.name_period('润泽国际信息港 A-2 月度能源报告-10月.xlsx', 2026) == '2026-10'
  assert period.name_period('月度能源报告-12月.pdf', 2025) == '2025-12'


def test_energy_period_of_file_ignores_create_time():
  """2025 期的文件可能是 2026 年补传的：年份只能取目录，不能取 create_time。"""
  from datetime import datetime

  from service.doc_update import period

  ts = datetime(2026, 6, 1).timestamp() * 1000
  f = {'segments': ['1.4.5能源管理报告', '能源月报', '2025年'],
       'topic': '润泽国际信息港 A-2 月度能源报告-10月.xlsx',
       'create_time': ts}
  assert period.period_of_file(f) == '', '默认仍只看目录名'
  assert period.period_of_file(f, from_name=True) == '2025-10'

  # 目录能解析时以目录为准（比文件名可靠）
  g = {'segments': ['能源月报', '2026年', '10月'],
       'topic': '能源报告-09月.pdf', 'create_time': 0}
  assert period.period_of_file(g, from_name=True) == '2026-10'


def test_energy_catalog_period_only():
  from service.doc_update import catalog

  energy = next(i for i in catalog.MONTHLY_ITEMS if i.seq == 2)
  assert energy.period_only is True
  # 4.1.5 四条同理：目录只到年份，月份写在文件名里
  assert {i.seq for i in catalog.MONTHLY_ITEMS if i.period_only} == {2, 29, 30, 31, 32}


def test_segments_year_from_directory_names():
  from service.doc_update import period

  assert period._segments_year(['1.4.5能源管理报告', '2026年输出物']) == 2026
  assert period._segments_year(['2026年数据']) == 2026
  assert period._segments_year(['2026年度']) == 2026
  assert period._segments_year(['2026']) == 2026
  assert period._segments_year([]) is None


def test_energy_period_only_judged_without_hash():
  snap = _snap([_dept('d1', 'A1', [
    _item(2, '1.4.5', '能源月报', 'period',
          [_period_file('f1', EXPECTED)], latest=EXPECTED, period_only=True),
  ])])
  assert _status(evaluate(snap, None), 'A1', 2) == STATUS_UPDATED

  old = _snap([_dept('d1', 'A1', [
    _item(2, '1.4.5', '能源月报', 'period',
          [_period_file('f1', '2026-09')], latest='2026-09', period_only=True),
  ])])
  assert _status(evaluate(old, None), 'A1', 2) == STATUS_STALE


def test_energy_unparsed_files_fail_with_reason():
  snap = _snap([_dept('d1', 'A1', [
    _item(2, '1.4.5', '能源月报', 'period',
          [_period_file('f1', '')], period_only=True),
  ])])
  verdict = evaluate(snap, None)
  item = verdict['items'][0]
  assert item['status'] == STATUS_FAILED, '不能含糊成「未更新」'
  assert '解析出期号' in item['error']
  assert item['files'][0]['status'] == STATUS_FAILED


def test_energy_stray_unparsed_file_does_not_flip_item():
  snap = _snap([_dept('d1', 'A1', [
    _item(2, '1.4.5', '能源月报', 'period',
          [_period_file('f1', EXPECTED), _period_file('f2', '')],
          latest=EXPECTED, period_only=True),
  ])])
  verdict = evaluate(snap, None)
  assert _status(verdict, 'A1', 2) == STATUS_UPDATED
  by_fid = {f['fid']: f for f in verdict['items'][0]['files']}
  assert by_fid['f1']['status'] == STATUS_UPDATED
  assert by_fid['f2']['status'] == STATUS_FAILED


def test_plain_period_item_keeps_old_stale_behavior():
  """其余 24 项不变：无期号的文件仍按「未更新」处理。"""
  snap = _snap([_dept('d1', 'A1', [
    _item(2, '1.4.5', '能源月报', 'period',
          [_period_file('f1', EXPECTED), _period_file('f2', '')]),
  ])])
  verdict = evaluate(snap, None)
  by_fid = {f['fid']: f for f in verdict['items'][0]['files']}
  assert by_fid['f2']['status'] == STATUS_STALE


def test_catalog_aliases_registered_items():
  """登记 `folders` 别名的项：新增别名要在这里显式登记。"""
  from service.doc_update import catalog

  item19 = next(i for i in catalog.MONTHLY_ITEMS if i.seq == 19)
  assert '客户IT库房应急卡' in item19.folders
  assert '客户库房应急卡' in item19.folders
  # A1 的钉点是章节目录（只有编号命名的模板），真实记录在无编号目录：
  # `卫生清洁记录2026/10月`、`2026年/货物运输通道使用记录单/10月`
  item24 = next(i for i in catalog.MONTHLY_ITEMS if i.seq == 24)
  item27 = next(i for i in catalog.MONTHLY_ITEMS if i.seq == 27)
  assert item24.folders == ('卫生清洁记录',)
  assert item27.folders == ('货物运输通道使用记录单',)
  assert not [i for i in catalog.MONTHLY_ITEMS
              if i.seq not in (2, 19, 24, 27) and i.folders], \
    '新增别名要在这里登记'


def test_item19_matches_unnumbered_folder():
  from service.doc_update import catalog, fetch

  item19 = next(i for i in catalog.MONTHLY_ITEMS if i.seq == 19)
  a1 = ['廊坊-A1-数据中心运维管理体系', '2.运行维护管理', '2.3值班管理',
        '2.3.2数据中心监控室管理', '客户IT库房应急卡领取记录表', '2026年', '10月']
  f = {'fid': 'f1', 'topic': 'IT库房应急卡.pdf', 'parent_id': 'd',
       'path': ' / '.join(a1), 'segments': a1, 'create_time': 0, 'size': 1}
  hits = fetch.match_item(item19, [f])
  assert len(hits) == 1
  assert hits[0]['match_dir'] == '客户IT库房应急卡领取记录表'

  # A10 形态：无编号目录挂在年份目录下（2026年 不参与编号剪枝）
  a10 = ['廊坊-A10-数据中心运维管理体系', '2.运行维护管理', '2.3值班管理',
         '2.3.2数据中心监控室管理', '2026年', '客户IT库房应急卡领取登记表']
  g = {'fid': 'f2', 'topic': '2026年10月-客户IT库房应急卡领取登记表.pdf',
       'parent_id': 'd', 'path': ' / '.join(a10), 'segments': a10,
       'create_time': 0, 'size': 1}
  hits10 = fetch.match_item(item19, [g])
  assert len(hits10) == 1
  assert hits10[0]['match_dir'] == '客户IT库房应急卡领取登记表'


def test_item19_not_confused_with_item15():
  """2.3.2.2 的「应急卡使用登记表」不能被 2.3.2.6 的别名吃进来。"""
  from service.doc_update import catalog, fetch

  item19 = next(i for i in catalog.MONTHLY_ITEMS if i.seq == 19)
  item15 = next(i for i in catalog.MONTHLY_ITEMS if i.seq == 15)
  segs = ['廊坊-A8-数据中心运维管理体系', '2.运行维护管理', '2.3值班管理',
          '2.3.2数据中心监控室管理', '2.3.2.2润泽科技-廊坊-A8-应急卡使用登记表',
          '2026年', '10月']
  f = {'fid': 'f1', 'topic': '10月应急卡使用登记表.pdf', 'parent_id': 'd',
       'path': ' / '.join(segs), 'segments': segs, 'create_time': 0, 'size': 1}
  assert fetch.match_item(item19, [f]) == []
  assert len(fetch.match_item(item15, [f])) == 1


def test_month_folder_not_pruned():
  """月份目录（10月/10月份）是期号容器，不能被当成章节编号剪掉。"""
  from service.doc_update import share

  for name in ('10月', '10月份', '10 月', '9月', '12月份隐患排查'):
    assert share._is_period_name(name), name
  for name in ('4.1.5隐患排查治理', '2.3值班管理', '班组隐患排查',
               '2026年隐患排查记录表'):
    assert not share._is_period_name(name), name

  numbers = share.known_numbers()
  assert share._should_prune('10月', numbers) is False
  assert share._should_prune('2026', numbers) is False
  assert share._should_prune('9.9', numbers) is True
  assert share._should_prune('班组隐患排查', numbers) is False


def test_walk_descends_into_month_folder_and_matches_item32():
  """A1 实测路径 …/4.1.5隐患排查治理/2026年隐患排查记录表/10月 必须被走到并命中。"""
  import asyncio

  from service.doc_update import catalog, fetch, period, share

  oct_ts = 1790812800000          # 2026-10-01（毫秒）
  tree = {
    'root': [{'fid': 'd4', 'name': '4.EHS管理', 'is_dir': True,
              'create_time': 0, 'size': 0}],
    'd4': [{'fid': 'd415', 'name': '4.1.5隐患排查治理', 'is_dir': True,
            'create_time': 0, 'size': 0}],
    'd415': [{'fid': 'dy', 'name': '2026年隐患排查记录表', 'is_dir': True,
              'create_time': 0, 'size': 0}],
    'dy': [{'fid': 'd9', 'name': '9月', 'is_dir': True,
            'create_time': 0, 'size': 0},
           {'fid': 'd10', 'name': '10月', 'is_dir': True,
            'create_time': 0, 'size': 0}],
    'd9': [{'fid': 'f9', 'name': '班组级隐患排查表.pdf', 'is_dir': False,
            'create_time': oct_ts - 30 * 86400000, 'size': 1}],
    'd10': [{'fid': 'f10', 'name': '班组级隐患排查表.pdf', 'is_dir': False,
             'create_time': oct_ts, 'size': 1}],
  }

  async def fake_list_dir(dir_id):
    return [dict(r) for r in tree.get(dir_id, [])]

  original = share.list_dir
  share.list_dir = fake_list_dir
  try:
    files = asyncio.run(share.walk('root', share.known_numbers()))
  finally:
    share.list_dir = original

  assert len(files) == 2, files
  assert [f for f in files if f['segments'][-1] == '10月'], files

  item32 = next(i for i in catalog.MONTHLY_ITEMS if i.seq == 32)
  hits = fetch.match_item(item32, files)
  assert len(hits) == 2, hits
  assert hits[0]['match_dir'] == '4.1.5隐患排查治理'
  assert period.detect_layout(hits) == period.LAYOUT_PERIOD
  assert period.pick_latest_period(hits) == '2026-10'


def test_a1_branch_team_alias_in_pinned_subtree():
  """A1 4.1.5：共享钉点 + 文件名不带专业词，靠按部门文件名别名分派（业务文档 2026-11-17）。"""
  from service.doc_update import catalog, fetch, paths

  root = '4.1.5隐患排查治理'
  segs = ['4.EHS管理', '4.1安全管理', root, '2026年隐患排查记录表', '10月']
  chain = [{'fid': f'c{i}', 'name': s} for i, s in enumerate(segs)]

  def rec(fid, topic):
    return {'fid': fid, 'topic': topic, 'chain': chain, 'segments': segs,
            'path': ' / '.join(segs), 'create_time': 0, 'size': 1}

  files = [rec('fb', '部门隐患排查表.pdf'), rec('ft', '班组级隐患排查表.pdf')]
  cfg_root = {'seq': 29, 'fid': 'c2', 'segments': [root]}
  names = paths.item_names('c1', '廊坊', 'A1')
  items = {i.seq: i for i in catalog.MONTHLY_ITEMS}
  for seq in (29, 30, 31):
    assert fetch._match_files(items[seq], files, cfg_root, configured=True) == [], seq
    got = fetch._match_files(items[seq], files, cfg_root, configured=True,
                             extra_names=names[seq])
    assert [f['fid'] for f in got] == ['fb'], seq
  got = fetch._match_files(items[32], files, cfg_root, configured=True,
                           extra_names=names[32])
  assert [f['fid'] for f in got] == ['ft']


def test_walk_reports_progress_and_flags_truncation():
  """遍历要回报进度；触顶必须标 `truncated` —— 静默截断会把「有文件」误判成「未找到」。"""
  import asyncio

  from service.doc_update import share

  tree = {'root': [
    {'fid': f'sub{i}', 'name': f'班组{i}', 'is_dir': True, 'create_time': 0, 'size': 0}
    for i in range(5)
  ]}
  for i in range(5):
    tree[f'sub{i}'] = [
      {'fid': f'f{i}{j}', 'name': f'10月报告{i}{j}.pdf', 'is_dir': False,
       'create_time': 0, 'size': 1}
      for j in range(2)
    ]

  async def fake_list_dir(dir_id):
    return [dict(r) for r in tree.get(dir_id, [])]

  original_list, original_limits = share.list_dir, share._limits
  share.list_dir = fake_list_dir
  share._limits = lambda: (8, 3, 16)     # 目录数阀值压到 3，必触顶
  calls: list[tuple[int, int, int]] = []
  stats: dict = {}
  try:
    files = asyncio.run(share.walk(
      'root', (), on_progress=lambda d, f, q: calls.append((d, f, q)), stats=stats,
    ))
  finally:
    share.list_dir, share._limits = original_list, original_limits

  assert stats['truncated'] is True, stats
  assert stats['dirs'] == 3, stats        # root + 2 个子目录
  assert stats['files'] == len(files) == 4, (stats, len(files))
  # 收尾那次队列必为 0（页面据此把遍历阶段算到 99% → 100%）
  assert calls and calls[-1] == (3, 4, 0), calls
  # 并发完成顺序不确定，输出必须自己排稳定
  assert files == sorted(files, key=lambda f: (f['path'], f['topic'], f['fid']))


def test_catalog_judge_follows_pdf_note():
  """第三列写「线上更新」= 查更新，其余（PDF/PPT/文档）= 查新增。"""
  from service.doc_update import catalog

  update_seqs = {i.seq for i in catalog.MONTHLY_ITEMS if i.note == '线上更新'}
  assert update_seqs == {1, 3, 4, 6, 34, 35}, update_seqs
  for item in catalog.MONTHLY_ITEMS:
    want = catalog.JUDGE_UPDATE if item.note == '线上更新' else catalog.JUDGE_NEW
    assert item.judge == want, (item.seq, item.note, item.judge)
  item32 = next(i for i in catalog.MONTHLY_ITEMS if i.seq == 32)
  assert item32.note == 'PDF' and item32.judge == catalog.JUDGE_NEW
  item2 = next(i for i in catalog.MONTHLY_ITEMS if i.seq == 2)
  assert item2.judge == catalog.JUDGE_NEW, '能源月报（PPT）也是查新增'


def test_update_item_hashed_even_in_month_folder():
  """线上更新项与目录形态无关：路径有月份目录也按内容哈希判，不看期号。"""
  cur = _snap([_dept('d1', 'A1', [
    _item(1, '1.2.2', '负载管理统计分析表', 'period',
          [_flat_file('f1', 'new')], latest='2026-09', judge='update'),
  ])])
  prev = _snap([_dept('d1', 'A1', [
    _item(1, '1.2.2', '负载管理统计分析表', 'period',
          [_flat_file('f1', 'old')], latest='2026-09', judge='update'),
  ])], period='2026-10')
  verdict = evaluate(cur, prev)
  assert _status(verdict, 'A1', 1) == STATUS_UPDATED, '期号停在 2026-09 也要按哈希判'
  assert verdict['items'][0]['files'][0]['status'] == STATUS_UPDATED

  same = _snap([_dept('d1', 'A1', [
    _item(1, '1.2.2', '负载管理统计分析表', 'period',
          [_flat_file('f1', 'old')], latest='2026-09', judge='update'),
  ])])
  assert _status(evaluate(same, prev), 'A1', 1) == STATUS_STALE


def test_new_item_in_month_folder_judged_by_period():
  """查新增项有月份期号：本期应到期已交即已更新，不下载。"""
  cur = _snap([_dept('d1', 'A1', [
    _item(32, '4.1.5', '班组级隐患排查记录表', 'period',
          [_period_file('f10', EXPECTED)], latest=EXPECTED, judge='new'),
  ])])
  assert _status(evaluate(cur, None), 'A1', 32) == STATUS_UPDATED

  old = _snap([_dept('d1', 'A1', [
    _item(32, '4.1.5', '班组级隐患排查记录表', 'period',
          [_period_file('f9', '2026-09')], latest='2026-09', judge='new'),
  ])])
  assert _status(evaluate(old, None), 'A1', 32) == STATUS_STALE


def test_new_item_flat_judged_by_new_fid():
  """查新增项没有期号：出现上期没有的文件即已交，同 fid 即未交；不下载不算失败。"""
  prev = _snap([_dept('d1', 'A1', [
    _item(32, '4.1.5', '班组级隐患排查记录表', 'flat',
          [_period_file('f_old', '')], judge='new'),
  ])], period='2026-10')

  cur_new = _snap([_dept('d1', 'A1', [
    _item(32, '4.1.5', '班组级隐患排查记录表', 'flat',
          [_period_file('f_old', ''), _period_file('f_new', '')], judge='new'),
  ])])
  verdict = evaluate(cur_new, prev)
  assert _status(verdict, 'A1', 32) == STATUS_UPDATED
  by_fid = {f['fid']: f for f in verdict['items'][0]['files']}
  assert by_fid['f_old']['status'] == STATUS_STALE
  assert by_fid['f_new']['status'] == STATUS_UPDATED
  assert all(f['status'] != STATUS_FAILED for f in by_fid.values()), \
    '查新增项没有 sha256 不能判下载失败'

  cur_same = _snap([_dept('d1', 'A1', [
    _item(32, '4.1.5', '班组级隐患排查记录表', 'flat',
          [_period_file('f_old', '')], judge='new'),
  ])])
  same_verdict = evaluate(cur_same, prev)
  assert _status(same_verdict, 'A1', 32) == STATUS_STALE
  assert same_verdict['items'][0]['files'][0]['status'] == STATUS_STALE


def test_new_item_flat_baseline_without_previous():
  """查新增项没有期号、又没有上期快照：只建基线，不猜「未更新」。"""
  snap = _snap([_dept('d1', 'A1', [
    _item(32, '4.1.5', '班组级隐患排查记录表', 'flat',
          [_period_file('f1', '')], judge='new'),
  ])])
  verdict = evaluate(snap, None)
  assert _status(verdict, 'A1', 32) == STATUS_BASELINE
  assert verdict['items'][0]['files'][0]['status'] == STATUS_BASELINE


def test_changes_changed_lists_only_update_items():
  """「本期变更」只列查更新项（有哈希变化时间），查新增项没有哈希不进榜。"""
  cur = _snap([_dept('d1', 'A1', [
    _item(1, '1.2.2', '负载管理统计分析表', 'flat',
          [_flat_file('f1', 'new')], judge='update'),
    _item(32, '4.1.5', '班组级隐患排查记录表', 'flat',
          [_period_file('f8', '')], judge='new'),
  ])])
  prev = _snap([_dept('d1', 'A1', [
    _item(1, '1.2.2', '负载管理统计分析表', 'flat',
          [_flat_file('f1', 'old')], judge='update'),
    _item(32, '4.1.5', '班组级隐患排查记录表', 'flat',
          [_period_file('f7', '')], judge='new'),
  ])], period='2026-10')
  verdict = evaluate(cur, prev)
  assert _status(verdict, 'A1', 32) == STATUS_UPDATED
  changed = verdict['changes']['changed']
  assert [c['number'] for c in changed] == ['1.2.2'], changed


def test_old_snapshot_without_judge_keeps_old_semantics():
  """老快照没有 `judge` 字段：按当时形态推断（period=查新增，flat=查更新）。"""
  snap = _snap([_dept('d1', 'A1', [
    _item(5, '1.6.3.2', '整定表', 'flat', [_flat_file('f9', '')]),
  ])])
  verdict = evaluate(snap, None)
  assert verdict['items'][0]['judge'] == 'update'
  assert _status(verdict, 'A1', 5) == STATUS_FAILED, '老口径 flat 无哈希仍是下载失败'

  period_snap = _snap([_dept('d1', 'A1', [
    _item(2, '1.4.5', '能源月报', 'period', [_period_file('f1', EXPECTED)]),
  ])])
  period_verdict = evaluate(period_snap, None)
  assert period_verdict['items'][0]['judge'] == 'new'


def test_one_file_failure_does_not_abort_the_run():
  """单个文件处理失败只记自己那条，不影响其余文件与整轮落库。

  同时锁两件事：① 循环包了 try/except（否则一处异常整期丢弃）；
  ② `_flat_record` 不硬依赖下载结果里的 `warn`（share 下载器曾漏这个键）。
  """
  import asyncio

  from service.doc_update import catalog, depts, fetch, share

  item = catalog.DocItem(99, ('9.9.9',), '测试表', '月度', '线上更新')
  files = [
    {'fid': 'f1', 'topic': '9.9.9测试表A.pdf', 'path': '9.9.9测试表 / A',
     'segments': ['9.9.9测试表'], 'parent_id': 'd', 'create_time': 0, 'size': 10},
    {'fid': 'f2', 'topic': '9.9.9测试表B.pdf', 'path': '9.9.9测试表 / B',
     'segments': ['9.9.9测试表'], 'parent_id': 'd', 'create_time': 0, 'size': 10},
  ]

  async def fake_depts(*_args, **_kwargs):
    return [{'company_id': 'c1', 'company_name': '廊坊', 'dept_id': 'd1',
             'dept_name': 'A1', 'root_fid': 'root'}]

  async def fake_walk(*_args, **_kwargs):
    return [dict(f) for f in files]

  async def fake_download(fid):
    if fid == 'f1':
      raise RuntimeError('boom')
    # 真实 share.download_sha256 的形状：没有 warn
    return {'sha256': 'abc', 'download_size': 10, 'url': '', 'error': ''}

  async def fake_load_index(*_args, **_kwargs):
    return None

  async def fake_save_index(*_args, **_kwargs):
    return None

  originals = (depts.resolve_departments, share.walk, share.download_sha256,
               fetch._load_index, fetch._save_index)
  depts.resolve_departments = fake_depts
  share.walk = fake_walk
  share.download_sha256 = fake_download
  fetch._load_index = fake_load_index
  fetch._save_index = fake_save_index
  try:
    snap = asyncio.run(fetch.take_dept_snapshot(only=[item]))
  finally:
    (depts.resolve_departments, share.walk, share.download_sha256,
     fetch._load_index, fetch._save_index) = originals

  item_out = snap['depts'][0]['items'][0]
  # 期间与应到期必须同源（页面期间选 11 月就查 11 月报告）
  assert item_out['expected_period'] == snap['period_key'], snap['period_key']
  out = item_out['files']
  assert len(out) == 2, out
  by_fid = {f['fid']: f for f in out}
  assert by_fid['f1']['sha256'] == '' and 'boom' in by_fid['f1']['error']
  assert by_fid['f2']['sha256'] == 'abc' and by_fid['f2']['error'] == ''

  evaluated = {f['fid']: f for f in evaluate(snap, None)['items'][0]['files']}
  assert evaluated['f1']['status'] == STATUS_FAILED
  assert evaluated['f2']['status'] == STATUS_BASELINE, '其余文件照常判定，不被失败文件带偏'


def test_dept_progress_counts_walk_steps():
  """遍历阶段也要推进百分比：遍历与逐项同权，按「已访问/(已访问+队列)」推进且不回退。

  否则一个部门遍历 2~10 分钟、`done` 一动不动，用户看到的就是「没有百分比」。
  """
  import asyncio

  from service.doc_update import catalog, depts, fetch, share

  calls: list[tuple[int, int, str, str]] = []

  async def fake_depts(*_args, **_kwargs):
    return [{'company_id': 'c1', 'company_name': '廊坊', 'dept_id': 'd1',
             'dept_name': 'ZZ', 'root_fid': 'root'}]

  async def fake_walk(_root, _numbers, *, on_progress=None, stats=None):
    # 未知子目录被发现时队列变长，分母跟着「当前已知目录数」走
    await on_progress(2, 5, 8)
    await on_progress(10, 20, 0)
    return []

  async def fake_load_index(*_args, **_kwargs):
    return None

  async def fake_save_index(*_args, **_kwargs):
    return None

  def on_progress(done, total, current, phase):
    calls.append((done, total, current, phase))

  originals = (depts.resolve_departments, share.walk, share.walk_roots,
               fetch._load_index, fetch._save_index)
  depts.resolve_departments = fake_depts
  share.walk = fake_walk
  fetch._load_index = fake_load_index
  fetch._save_index = fake_save_index
  try:
    asyncio.run(fetch.take_dept_snapshot(progress=on_progress))
  finally:
    (depts.resolve_departments, share.walk, share.walk_roots,
     fetch._load_index, fetch._save_index) = originals

  total = len(catalog.MONTHLY_ITEMS) * 2   # 每部门：遍历 25 步 + 逐项 25 步
  assert calls[0] == (0, total, '廊坊 · ZZ · 正在遍历目录', 'walk'), calls[0]
  walk_calls = [c for c in calls if c[3] == 'walk']
  assert len(walk_calls) >= 3, walk_calls          # 起始 + 两次目录回调
  assert all(0 <= c[0] < total for c in walk_calls[1:]), walk_calls
  assert walk_calls[0][0] <= walk_calls[1][0] <= walk_calls[2][0], \
    f'遍历百分比不能倒退：{walk_calls}'
  assert '已访问 2 个目录 / 5 个文件' in walk_calls[1][2], walk_calls[1]
  assert calls[-1][0] == calls[-1][1] == total, calls[-1]
  assert calls[-1][3] == 'items', calls[-1]


def _fake_tree(tree, limits=None):
  """把 dict 树装成 `share.list_dir` 替身，返回 restore 函数。"""
  from service.doc_update import share

  async def fake_list_dir(dir_id):
    return [dict(r) for r in tree.get(dir_id, [])]

  original = (share.list_dir, share._limits)
  share.list_dir = fake_list_dir
  if limits:
    share._limits = lambda: limits

  def restore():
    share.list_dir, share._limits = original

  return restore


def test_walk_skips_recycle_bin():
  """回收站里的文件不算「本期提交」，不参与匹配。"""
  import asyncio

  from service.doc_update import share

  tree = {
    'root': [
      {'fid': 'dr', 'name': '回收站', 'is_dir': True, 'create_time': 0, 'size': 0},
      {'fid': 'dn', 'name': '1.4.5能源管理报告', 'is_dir': True,
       'create_time': 0, 'size': 0},
    ],
    'dr': [{'fid': 'f1', 'name': '能源报告2026年10月.pdf', 'is_dir': False,
            'create_time': 0, 'size': 1}],
    'dn': [{'fid': 'f2', 'name': '能源报告2026年10月.pdf', 'is_dir': False,
            'create_time': 0, 'size': 1}],
  }
  restore = _fake_tree(tree)
  try:
    files = asyncio.run(share.walk('root', share.known_numbers()))
  finally:
    restore()
  assert [f['fid'] for f in files] == ['f2'], files


def test_walk_returns_chain_and_index_picks_shallowest_dir():
  """文件记录带目录链；索引取最浅的别名目录（不取每月新建的叶子月目录）。"""
  import asyncio

  from service.doc_update import catalog, dir_index, fetch, share

  tree = {
    'root': [{'fid': 'd15', 'name': '1.4.5能源管理报告（能源月报同步）',
              'is_dir': True, 'create_time': 0, 'size': 0}],
    'd15': [{'fid': 'dy', 'name': '能源月报', 'is_dir': True,
             'create_time': 0, 'size': 0}],
    'dy': [{'fid': 'dm', 'name': '10月', 'is_dir': True,
            'create_time': 0, 'size': 0}],
    'dm': [{'fid': 'f1', 'name': '能源报告2026年10月.pdf', 'is_dir': False,
            'create_time': 0, 'size': 1}],
  }
  restore = _fake_tree(tree)
  try:
    files = asyncio.run(share.walk('root', share.known_numbers()))
  finally:
    restore()

  assert [c['fid'] for c in files[0]['chain']] == ['d15', 'dy', 'dm'], files[0]['chain']
  item = next(i for i in catalog.MONTHLY_ITEMS if i.seq == 2)
  roots = dir_index.roots_for_item(item, fetch.match_item(item, files))
  assert roots == [{'seq': 2, 'fid': 'd15',
                    'segments': ['1.4.5能源管理报告（能源月报同步）']}], roots


def test_index_reuse_skips_full_walk():
  """有索引时只列固定子树，不再从部门根全量遍历。"""
  import asyncio

  from datetime import datetime, timezone

  from service.doc_update import catalog, depts, fetch, share

  item = catalog.DocItem(99, ('9.9.9',), '测试表', '月度', 'PDF')
  file = {'fid': 'f1', 'topic': '9.9.9测试表A.pdf', 'path': '9.9.9测试表',
          'segments': ['9.9.9测试表'], 'parent_id': 'd', 'create_time': 0, 'size': 1,
          'chain': [{'name': '9.9.9测试表', 'fid': 'd9'}]}
  calls: list = []

  async def fake_depts(*_a, **_k):
    return [{'company_id': 'c1', 'company_name': '廊坊', 'dept_id': 'd1',
             'dept_name': 'A1', 'root_fid': 'root'}]

  async def fake_walk(*_a, **_k):
    calls.append('walk')
    return []

  async def fake_walk_roots(roots, *_a, **_k):
    calls.append(('roots', tuple(r['fid'] for r in roots)))
    return [dict(file)]

  async def fake_list_dir(_dir):
    return []

  async def fake_load_index(*_a, **_k):
    return {'roots': [{'seq': 99, 'fid': 'd9', 'segments': ['9.9.9测试表']}],
            'full_scan_at': datetime.now(timezone.utc)}

  async def fake_save_index(*_a, **_k):
    calls.append('save')

  originals = (depts.resolve_departments, share.walk, share.walk_roots,
               share.list_dir, fetch._load_index, fetch._save_index)
  depts.resolve_departments, share.walk = fake_depts, fake_walk
  share.walk_roots, share.list_dir = fake_walk_roots, fake_list_dir
  fetch._load_index, fetch._save_index = fake_load_index, fake_save_index
  try:
    snap = asyncio.run(fetch.take_dept_snapshot(only=[item]))
  finally:
    (depts.resolve_departments, share.walk, share.walk_roots,
     share.list_dir, fetch._load_index, fetch._save_index) = originals

  assert calls == [('roots', ('d9',))], calls
  assert len(snap['depts'][0]['items'][0]['files']) == 1


def test_index_falls_back_to_full_walk_when_roots_empty():
  """索引子树一个文件都没有 → 回退一次全量并刷新索引（距上次全量超过间隔才允许）。"""
  import asyncio

  from service.doc_update import catalog, depts, fetch, share

  item = catalog.DocItem(99, ('9.9.9',), '测试表', '月度', 'PDF')
  file = {'fid': 'f1', 'topic': '9.9.9测试表A.pdf', 'path': '9.9.9测试表',
          'segments': ['9.9.9测试表'], 'parent_id': 'd', 'create_time': 0, 'size': 1,
          'chain': [{'name': '9.9.9测试表', 'fid': 'd9'}]}
  calls: list = []

  async def fake_depts(*_a, **_k):
    return [{'company_id': 'c1', 'company_name': '廊坊', 'dept_id': 'd1',
             'dept_name': 'A1', 'root_fid': 'root'}]

  async def fake_walk(*_a, **_k):
    calls.append('walk')
    return [dict(file)]

  async def fake_walk_roots(*_a, **_k):
    calls.append('roots')
    return []

  async def fake_list_dir(_dir):
    return []

  async def fake_load_index(*_a, **_k):
    return {'roots': [{'seq': 99, 'fid': 'd9', 'segments': ['9.9.9测试表']}]}

  async def fake_save_index(*_a, **_k):
    calls.append('save')

  originals = (depts.resolve_departments, share.walk, share.walk_roots,
               share.list_dir, fetch._load_index, fetch._save_index)
  depts.resolve_departments, share.walk = fake_depts, fake_walk
  share.walk_roots, share.list_dir = fake_walk_roots, fake_list_dir
  fetch._load_index, fetch._save_index = fake_load_index, fake_save_index
  try:
    snap = asyncio.run(fetch.take_dept_snapshot(only=[item]))
  finally:
    (depts.resolve_departments, share.walk, share.walk_roots,
     share.list_dir, fetch._load_index, fetch._save_index) = originals

  assert calls == ['roots', 'walk', 'save'], calls
  assert len(snap['depts'][0]['items'][0]['files']) == 1


def test_period_item_stores_only_expected_files():
  """查新增 + 按期：只列本期/未来期文件，历史文件不落快照（判定口径不变）。"""
  from service.doc_update import fetch

  records = [
    _period_file('f1', '2024-08'), _period_file('f2', '2025-10'),
    _period_file('f3', '2026-09'), _period_file('f4', EXPECTED),
  ]
  kept = fetch._relevant_records(records, EXPECTED, 'new', 'period')
  assert [r['fid'] for r in kept] == ['f4'], kept
  assert len(records) == 4, '原始匹配结果不被修改'


def test_period_item_falls_back_to_latest_files():
  """一条本期都没有 → 保留最新一期（要能判「未更新」并显示最新期号）。"""
  from service.doc_update import fetch

  records = [_period_file('f1', '2026-08'), _period_file('f2', '2026-09')]
  kept = fetch._relevant_records(records, EXPECTED, 'new', 'period')
  assert [r['fid'] for r in kept] == ['f2'], kept

  # 查更新项与无期号项原样保留（哈希/fid 比对需要全部文件）
  flat = [_flat_file('f1', 'a'), _flat_file('f2', 'b')]
  assert fetch._relevant_records(flat, EXPECTED, 'update', 'period') == flat
  assert fetch._relevant_records(flat, EXPECTED, 'new', 'flat') == flat
  # 全部解析不出期号也原样保留（要能判 failed 并给原因）
  unparsed = [_period_file('f1', ''), _period_file('f2', '')]
  assert fetch._relevant_records(unparsed, EXPECTED, 'new', 'period') == unparsed


def test_index_climbs_period_dirs_to_stable_ancestor():
  """命中的是每月新建的归档目录时，上浮到稳定祖先（否则下月新目录永远不会被遍历）。"""
  from service.doc_update import catalog, dir_index

  item = next(i for i in catalog.MONTHLY_ITEMS if i.seq == 2)
  chain = [
    {'name': 'A1-M&O文件', 'fid': 'd1'},
    {'name': '6、数据中心运维报告模板', 'fid': 'd2'},
    {'name': '2、数据中心运维月报', 'fid': 'd3'},
    {'name': '2025', 'fid': 'd4'},
    {'name': '润泽国际信息港A-1能源报告【2025年1月总第107期】', 'fid': 'd5'},
  ]
  roots = dir_index.roots_for_item(item, [{'fid': 'f1', 'chain': chain}])
  assert roots == [{'seq': 2, 'fid': 'd3',
                    'segments': ['A1-M&O文件', '6、数据中心运维报告模板',
                                 '2、数据中心运维月报']}], roots

  # 「2025年考勤」按年轮转，也要上浮到稳定父目录
  item34 = next(i for i in catalog.MONTHLY_ITEMS if i.seq == 34)
  year_chain = [
    {'name': '6.人员配置及组织管理', 'fid': 'd1'},
    {'name': '6.3人员工时管理', 'fid': 'd2'},
    {'name': '2025年考勤', 'fid': 'd3'},
  ]
  roots = dir_index.roots_for_item(item34, [{'fid': 'f1', 'chain': year_chain}])
  assert roots == [{'seq': 34, 'fid': 'd2',
                    'segments': ['6.人员配置及组织管理', '6.3人员工时管理']}], roots

  # 期号目录本身就在部门根下（无处上浮）时保留，靠 sibling_roots 每轮发现新目录
  top = [{'name': '润泽国际信息港A-1能源报告【2025年1月总第107期】', 'fid': 'd1'}]
  roots = dir_index.roots_for_item(item, [{'fid': 'f1', 'chain': top}])
  assert roots == [{'seq': 2, 'fid': 'd1',
                    'segments': ['润泽国际信息港A-1能源报告【2025年1月总第107期】']}], roots


def test_index_drops_roots_covered_by_ancestor():
  """祖先根已覆盖的子树不再单独存根，避免重复遍历。"""
  from service.doc_update import catalog, dir_index

  item15 = next(i for i in catalog.MONTHLY_ITEMS if i.seq == 15)
  item19 = next(i for i in catalog.MONTHLY_ITEMS if i.seq == 19)
  ancestor = [{'name': '2.3值班管理', 'fid': 'd1'},
              {'name': '2.3.2数据中心监控室管理', 'fid': 'd2'}]
  child = ancestor + [{'name': '客户IT库房应急卡领取记录表', 'fid': 'd3'}]
  roots = dir_index.pick_roots([
    (item15, [{'fid': 'f1', 'chain': ancestor}]),
    (item19, [{'fid': 'f2', 'chain': child}]),
  ])
  assert [r['fid'] for r in roots] == ['d2'], roots


def test_energy_item_skips_deprecated_path():
  """作废的能源月报路径不再参与匹配（同一文件在在用的 1.4.5 路径下照常命中）。"""
  from service.doc_update import catalog, fetch

  item = next(i for i in catalog.MONTHLY_ITEMS if i.seq == 2)
  dep_segments = ['二线文件', '2.客户报表', '供配电', '能源月报', '2026']
  deprecated = {
    'fid': 'f1', 'topic': '能源报告2026-05.pdf',
    'path': ' / '.join(dep_segments), 'segments': dep_segments,
    'chain': [{'name': n, 'fid': f'd{i}'} for i, n in enumerate(dep_segments)],
  }
  cur_segments = ['1.4.5能源管理报告（能源月报同步）', '2026']
  current = {
    'fid': 'f2', 'topic': '能源报告2026-10.pdf',
    'path': ' / '.join(cur_segments), 'segments': cur_segments,
    'chain': [{'name': n, 'fid': f'e{i}'} for i, n in enumerate(cur_segments)],
  }
  hits = fetch.match_item(item, [deprecated, current])
  assert [h['fid'] for h in hits] == ['f2'], hits


def test_index_never_picks_deprecated_path():
  """建索引时也不选作废路径的根（即使调用方传进来未过滤的命中）。"""
  from service.doc_update import catalog, dir_index

  item = next(i for i in catalog.MONTHLY_ITEMS if i.seq == 2)
  dep_segments = ['二线文件', '2.客户报表', '供配电', '能源月报']
  deprecated = [{'fid': 'f1', 'chain': [
    {'name': n, 'fid': f'd{i}'} for i, n in enumerate(dep_segments)]}]
  assert dir_index.roots_for_item(item, deprecated) == []

  ok = [{'fid': 'f2', 'chain': [
    {'name': '1.4.5能源管理报告（能源月报同步）', 'fid': 'd9'}]}]
  roots = dir_index.pick_roots([(item, deprecated), (item, ok)])
  assert [r['fid'] for r in roots] == ['d9'], roots


def test_loaded_index_drops_deprecated_roots():
  """已落库索引里的作废根被丢弃，不再被遍历；其余根与其它 seq 原样保留。"""
  import asyncio

  from datetime import datetime, timezone

  from service.doc_update import catalog, depts, dir_index, fetch, share

  item = next(i for i in catalog.MONTHLY_ITEMS if i.seq == 2)
  bad = {'seq': 2, 'fid': 'dbad',
         'segments': ['二线文件', '2.客户报表', '供配电', '能源月报']}
  good = {'seq': 2, 'fid': 'dok', 'segments': ['1.4.5能源管理报告（能源月报同步）']}
  other = {'seq': 34, 'fid': 'd34', 'segments': ['6.人员配置及组织管理']}
  kept = dir_index.drop_excluded([dict(bad), dict(good), dict(other)], [item])
  assert [r['fid'] for r in kept] == ['dok', 'd34'], kept

  calls: list = []

  # 用未配显式路径的公司/部门：本用例只验索引根的丢弃与保留，别被 PATHS 收窄干扰
  async def fake_depts(*_a, **_k):
    return [{'company_id': 'c9', 'company_name': '某公司', 'dept_id': 'd1',
             'dept_name': 'A1', 'root_fid': 'root'}]

  async def fake_walk(*_a, **_k):
    calls.append('walk')
    return []

  async def fake_walk_roots(roots, *_a, **_k):
    calls.append(tuple(r['fid'] for r in roots))
    return []

  async def fake_list_dir(_dir):
    return []

  async def fake_load_index(*_a, **_k):
    return {'roots': [dict(bad), dict(good)],
            'full_scan_at': datetime.now(timezone.utc)}

  async def fake_save_index(*_a, **_k):
    calls.append('save')

  originals = (depts.resolve_departments, share.walk, share.walk_roots,
               share.list_dir, fetch._load_index, fetch._save_index)
  depts.resolve_departments, share.walk = fake_depts, fake_walk
  share.walk_roots, share.list_dir = fake_walk_roots, fake_list_dir
  fetch._load_index, fetch._save_index = fake_load_index, fake_save_index
  try:
    asyncio.run(fetch.take_dept_snapshot(only=[item]))
  finally:
    (depts.resolve_departments, share.walk, share.walk_roots,
     share.list_dir, fetch._load_index, fetch._save_index) = originals

  assert calls == [('dok',)], calls


def main() -> int:
  tests = [v for k, v in sorted(globals().items()) if k.startswith('test_')]
  for fn in tests:
    fn()
    print(f'  ok  {fn.__name__}')
  print(f'全部通过：{len(tests)} 项')
  return 0


if __name__ == '__main__':
  raise SystemExit(main())

"""只读探测自检（纯 assert、不连库不联网；假 lister / walk 桩）。

跑法：`python -m service.doc_update.tests.test_probe`
"""

import asyncio

from service.doc_update import depts, paths, period, probe, share
from service.doc_update.catalog import MONTHLY_ITEMS

DEPT = 'A1'
CONF = ('2.运行维护管理', '2.3.4巡检管理')


def _item(seq: int):
  return next(i for i in MONTHLY_ITEMS if i.seq == seq)


def _file(segments, topic='巡检表.pdf'):
  return {'segments': list(segments), 'topic': topic, 'create_time': None,
          'fid': 'f1', 'chain': [], 'path': '', 'size': 1}


def _row(fid, name, is_dir=True):
  return {'fid': fid, 'name': name, 'is_dir': is_dir, 'create_time': 0, 'size': 1}


def _months(expected: str):
  """`'2026-11'` → 当期与上期目录段（年容器 + 月目录）。"""
  year, month = int(expected[:4]), int(expected[5:])
  older = (year, month - 1) if month > 1 else (year - 1, 12)
  return (f'{year}年考勤', f'{month}月'), (f'{older[0]}年考勤', f'{older[1]}月')


def test_classify_statuses():
  expected = period.expected_period()
  cur, older = _months(expected)
  item_new = _item(22)
  item_upd = _item(1)

  # 月份目录形态 → 抓取阶段会自动改按期判，探测结论跟着走
  got = probe._classify(item_upd, {'error': ''}, [_file(cur)], expected)
  assert got[0] == probe.STATUS_OK and '按期判' in got[1], got

  # 原地更新（名字只有年份、没有月份，单条文件）→ 仍是查更新项
  inplace = [_file(['6.3人员工时管理', '人员排班计划表'],
                   '6.3.1润泽科技-廊坊-A1-人员排班计划表-2026年.xlsx')]
  got = probe._classify(item_upd, {'error': ''}, inplace, expected)
  assert got[0] == probe.STATUS_OK and '查更新项' in got[1], got

  assert probe._classify(item_new, {'error': ''}, [], expected)[0] == probe.STATUS_NO_FILES

  got = probe._classify(item_new, {'error': ''}, [_file(cur)], expected)
  assert got[0] == probe.STATUS_OK, got

  got = probe._classify(item_new, {'error': ''}, [_file(older)], expected)
  assert got[0] == probe.STATUS_PERIOD, got

  got = probe._classify(item_new, {'error': ''}, [_file(('10月',))], expected)
  assert got[0] == probe.STATUS_MONTH_NO_YEAR, got

  got = probe._classify(item_new, {'error': '配置路径不存在：x'}, [], expected)
  assert got[0] == probe.STATUS_PATH

  # 目录只到年份、月份写在文件名里（扁平查新增项）：仍按当期判 OK
  flat = [_file(('2026年文件',), f'A1-{expected[5:]}月值班巡检.pdf')]
  assert probe._classify(item_new, {'error': ''}, flat, expected)[0] == probe.STATUS_OK

  # 混合年形态：旧年带月份目录、新年只有文件名月份 → 期号取新年，不再报旧年
  prev = period.shift_period(expected, -1)
  mixed = [
    _file([f'{prev[:4]}年', f'{int(prev[5:])}月'], '旧年检查表.pdf'),
    _file([f'{expected[:4]}年'], f'电伴热检查-{int(expected[5:])}月.pdf'),
  ]
  status, detail = probe._classify(item_new, {'error': ''}, mixed, expected)
  assert status == probe.STATUS_OK and f'latest={expected}' in detail, detail


def _fake_tree():
  return {
    'root': [_row('d1', CONF[0]), _row('file0', 'readme.txt', False)],
    'd1': [_row('d2', CONF[1])],
    'd2': [],
  }


class _Stubs:
  """临时替换 probe 依赖的联网/写库入口，退出时还原。"""

  def __init__(self, files=None, stats=None, caps=None):
    self.files = files if files is not None else []
    self.stats = stats or {}
    self.caps = caps if caps is not None else {}

  def __enter__(self):
    self.orig = (depts.configured_groups, depts.cached_registry,
                 depts.list_departments, depts.root_file_id,
                 share.list_dir, share.walk_roots)
    tree = _fake_tree()

    def configured_groups():
      return [{'company_id': 'c1', 'company_name': '某公司', 'names': [DEPT]}]

    async def cached_registry():
      return [{'company_id': 'c1', 'company_name': '某公司', 'dept_name': DEPT,
               'dept_id': 'd1', 'root_fid': 'root'}]

    async def list_departments(_cid):
      raise AssertionError('缓存齐全时不应联网解析部门')

    async def root_file_id(_dept_id):
      raise AssertionError('缓存齐全时不应联网取根')

    async def list_dir(dir_id):
      return [dict(r) for r in tree.get(dir_id, [])]

    async def walk_roots(roots, numbers, *, on_progress=None, stats=None,
                         max_dirs=None, max_depth=None):
      self.caps.update(root=roots[0]['fid'], max_dirs=max_dirs,
                       max_depth=max_depth, numbers=numbers)
      if stats is not None:
        stats.update(self.stats)
      return list(self.files)

    depts.configured_groups = configured_groups
    depts.cached_registry = cached_registry
    depts.list_departments = list_departments
    depts.root_file_id = root_file_id
    share.list_dir = list_dir
    share.walk_roots = walk_roots
    return self

  def __exit__(self, *_exc):
    (depts.configured_groups, depts.cached_registry,
     depts.list_departments, depts.root_file_id,
     share.list_dir, share.walk_roots) = self.orig
    paths.PATHS.pop(('c1', DEPT), None)


def _run(seqs, depth=6, max_dirs=300):
  lines: list[str] = []
  code = asyncio.run(probe.run(dept_names=[DEPT], seqs=seqs, depth=depth,
                               max_dirs=max_dirs, out=lines.append))
  return code, lines


def test_run_ok_and_caps():
  expected = period.expected_period()
  cur, _older = _months(expected)
  files = [_file([*CONF, *cur])]
  paths.PATHS[('c1', DEPT)] = {22: CONF}
  with _Stubs(files=files) as stub:
    code, lines = _run([22], depth=4, max_dirs=7)
  text = '\n'.join(lines)
  assert code == 0, text
  assert 'status=OK' in text, text
  assert 'OK 1' in text, text  # SUMMARY 计数
  assert stub.caps['max_dirs'] == 7 and stub.caps['max_depth'] == 4, stub.caps
  assert stub.caps['root'] == 'd2', stub.caps
  assert stub.caps['numbers'] == (), '探测不做编号剪枝'


def test_run_reports_truncated_and_unconfigured():
  paths.PATHS[('c1', DEPT)] = {22: CONF}
  with _Stubs(stats={'truncated': True, 'dirs': 300}) as _stub:
    code, lines = _run([1, 22])
  text = '\n'.join(lines)
  assert code == 0, text
  assert '未配显式路径' in text, 'seq 1 没配，计入未配置'
  assert '遍历触顶被截断' in text
  assert f'{probe.STATUS_UNCONFIGURED} 1' in text


def test_run_reports_missing_config_path():
  paths.PATHS[('c1', DEPT)] = {22: (*CONF, '不存在的子目录')}
  with _Stubs() as _stub:
    code, lines = _run([22])
  text = '\n'.join(lines)
  assert code == 0, text
  assert f'status={probe.STATUS_PATH}' in text
  assert f'{probe.STATUS_PATH} 1' in text


def test_run_dept_root_unavailable():
  paths.PATHS[('c1', DEPT)] = {22: CONF}

  def configured_groups():
    return [{'company_id': 'c1', 'company_name': '某公司', 'names': [DEPT]}]

  async def cached_registry():
    return [{'company_id': 'c1', 'dept_name': DEPT, 'dept_id': 'd1', 'root_fid': ''}]

  async def root_file_id(_dept_id):
    raise RuntimeError('接口超时')

  originals = (depts.configured_groups, depts.cached_registry, depts.root_file_id)
  depts.configured_groups = configured_groups
  depts.cached_registry = cached_registry
  depts.root_file_id = root_file_id
  try:
    lines: list[str] = []
    code = asyncio.run(probe.run(dept_names=[DEPT], seqs=[22], out=lines.append))
  finally:
    (depts.configured_groups, depts.cached_registry, depts.root_file_id) = originals
    paths.PATHS.pop(('c1', DEPT), None)

  text = '\n'.join(lines)
  assert code == 1, '一个部门根都取不到才算失败'
  assert '部门根不可用' in text, text


def main() -> int:
  tests = [v for k, v in sorted(globals().items()) if k.startswith('test_')]
  for fn in tests:
    fn()
    print(f'  ok  {fn.__name__}')
  print(f'全部通过：{len(tests)} 项')
  return 0


if __name__ == '__main__':
  raise SystemExit(main())

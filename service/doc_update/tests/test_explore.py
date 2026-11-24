"""受控子 agent 定位自检（纯 assert、不连网）：目录 allowlist / 越界拒绝 / 有界 / 取消透传。

用假的 `share.list_dir` 树 + monkeypatch `explore._make_bound` 返回脚本化的假模型，
不启动网络、不读真实共享盘。

跑法：`python -m service.doc_update.tests.test_explore`
"""

import asyncio
from types import SimpleNamespace

from config.settings import settings
from service.doc_update import explore
from service.doc_update.sync_state import SyncCancelled

# 起点：章节目录 `2.3.3交接班管理`；真实按月记录在不带编号的 `交接班签字表2026/`
ROOT = 'root'
SUB = 'sub'
TPL = 'tplfile'

TREE = {
  # list_dir(ROOT) 列出子目录（真实记录所在的 SUB）与外层模板
  ROOT: [
    {'fid': SUB, 'name': '交接班签字表2026', 'is_dir': True},
    {'fid': TPL, 'name': '2.3.3.2润泽科技-交接班日志、签字表.xlsx', 'is_dir': False},
  ],
  SUB: [
    {'fid': 'm1', 'name': '1月.pdf', 'is_dir': False},
    {'fid': 'm2', 'name': '10月.pdf', 'is_dir': False},
  ],
}

FILES = [
  {'fid': 'm1', 'file_name': '1月.pdf', 'chain': [
    {'fid': ROOT, 'name': '2.3.3交接班管理'},
    {'fid': SUB, 'name': '交接班签字表2026'}]},
  {'fid': 'm2', 'file_name': '10月.pdf', 'chain': [
    {'fid': ROOT, 'name': '2.3.3交接班管理'},
    {'fid': SUB, 'name': '交接班签字表2026'}]},
  {'fid': 'tplrec', 'file_name': '2.3.3.2模板.xlsx', 'chain': [
    {'fid': ROOT, 'name': '2.3.3交接班管理'}]},
]

START = {'fid': ROOT, 'segments': ['2.3.3交接班管理']}
ITEM = SimpleNamespace(number_label='2.3.3', name='交接班签字表', note='',
                       keywords=(), folders=())


class _AI:
  def __init__(self, calls):
    self.tool_calls = calls


class _FakeBound:
  """按脚本返回 tool_calls；脚本用尽后返回空调用（模型停下）。"""

  def __init__(self, script):
    self.script = list(script)
    self.step = 0
    self.calls = 0

  async def ainvoke(self, messages):
    self.calls += 1
    calls = self.script[self.step] if self.step < len(self.script) else []
    self.step += 1
    return _AI(calls)


def _patch_bound(script):
  original = explore._make_bound
  explore._make_bound = lambda: _FakeBound(script)
  return original


def _patch_tree(counter):
  original = explore.share.list_dir

  async def fake(fid):
    counter.append(fid)
    return list(TREE.get(fid, []))

  explore.share.list_dir = fake
  return original


def _restore(bound_original, tree_original):
  explore._make_bound = bound_original
  explore.share.list_dir = tree_original


def test_locate_selects_real_records_under_unnumbered_subdir():
  """编号命中的是模板，真实按月记录在不带编号子目录里 → 子 agent 选它并映射文件。"""
  counter: list[str] = []
  tree_original = _patch_tree(counter)
  bound_original = _patch_bound([
    [{'name': 'list_dir', 'args': {'dir_fid': ROOT}, 'id': 'c1'}],
    [{'name': 'submit_selection',
      'args': {'dir_fids': [SUB], 'reason': '真实按月记录在此'}, 'id': 'c2'}],
  ])
  try:
    result = asyncio.run(explore.locate(ITEM, files=FILES, start=START))
  finally:
    _restore(bound_original, tree_original)
  assert result, result
  got = {f['fid'] for f in result['files']}
  assert got == {'m1', 'm2'}, got  # 外层模板 tplrec 不在这棵子树里
  assert result['dir_fids'] == [SUB], result['dir_fids']
  assert result['dirs'] == [{'fid': SUB, 'segments': ['2.3.3交接班管理', '交接班签字表2026']}]
  assert result['reason'] == '真实按月记录在此'
  # match_dir 回填到命中的最深目录
  assert all(f['match_dir'] == '交接班签字表2026' for f in result['files'])
  # 只对 start 后的合法目录列了一次
  assert counter == [ROOT], counter


def test_out_of_range_fid_rejected():
  """越界 fid 不被接受：list_dir 拒绝，且绝不触及 share.list_dir。"""
  counter: list[str] = []
  tree_original = _patch_tree(counter)
  bound_original = _patch_bound([
    [{'name': 'list_dir', 'args': {'dir_fid': 'evil'}, 'id': 'c1'}],
  ])
  try:
    result = asyncio.run(explore.locate(ITEM, files=FILES, start=START))
  finally:
    _restore(bound_original, tree_original)
  assert result == {}, result
  assert counter == [], counter  # 越界 fid 连列目录都没有发生


def test_bounded_max_steps_returns_empty():
  """模型一直列目录、从不 submit → 触顶步数后返回空（调用方保持确定性结论）。"""
  counter: list[str] = []
  tree_original = _patch_tree(counter)
  original_steps = settings.DOC_UPDATE_EXPLORE_MAX_STEPS
  settings.DOC_UPDATE_EXPLORE_MAX_STEPS = 2
  bound_original = _patch_bound(
    [[{'name': 'list_dir', 'args': {'dir_fid': ROOT}, 'id': f'c{i}'}]
     for i in range(10)])
  try:
    result = asyncio.run(explore.locate(ITEM, files=FILES, start=START))
  finally:
    _restore(bound_original, tree_original)
    settings.DOC_UPDATE_EXPLORE_MAX_STEPS = original_steps
  assert result == {}, result
  assert len(counter) == 2, counter  # 正好两步步数


def test_disabled_is_noop():
  """开关关闭 → 直接返回空，连模型都不构造。"""
  original = settings.DOC_UPDATE_EXPLORE_ENABLED
  settings.DOC_UPDATE_EXPLORE_ENABLED = False
  called = []

  def boom():
    called.append(True)
    raise AssertionError('不应构造模型')

  bound_original = explore._make_bound
  explore._make_bound = boom
  try:
    result = asyncio.run(explore.locate(ITEM, files=FILES, start=START))
  finally:
    explore._make_bound = bound_original
    settings.DOC_UPDATE_EXPLORE_ENABLED = original
  assert result == {}, result
  assert called == [], called


def test_sync_cancelled_propagates():
  """SyncCancelled 继承 BaseException，必须原样上抛，不能被吞成本次失败。"""
  class _CancelBound:
    async def ainvoke(self, messages):
      raise SyncCancelled('stop')

  bound_original = explore._make_bound
  explore._make_bound = lambda: _CancelBound()
  raised = False
  try:
    asyncio.run(explore.locate(ITEM, files=FILES, start=START))
  except SyncCancelled:
    raised = True
  finally:
    explore._make_bound = bound_original
  assert raised, 'SyncCancelled 未被透传'


def main() -> int:
  tests = [v for k, v in sorted(globals().items()) if k.startswith('test_')]
  for fn in tests:
    fn()
    print(f'  ok  {fn.__name__}')
  print(f'全部通过：{len(tests)} 项')
  return 0


if __name__ == '__main__':
  raise SystemExit(main())

"""未找到项的模型复核自检（纯 assert、不连网不连 Redis）。

跑法：`python -m service.doc_update.tests.test_match_llm`
"""

import asyncio
import json

from service.doc_update import match_llm as ml
from service.doc_update.catalog import DocItem


def _item():
  """seq19：客户IT库房应急卡（真实文件名是「字节IT库房应急卡领取登记记录表」）。"""
  return DocItem(seq=19, numbers=('2.3.2.6',), name='客户IT库房应急卡领取记录表',
                 cadence='month', keywords=('应急卡',),
                 folders=('客户IT库房应急卡', '客户库房应急卡'))


def _card_item():
  """seq15：应急卡使用登记表（中控室）—— 与 seq19 同一个钉点，最容易互相串。"""
  return DocItem(seq=15, numbers=('2.3.2.2',), name='应急卡使用登记表（中控室）',
                 cadence='month', keywords=('应急卡',))


def _candidates():
  return [
    {'fid': 'f1', 'topic': '字节IT库房应急卡领取登记记录表.pdf',
     'path': '2.运维 / 记录表 / 2026 / 9月'},
    {'fid': 'f2', 'topic': '应急卡使用登记表.pdf', 'path': '2.运维 / 记录表 / 2026 / 9月'},
  ]


def _run(coro):
  return asyncio.run(coro)


def _assign(number, files, reason='按含义挑'):
  return {'number': number, 'files': files, 'reason': reason}


def _reply(*assignments):
  return json.dumps({'assignments': list(assignments)}, ensure_ascii=False)


def _reply_both(assign=(), unsure=(), reasons=None):
  return json.dumps({'assignments': list(assign), 'uncertain': list(unsure),
                     'reasons': reasons or {}}, ensure_ascii=False)


class _Patched:
  """临时替换缓存与模型出口，退出时还原（同一进程跑多个用例互不影响）。"""

  def __init__(self, reply=None, cached=None, raises=False):
    self.reply = reply
    self.cached = cached
    self.raises = raises
    self.calls = 0
    self.writes = []
    self.reads = []

  def __enter__(self):
    self._orig = (ml._chat, ml._cache_read, ml._cache_write,
                  ml.settings.DOC_UPDATE_MATCH_LLM_ENABLED)
    outer = self

    async def fake_chat(messages, **kwargs):
      outer.calls += 1
      if outer.raises:
        raise RuntimeError('上游超时')
      return outer.reply

    ml._chat = fake_chat
    ml._cache_read = lambda payload: (outer.reads.append(payload), outer.cached)[1]
    ml._cache_write = lambda payload, result: outer.writes.append(result)
    ml.settings.DOC_UPDATE_MATCH_LLM_ENABLED = True
    return self

  def __exit__(self, *exc):
    (ml._chat, ml._cache_read, ml._cache_write,
     ml.settings.DOC_UPDATE_MATCH_LLM_ENABLED) = self._orig
    return False


def test_pick_inside_candidates():
  """候选清单内的合法选择命中：返回原候选记录，顺序与候选一致。"""
  reply = _reply(_assign('2.3.2.6', ['字节IT库房应急卡领取登记记录表.pdf'], '「字节」即客户方'))
  with _Patched(reply=reply) as p:
    res = _run(ml.recheck_many([(_item(), _candidates())]))
  assert p.calls == 1
  assert [f['fid'] for f in res[19]['files']] == ['f1'], res
  assert '字节' in res[19]['reason']
  assert len(p.writes) == 1, '正常返回要写缓存'


def test_same_name_backfills_all_months():
  """同名文件散在多个月份目录：模型只写一次名字，本地要回填全部同名记录（否则漏最新月）。"""
  cands = [
    {'fid': 'f1', 'topic': '应急卡使用登记表.pdf', 'path': '记录表 / 2026 / 9月'},
    {'fid': 'f2', 'topic': '钥匙使用登记表-中控室.pdf', 'path': '记录表 / 2026 / 9月'},
    {'fid': 'f3', 'topic': '应急卡使用登记表.pdf', 'path': '记录表 / 2026 / 10月'},
  ]
  with _Patched(reply=_reply(_assign('2.3.2.2', ['应急卡使用登记表.pdf']))):
    res = _run(ml.recheck_many([(_card_item(), cands)]))
  assert [f['fid'] for f in res[15]['files']] == ['f1', 'f3'], res


def test_group_dispatch_one_call():
  """同一范围的多条未找到项合并成一次调用，各拿各的文件（不互相串）。"""
  reply = _reply(
    _assign('2.3.2.2', ['应急卡使用登记表.pdf'], '中控室应急卡使用表'),
    _assign('2.3.2.6', ['字节IT库房应急卡领取登记记录表.pdf'], '字节是客户方'),
  )
  with _Patched(reply=reply) as p:
    res = _run(ml.recheck_many([(_card_item(), _candidates()), (_item(), _candidates())]))
  assert p.calls == 1, '同一范围只该调一次模型'
  assert [f['fid'] for f in res[15]['files']] == ['f2'], res
  assert [f['fid'] for f in res[19]['files']] == ['f1'], res


def test_conflicting_name_dropped():
  """同一个文件名被两条清单项同时选中 → 判不清，谁都不算。"""
  reply = _reply(
    _assign('2.3.2.2', ['应急卡使用登记表.pdf']),
    _assign('2.3.2.6', ['应急卡使用登记表.pdf']),
  )
  with _Patched(reply=reply):
    res = _run(ml.recheck_many([(_card_item(), _candidates()), (_item(), _candidates())]))
  assert res[15]['files'] == [] and res[19]['files'] == [], res


def test_unknown_number_dropped():
  """模型编出来的编号（不在本次询问的条目里）整条丢弃。"""
  reply = _reply(_assign('9.9.9', ['应急卡使用登记表.pdf']))
  with _Patched(reply=reply):
    res = _run(ml.recheck_many([(_item(), _candidates())]))
  assert res[19]['files'] == [], res


def test_off_list_names_dropped():
  """模型编出来的文件名（不在清单里）必须丢弃，只留真实候选。"""
  reply = _reply(_assign('2.3.2.6',
                         ['客户IT库房应急卡领取记录表.pdf',        # 编造
                          '字节IT库房应急卡领取登记记录表.pdf',
                          '  应急卡使用登记表.pdf  ']))               # 空白归一后合法
  with _Patched(reply=reply):
    res = _run(ml.recheck_many([(_item(), _candidates())]))
  assert sorted(f['fid'] for f in res[19]['files']) == ['f1', 'f2'], res


def test_whitespace_inserted_names_match():
  """模型会在中文名里插空格（实测「钥匙使用登记表-中控室.pdf」→「… - 中控室.pdf」），
  比对必须忽略全部空白，否则真实文件全被丢掉。"""
  cands = [
    {'fid': 'f1', 'topic': '钥匙使用登记表-中控室.pdf', 'path': '记录表 / 2026 / 9月'},
    {'fid': 'f2', 'topic': '监控大厅钥匙使用记录3月.pdf', 'path': '记录表 / 2026 / 3月'},
    {'fid': 'f3', 'topic': '钥匙使用登记表-值班室.pdf', 'path': '记录表 / 2026 / 9月'},
  ]
  item = DocItem(seq=16, numbers=('2.3.2.3',), name='钥匙使用登记表（中控室）',
                 cadence='month', keywords=('钥匙',))
  reply = _reply(_assign('2.3.2.3',
                         ['钥匙使用登记表 - 中控室.pdf', '监控大厅钥匙使用记录 3 月.pdf']))
  with _Patched(reply=reply) as p:
    res = _run(ml.recheck_many([(item, cands)]))
  assert p.calls == 1
  assert [f['fid'] for f in res[16]['files']] == ['f1', 'f2'], res


def test_dict_shape_reply():
  """首选输出是 `{编号: [名字]}` 对象（输出短，抑制模型退化重复片段）。"""
  reply = json.dumps({
    'assignments': {'2.3.2.6': ['字节IT库房应急卡领取登记记录表.pdf']},
    'reasons': {'2.3.2.6': '「字节」是客户方名称'},
  }, ensure_ascii=False)
  with _Patched(reply=reply) as p:
    res = _run(ml.recheck_many([(_item(), _candidates())]))
  assert [f['fid'] for f in res[19]['files']] == ['f1'], res
  assert '客户方' in res[19]['reason']
  assert len(p.writes) == 1


def test_uncertain_becomes_review_files():
  """拿不准的候选进 review_files（下游判「待复核」），不算已命中。"""
  reply = _reply_both(unsure=[_assign('2.3.2.6', ['字节IT库房应急卡领取登记记录表.pdf'],
                                      '疑似客户库房应急卡')])
  with _Patched(reply=reply) as p:
    res = _run(ml.recheck_many([(_item(), _candidates())]))
  assert p.calls == 1
  assert res[19]['files'] == [], 'uncertain 不得当已命中'
  assert [f['fid'] for f in res[19]['review_files']] == ['f1'], res
  assert '客户库房' in res[19]['reason']
  assert len(p.writes) == 1


def test_uncertain_off_list_dropped():
  """uncertain 里清单外的名字同样丢弃，只留真实候选。"""
  reply = _reply_both(unsure=[_assign('2.3.2.6', ['编造的应急卡.pdf',
                                                '字节IT库房应急卡领取登记记录表.pdf'])])
  with _Patched(reply=reply):
    res = _run(ml.recheck_many([(_item(), _candidates())]))
  assert [f['fid'] for f in res[19]['review_files']] == ['f1'], res
  assert res[19]['files'] == []


def test_assignment_wins_over_uncertain():
  """同一名字同时出现在两条通道（同一清单项）→ 只算 assignment。"""
  name = '字节IT库房应急卡领取登记记录表.pdf'
  reply = _reply_both(assign=[_assign('2.3.2.6', [name])],
                      unsure=[_assign('2.3.2.6', [name])])
  with _Patched(reply=reply):
    res = _run(ml.recheck_many([(_item(), _candidates())]))
  assert [f['fid'] for f in res[19]['files']] == ['f1'], res
  assert res[19]['review_files'] == [], res


def test_uncertain_cross_item_conflict_dropped():
  """同一文件被两条清单项选中（哪怕一条是 uncertain）→ 判不清，谁都不算。"""
  name = '应急卡使用登记表.pdf'
  reply = _reply_both(assign=[_assign('2.3.2.2', [name])],
                      unsure=[_assign('2.3.2.6', [name])])
  with _Patched(reply=reply):
    res = _run(ml.recheck_many([(_card_item(), _candidates()), (_item(), _candidates())]))
  assert res[15]['files'] == [] and res[15]['review_files'] == [], res
  assert res[19]['files'] == [] and res[19]['review_files'] == [], res


def test_empty_selection_kept_as_missing():
  """挑不出时返回空列表 + 依据；依据要留给页面提示，但不改判定。"""
  reply = _reply(_assign('2.3.2.6', [], '目录里没有对应登记表'))
  with _Patched(reply=reply) as p:
    res = _run(ml.recheck_many([(_item(), _candidates())]))
  assert res[19]['files'] == []
  assert res[19]['reason'] == '目录里没有对应登记表'
  assert len(p.writes) == 1, '空选择也是正常返回，应缓存'


def test_failure_not_cached():
  """调用异常 / 返回结构不合预期 → 空结果且不写缓存（下一轮重试）。"""
  with _Patched(raises=True) as p:
    res = _run(ml.recheck_many([(_item(), _candidates())]))
  assert res[19] == {'files': [], 'review_files': [], 'reason': ''}
  assert p.calls == 1 and p.writes == []

  for bad in ('不是 JSON', json.dumps({'files': []}),   # 旧结构、两条通道都缺
              json.dumps({'assignments': 'x'})):
    with _Patched(reply=bad) as p2:
      res2 = _run(ml.recheck_many([(_item(), _candidates())]))
    assert res2[19] == {'files': [], 'review_files': [], 'reason': ''}, bad
    assert p2.writes == [], bad


def test_cache_hit_skips_model():
  cached = {'entries': [{'number': '2.3.2.6',
                         'files': ['字节IT库房应急卡领取登记记录表.pdf'],
                         'review': [], 'reason': '缓存'}]}
  with _Patched(reply=None, cached=cached) as p:
    res = _run(ml.recheck_many([(_item(), _candidates())]))
  assert p.calls == 0, '缓存命中不该再调模型'
  assert [f['fid'] for f in res[19]['files']] == ['f1']


def test_cache_key_changes_with_candidates():
  """候选集变化（新文件上传）必须换键，否则旧结论会挡住新文件。"""
  item = _item()
  key1 = ml._cache_key(ml._payload([item], _candidates()))
  more = _candidates() + [{'fid': 'f9', 'topic': '新上传的应急卡.pdf', 'path': ''}]
  key2 = ml._cache_key(ml._payload([item], more))
  assert key1 != key2
  assert key1 == ml._cache_key(ml._payload([item], _candidates())), '同一输入键要稳定'
  # 条目集变化同样换键（同目录多一条未找到项，分派结论就不同）
  key3 = ml._cache_key(ml._payload([item, _card_item()], _candidates()))
  assert key1 != key3


def test_cache_version_and_prompt_in_key():
  """改缓存版本或提示词（_SYSTEM_HASH）必须换键，否则旧结论会挡住新逻辑。"""
  payload = ml._payload([_item()], _candidates())
  key1 = ml._cache_key(payload)
  orig_version, orig_hash = ml._CACHE_VERSION, ml._SYSTEM_HASH
  try:
    ml._CACHE_VERSION = orig_version + 1
    key2 = ml._cache_key(payload)
    ml._SYSTEM_HASH = 'deadbeefcafe'
    key3 = ml._cache_key(payload)
  finally:
    ml._CACHE_VERSION, ml._SYSTEM_HASH = orig_version, orig_hash
  assert key1 != key2 and key2 != key3 and key1 != key3
  assert key1 == ml._cache_key(payload), '还原后键要稳定'


def test_disabled_or_empty_skips():
  with _Patched(reply=_reply()) as p:
    ml.settings.DOC_UPDATE_MATCH_LLM_ENABLED = False
    res = _run(ml.recheck(_item(), _candidates()))
  assert p.calls == 0 and res == {'files': [], 'review_files': [], 'reason': ''}

  with _Patched(reply=_reply()) as p2:
    res2 = _run(ml.recheck(_item(), []))
  assert p2.calls == 0 and res2 == {'files': [], 'review_files': [], 'reason': ''}


def test_loads_tolerates_fences():
  assert ml._loads('```json\n{"assignments": []}\n```') == {'assignments': []}
  assert ml._loads('好的：{"assignments": [], "reason": "x"} 以上') == {
    'assignments': [], 'reason': 'x'}
  assert ml._loads('') == {}


if __name__ == '__main__':
  test_pick_inside_candidates()
  test_same_name_backfills_all_months()
  test_group_dispatch_one_call()
  test_conflicting_name_dropped()
  test_unknown_number_dropped()
  test_off_list_names_dropped()
  test_whitespace_inserted_names_match()
  test_dict_shape_reply()
  test_uncertain_becomes_review_files()
  test_uncertain_off_list_dropped()
  test_assignment_wins_over_uncertain()
  test_uncertain_cross_item_conflict_dropped()
  test_empty_selection_kept_as_missing()
  test_failure_not_cached()
  test_cache_hit_skips_model()
  test_cache_key_changes_with_candidates()
  test_cache_version_and_prompt_in_key()
  test_disabled_or_empty_skips()
  test_loads_tolerates_fences()
  print('test_match_llm: all ok')

"""显式路径自检（纯 assert、不连库）：配置解析 / 子树过滤 / 失败不回落启发式。

跑法：`python -m service.doc_update.tests.test_paths`
"""

import asyncio

from service.doc_update import dir_index, fetch, paths, period, share
from service.doc_update.catalog import MONTHLY_ITEMS
from service.doc_update.verdict import STATUS_MISSING, evaluate

SEQ = 22
CONF_DIR = '2.3.4.6值班巡检记录表（部长 工程师 班长）'
FULL = ('2.运行维护管理', '2.3值班管理', '2.3.4巡检管理', CONF_DIR)
MONITOR_LEAF = '视频监控录像查询记录单'
COPY_LEAF = '视频监控录像拷贝记录单'
FULL12 = ('廊坊-A1-数据中心运维管理体系', '2.运行维护管理', '2.2运行管理', MONITOR_LEAF)
FULL13 = ('廊坊-A1-数据中心运维管理体系', '2.运行维护管理', '2.2运行管理', COPY_LEAF)


def _item(seq=SEQ):
  return next(i for i in MONTHLY_ITEMS if i.seq == seq)


def _row(fid, name, is_dir=True):
  return {'fid': fid, 'name': name, 'is_dir': is_dir, 'create_time': 0, 'size': 1}


def _file(fid, topic, chain, segments):
  return {'fid': fid, 'topic': topic, 'chain': chain, 'segments': segments,
          'path': ' / '.join(segments), 'create_time': 0, 'size': 1}


def _hit():
  chain = [{'fid': 'd2', 'name': '2.3.4巡检管理'}, {'fid': 'd4', 'name': CONF_DIR}]
  return _file('f1', '2026-11-03班长巡检表.pdf', chain,
               ['2.运行维护管理', '2.3值班管理', '2.3.4巡检管理', CONF_DIR])


def _dup():
  # 别处的同名副本：目录名一样、fid 不同（实测模板/归档/备份分支都有）
  chain = [{'fid': 'x2', 'name': '2.3.4巡检管理'}, {'fid': 'd4x', 'name': CONF_DIR}]
  return _file('f9', '2026-11-03班长巡检表.pdf', chain,
               ['备份', '2.3.4巡检管理', CONF_DIR])


def test_parse_path():
  assert paths.parse_path('a > b > c') == ('a', 'b', 'c')
  assert paths.parse_path('a/b/c') == ('a', 'b', 'c')
  assert paths.parse_path('a\\b') == ('a', 'b')
  assert paths.parse_path('  a ／ b ') == ('a', 'b')
  assert paths.parse_path('a > 　b') == ('a', 'b'), '全角空格也要去掉'
  assert paths.parse_path('') == ()


def test_configured_by_label_and_company_id():
  assert paths.configured('', '廊坊', 'A1')[SEQ] == (
    '廊坊-A1-数据中心运维管理体系', '2.运行维护管理', '2.3值班管理', '2.3.4巡检管理',
    CONF_DIR)
  paths.PATHS[('C123', 'a2')] = {SEQ: ('x',)}
  try:
    assert paths.configured('C123', '随便什么名字', 'A2') == {SEQ: ('x',)}
    assert paths.configured('c123', '', 'a2') == {SEQ: ('x',)}, '按 company_id 也能命中'
  finally:
    paths.PATHS.pop(('C123', 'a2'), None)
  assert paths.configured('x', 'y', 'A99') == {}, '不在清单里的部门返回空'
  pinghu = paths.configured('', '平湖', 'A1')
  assert pinghu, '平湖 A1 已钉显式路径'
  assert pinghu[SEQ][0] == '平湖-A1-数据中心运维管理体系', '同名部门不串用廊坊配置'


def test_langfang_depts_cover_all_monthly_items():
  """廊坊 14 个部门（含 B1M1/B1M2）的显式路径必须覆盖全部 25 条月度项，且只落在稳定章节目录。

  - 段不能是期号容器（`2026` / `10月` / `…【2025年1月总第107期】`），否则下月失效；
  - 首段是体系目录（`廊坊-AX-…` / `区域-A5-…`），漏了从部门根往下第一步就找不到；
  - 其余园区的钉子见 `test_other_parks_pins`。
  """
  expect = {i.seq for i in MONTHLY_ITEMS}
  depts = ['A1', 'A2', 'A3', 'A5', 'A6', 'A7', 'A8', 'A9', 'A10', 'A11', 'A12', 'A18',
           'B1M1', 'B1M2']
  for dept in depts:
    cfg = paths.configured('', '廊坊', dept)
    assert set(cfg) == expect, f'{dept} 配置项不齐：{sorted(expect - set(cfg))}'
    for seq, segs in cfg.items():
      assert segs, f'{dept}/{seq} 段为空'
      assert segs[0] == paths._SYSTEM_DIRS[dept], f'{dept}/{seq} 首段不是体系目录'
      for seg in segs:
        assert not dir_index.is_period_dir(seg), f'{dept}/{seq} 含期号容器：{seg}'


def test_other_parks_pins():
  """平湖/佛山/重庆/惠州按业务文档逐条钉：只钉月度项、首段自带体系目录、无期号容器。"""
  expect = {i.seq for i in MONTHLY_ITEMS}
  depts = {'平湖': {'A1', 'A2'}, '佛山': {'A1', 'A2'},
           '重庆': {'A1', 'A2'}, '惠州': {'A2'}}
  # 实测根目录名带前缀的部门（重庆 A2 是 `1、重庆-A2-…`，2026-11-18 探针确认）
  head_override = {('重庆', 'A2'): '1、重庆-A2-数据中心运维管理体系'}
  for (park, dept), items in paths._OTHER_PARK_DEPTS.items():
    assert dept in depts[park], f'{park}/{dept} 不在已配清单'
    assert items, f'{park}/{dept} 一条都没钉'
    head = head_override.get((park, dept), f'{park}-{dept}-数据中心运维管理体系')
    for seq, segs in items.items():
      assert seq in expect, f'{park}/{dept}/{seq} 不是月度项（25/26 不得出现）'
      assert segs, f'{park}/{dept}/{seq} 段为空'
      assert segs[0] == head, f'{park}/{dept}/{seq} 首段不是体系目录：{segs[0]}'
      for seg in segs:
        assert not dir_index.is_period_dir(seg), f'{park}/{dept}/{seq} 含期号容器：{seg}'

  def last(park, dept, seq):
    return paths.configured('', park, dept)[seq][-1]

  # 抽查叶子形态：钉到记录文件夹的 / 只到章节的
  assert last('平湖', 'A2', 35) == '6.3.2润泽科技-平湖-A2-考勤统计表'
  assert last('佛山', 'A1', 29) == '部门隐患排查'
  assert last('佛山', 'A2', 15) == '2.3.2数据中心监控室管理'
  assert last('重庆', 'A2', 15) == '记录表'
  assert last('重庆', 'A1', 13) == '2.2运行管理'
  assert last('惠州', 'A2', 22) == '2.3.4.6润泽科技-惠州-A2-值班巡检记录表（部长 工程师 班长）'
  assert last('惠州', 'A2', 20) == '2.3.3交接班管理'

  # 业务文档标「无」的项不配：这些 seq 必须缺席（继续走启发式）
  for park, dept in (('佛山', 'A1'), ('佛山', 'A2'), ('重庆', 'A1'),
                     ('重庆', 'A2'), ('惠州', 'A2')):
    assert 19 not in paths.configured('', park, dept), f'{park}/{dept} seq19 文档为「无」'
  assert 28 not in paths.configured('', '重庆', 'A1'), '重庆 A1 seq28 文档为「无」'


def test_override_paths():
  """代表性叶子：钉到业务给的记录文件夹，年份版本化目录退回父目录。"""
  def segs(dept, seq):
    return paths.configured('', '廊坊', dept)[seq]

  def last(dept, seq):
    return segs(dept, seq)[-1]

  # A1：记录文件夹叶子（不再停在章节目录，模板 docx 在那里）
  assert last('A1', 1) == '1.2.2润泽科技-廊坊-A1-负载管理统计分析表'
  assert last('A1', 12) == MONITOR_LEAF
  assert last('A1', 13) == COPY_LEAF
  assert last('A1', 15) == '应急卡使用登记表'
  assert last('A1', 20) == '交接班日志'
  assert last('A1', SEQ) == CONF_DIR
  # A18：seq12 停在章节目录（2026 版目录会变），seq34 钉到编号叶子
  assert segs('A18', 12)[1:] == ('2.运行维护管理', '2.2运行管理')
  assert last('A18', 34) == '6.3.1润泽科技-廊坊-A18-人员排班计划表'
  # A3 seq12 编号叶子
  assert last('A3', 12) == '2.2.8视频监控录像查询记录单'
  # A2 seq15 收窄到「记录表」子目录
  assert last('A2', 15) == '记录表'
  # A5/A7 首段体系目录是「区域-」
  assert segs('A5', SEQ)[0] == '区域-A5-数据中心运维管理体系'
  assert segs('A7', SEQ)[0] == '区域-A7-数据中心运维管理体系'
  # A6 seq2 用父目录（1.4.5 带「同步2026」），seq21 退回章节目录
  assert segs('A6', 2) == (
    '廊坊-A6-数据中心运维管理体系', '1.规划管理', '1.4精益化能源管理')
  assert last('A6', 21) == '2.3.4巡检管理'
  # A6 seq27 钉到章节目录：真实目录按年版本化（`2026-3.8.7…扫描件`），
  # 由 `_starts_with_number` 剥年份前缀后按编号命中，明年同样命中
  assert last('A6', 27) == '3.8门禁权限管理'


def test_period_dir_detects_paren_year():
  """括号年份目录同样不该固化进索引；括号里不是四位数年份的仍算稳定目录。"""
  assert dir_index.is_period_dir('A9-卫生清洁记录表（2026）')
  assert dir_index.is_period_dir('2026年考勤')
  assert dir_index.is_period_dir('卫生清洁记录2026'), '年份后缀同样是期号容器'
  assert not dir_index.is_period_dir(CONF_DIR)
  assert not dir_index.is_period_dir('2.3.4巡检管理')
  assert not dir_index.is_period_dir('区域2101机房'), '四位房间代号不是年份'


def test_normalize_segments():
  assert paths.normalize_segments(['2.3.4.6 值班巡检记录表', ' ABC ']) == (
    '2.3.4.6值班巡检记录表', 'abc')
  assert paths.normalize_segments([CONF_DIR, '']) == (
    '2.3.4.6值班巡检记录表（部长工程师班长）',)


def test_under_ignores_same_named_copy():
  assert fetch._under(_hit(), 'd4')
  assert not fetch._under(_dup(), 'd4'), '同名别处副本不算在配置目录下'
  assert not fetch._under(_hit(), '')


def test_match_files_uses_config_subtree():
  files = [_hit(), _dup()]
  item = _item()
  heur = fetch._match_files(item, files, None)
  assert {f['fid'] for f in heur} == {'f1', 'f9'}, '未配置项仍走启发式，两处副本都算'
  matched = fetch._match_files(
    item, files, {'seq': SEQ, 'fid': 'd4', 'segments': [CONF_DIR]}, configured=True)
  assert [f['fid'] for f in matched] == ['f1']
  assert matched[0]['match_dir'] == CONF_DIR


def test_config_shared_parent_disambiguates():
  """稳定父目录被多项共用时，子树内仍按编号+关键词挑，不互相串项。"""
  root = '4.1.5隐患排查治理'
  chain = [{'fid': 'dbc', 'name': root}]
  supply = _file('f29', 'A18-2026.9月部门级隐患排查记录表_供配电.pdf', chain,
                 ['4.EHS管理', '4.1安全管理', root])
  cool = _file('f30', 'A18-2026.9月部门级隐患排查记录表_制冷.pdf', chain,
               ['4.EHS管理', '4.1安全管理', root])
  items = {i.seq: i for i in MONTHLY_ITEMS}
  cfg_root = {'seq': 29, 'fid': 'dbc', 'segments': [root]}
  got = fetch._match_files(items[29], [supply, cool], cfg_root, configured=True)
  assert [f['fid'] for f in got] == ['f29']
  got = fetch._match_files(items[30], [supply, cool], cfg_root, configured=True)
  assert [f['fid'] for f in got] == ['f30']


def test_configured_without_resolution_never_falls_back():
  """配了路径但解析失败：即使全树启发式有命中，也必须返回空。"""
  files = [_hit()]
  assert fetch.match_item(_item(), files), '全树启发式本身有命中'
  assert fetch._match_files(_item(), files, None, configured=True) == []


def test_config_dir_number_keeps_match():
  """文件名不带别名/编号时，配置目录名里的编号仍要能命中（别改成空 segments 遍历）。"""
  chain = [{'fid': 'd2', 'name': '2.3.4巡检管理'}, {'fid': 'd4', 'name': CONF_DIR}]
  rec = _file('f1', '2026-11-03巡检记录.pdf', chain,
              ['2.运行维护管理', '2.3值班管理', '2.3.4巡检管理', CONF_DIR])
  got = fetch._match_files(_item(), [rec],
                           {'seq': SEQ, 'fid': 'd4', 'segments': [CONF_DIR]},
                           configured=True)
  assert [f['fid'] for f in got] == ['f1']
  assert got[0]['match_dir'] == CONF_DIR


def test_named_pin_matches_by_folder_name():
  """廊坊 A1 形态：钉住的目录名就是名字，目录/文件名都不带清单编号与别名。"""
  chain = [{'fid': f'd{i}', 'name': seg} for i, seg in enumerate(FULL12)]
  rec = _file('f12', '视频监控录像查询单.pdf', chain, [*FULL12, '2026年', '10月'])
  root = {'seq': 12, 'fid': f'd{len(FULL12) - 1}', 'segments': list(FULL12)}
  assert fetch._match_files(_item(12), [rec], None) == [], '全树启发式本身不命中'
  assert fetch._match_files(_item(12), [rec], root, configured=True) == [], \
    '没标 named 时保持原口径，不由目录名兜底'
  got = fetch._match_files(_item(12), [rec], root, configured=True, named=True)
  assert [f['fid'] for f in got] == ['f12']
  assert got[0]['match_dir'] == MONITOR_LEAF, '实际目录记钉住的目录名，不记月份目录'


def test_named_roots_only_dedicated():
  """只有「独占且不是别的钉点祖先」的配置根，目录名才能单独当名字。"""
  def root(seq, fid, segs):
    return {'seq': seq, 'fid': fid, 'segments': list(segs)}

  monitor = root(12, 'd3', FULL12)
  copy = root(13, 'd13', FULL13)
  chapter = root(19, 'd2', FULL12[:-1])
  assert fetch._named_roots({}) == set()
  assert fetch._named_roots({12: monitor, 13: copy}) == {12, 13}
  assert fetch._named_roots({12: monitor, 15: root(15, 'd3', FULL12)}) == set(), \
    '同一目录被多项共用（A2 的「记录表」形态）不算专属'
  assert fetch._named_roots({19: chapter, 12: monitor}) == {12}, \
    '钉在章节目录（是别的钉点的祖先）不算专属'


def test_named_pin_skips_update_items():
  """查更新项按名字全收会把历史版本一并下载，故不兜底。"""
  item = next(i for i in MONTHLY_ITEMS if i.judge == 'update')
  segs = ('1.规划管理', '1.6机房初始化管理', '1.6.3阈值管理')
  chain = [{'fid': f'u{i}', 'name': seg} for i, seg in enumerate(segs)]
  rec = _file('u9', '随便一个名字.pdf', chain, [*segs, '2026', '10月'])
  root = {'seq': item.seq, 'fid': f'u{len(segs) - 1}', 'segments': list(segs)}
  assert fetch._match_files(item, [rec], root, configured=True, named=True) == []


def test_chapter_template_drops_before_month_records():
  """A1 实测：钉在章节目录，编号只命中模板，真实记录在无编号目录的月份子目录里。

  seq24 记录在 `卫生清洁记录2026/<月>`（年份后缀目录），seq27 在
  `2026年/货物运输通道使用记录单/<月>`；别名命中后模板仍会进候选，
  由 `_relevant_records` 按「本期无期号」剔掉，不能判成已更新。
  """
  items = {i.seq: i for i in MONTHLY_ITEMS}
  cases = (
    (24, ('2.运行维护管理', '2.10环境管理'),
     '2.10.5润泽科技-廊坊-A1-卫生清洁记录表.xlsx', '卫生清洁记录2026',
     '卫生清洁记录表10月.PDF', '2026-10'),
    (27, ('3.流程管理', '3.8门禁权限管理'),
     '3.8.7润泽科技-廊坊-A1-货物运输通道使用记录单.docx', '货物运输通道使用记录单',
     '货运运输通道使用记录单.pdf', '2026-10'),
  )
  for seq, root, tpl_name, rec_dir, rec_name, want_period in cases:
    chain = [{'fid': f'r{i}', 'name': s} for i, s in enumerate(root)]
    tpl = _file('tpl', tpl_name, chain, list(root))
    rec = _file('rec', rec_name,
                [*chain, {'fid': 'dy', 'name': '2026年'},
                 {'fid': 'dd', 'name': rec_dir}, {'fid': 'dm', 'name': '10月'}],
                [*root, '2026年', rec_dir, '10月'])
    cfg = {'seq': seq, 'fid': f'r{len(root) - 1}', 'segments': list(root)}
    hit = fetch._match_files(items[seq], [tpl, rec], cfg, configured=True, named=True)
    assert {f['fid'] for f in hit} == {'tpl', 'rec'}, seq
    records = [fetch._period_record(f) for f in hit]
    kept = fetch._relevant_records(records, '2026-11', items[seq].judge,
                                   period.LAYOUT_PERIOD)
    assert [r['fid'] for r in kept] == ['rec'], seq
    assert kept[0]['period'] == want_period, seq


def _xm_file(fid, topic, leaf='10月'):
  """A1 真实形态：钉点章节目录 + `2026年隐患排查记录表/<月>`。"""
  segs = ['4.EHS管理', '4.1安全管理', '4.1.5隐患排查治理', '2026年隐患排查记录表', leaf]
  return _file(fid, topic, [{'fid': f'c{i}', 'name': s} for i, s in enumerate(segs)], segs)


def test_item_names_only_registered_depts():
  """按部门文件名别名只登记实测过的部门；同名部门跨公司不能借用。"""
  assert paths.item_names('27WqDiazDVr', '廊坊', 'A1')[29] == paths._BRANCH_NAMES
  assert paths.item_names('27WqDiazDVr', '廊坊', 'a1')[32] == paths._TEAM_NAMES
  assert paths.item_names('c1', '廊坊', 'A10')[30] == ('制冷部门隐患排查',)
  # 其余园区：业务文档里「部门（级）隐患排查」有据可查的那几个
  assert paths.item_names('c1', '平湖', 'A2')[29] == paths._BRANCH_NAMES
  assert paths.item_names('c1', '佛山', 'A1')[32] == paths._TEAM_NAMES
  assert paths.item_names('c1', '佛山', 'A2')[30] == paths._BRANCH_NAMES
  assert paths.item_names('c1', '重庆', 'A1')[29] == paths._BRANCH_NAMES
  assert paths.item_names('c1', '平湖', 'A1') == {}, '平湖 A1 与廊坊 A1 是不同目录'
  assert paths.item_names('c1', '重庆', 'A2') == {}, '文档只到章节，没有目录名佐证'
  assert paths.item_names('c1', '惠州', 'A2') == {}, '文档只到章节，没有目录名佐证'
  # A6：16/17/18/20 钉在共用章节目录，靠别名兜住；29~32 仍不配（专业写在文件名里）
  a6 = paths.item_names('c1', '廊坊', 'A6')
  assert a6[16] == ('钥匙使用登记表',) and a6[20] == ('交接班签字表',)
  assert not ({29, 30, 31, 32} & set(a6)), 'A6 隐患排查不给别名'
  # A 区其余补丁（2026-11-19 对照《廊坊园区修改.txt》）
  a2 = paths.item_names('c1', '廊坊', 'A2')
  assert a2[34] == ('值班表',) and a2[35] == ('考勤记录表',)
  assert paths.item_names('c1', '廊坊', 'A5')[20] == ('交接班签字表',)
  assert paths.item_names('c1', '廊坊', 'A8')[21] == ('门禁巡检报警记录表',)
  assert paths.item_names('c1', '廊坊', 'A9')[20] == ('交接班签字表',)
  a10 = paths.item_names('c1', '廊坊', 'A10')
  assert a10[12] == ('视频监控录像查询记录单', '视频监控查询记录单')
  assert a10[13] == ('视频监控录像拷贝记录单', '视频监控拷贝记录单')
  assert a10[27] == ('货运通道使用登记表', '货物运维通道')
  assert paths.item_names('c1', '廊坊', 'A11')[23] == ('物品使用登记',)
  # B 区：B1M1 有别名（合订文件不写专业）；B1M2 钉独占叶子，靠编号 + 关键词，不配
  b1m1 = paths.item_names('c1', '廊坊', 'B1M1')
  assert b1m1[29] == paths._BRANCH_NAMES and b1m1[32] == paths._TEAM_NAMES
  assert paths.item_names('c1', '廊坊', 'B1M2') == {}
  assert paths.item_names('c1', '廊坊', 'A5')[24] == ('清洁记录表',), \
    'A5 真实记录叫「九月份清洁记录表」，不含「卫生清洁记录」也不带编号'
  assert paths._DEPT_PATHS['A2'][22][-1] == '巡检计划执行记录表', \
    'A2 巡检记录在 2.3.4巡检管理/巡检计划执行记录表 下，靠 named 兜底认整棵子树'
  assert paths.item_names('c1', '廊坊', 'ZZ') == {}


def test_a5_clean_record_matched_by_dept_alias():
  """A5 seq24：上级模板先命中时子树兜底不生效，真实记录要靠部门别名进 hits。"""
  def rec(fid, leaf):
    segs = ['2.运行维护管理', '2.10环境管理', '2026年输出物', leaf]
    return _file(fid, leaf,
                 [{'fid': f'b{i}', 'name': s} for i, s in enumerate(segs)], segs)

  files = [rec('fp', '九月份清洁记录表.pdf'), rec('fb', '九月份清洁记录表-保洁.pdf'),
           rec('fx', '卫生清洁计划表.pdf')]
  items = {i.seq: i for i in MONTHLY_ITEMS}
  names = paths.item_names('c1', '廊坊', 'A5')
  got = fetch.match_item(items[24], files, extra_names=names[24])
  assert [f['fid'] for f in got] == ['fp', 'fb']
  assert fetch.match_item(items[24], files) == [], '原口径挑不出无编号的真实记录'


def test_extra_names_split_branch_and_team():
  """A1 形态：三个专业合订成一份「部门隐患排查表」，班组另有「班组级隐患排查表」。"""
  files = [_xm_file('fb', '部门隐患排查表.pdf'), _xm_file('ft', '班组级隐患排查表.pdf')]
  items = {i.seq: i for i in MONTHLY_ITEMS}
  names = paths.item_names('c1', '廊坊', 'A1')
  for seq in (29, 30, 31):
    assert fetch.match_item(items[seq], files) == [], '没有别名时关键词对不上'
    got = fetch.match_item(items[seq], files, extra_names=names[seq])
    assert [f['fid'] for f in got] == ['fb'], seq
    assert got[0]['match_dir'] == ''
  # 班组项的关键词本来就命中；别名结果必须一致（同属班组别名，不引入别的文件）
  assert [f['fid'] for f in fetch.match_item(items[32], files)] == ['ft']
  got = fetch.match_item(items[32], files, extra_names=names[32])
  assert [f['fid'] for f in got] == ['ft']


def test_dept_alias_on_directory_for_a10():
  """A10 的专业只写在目录名上（文件名注释不可信），别名要能命中目录段。"""
  def file_with_dir(fid, dir_name):
    segs = ['4.EHS管理', '4.1安全管理', '4.1.5隐患排查治理', '2026年', dir_name, '10月']
    return _file(fid, '隐患排查表-10月.pdf',
                 [{'fid': f'a{i}', 'name': s} for i, s in enumerate(segs)], segs)

  files = [
    file_with_dir('fs', '供配电部门隐患排查表'),
    file_with_dir('fr', '制冷部门隐患排查表'),
    file_with_dir('fi', '智能化部门隐患排查表'),
  ]
  items = {i.seq: i for i in MONTHLY_ITEMS}
  names = paths.item_names('c1', '廊坊', 'A10')
  for seq, fid in ((29, 'fs'), (30, 'fr'), (31, 'fi')):
    got = fetch.match_item(items[seq], files, extra_names=names[seq])
    assert [f['fid'] for f in got] == [fid], seq
  # 文件名不带专业时，原口径（编号 + 关键词）确实挑不出来 —— 别名是必需的
  assert fetch.match_item(items[30], files) == []


def test_a2_update_items_need_dept_alias():
  """A2 seq34/35 是查更新项：兜底永不生效，文件名不带编号，只能靠部门别名命中。"""
  def rec(fid, leaf, dir_name):
    segs = ['6.人员配置及组织管理', '6.3人员工时管理', dir_name, leaf]
    return _file(fid, leaf, [{'fid': f'u{i}', 'name': s} for i, s in enumerate(segs)], segs)

  files = [rec('f34', '2026年A2一线值班表（10月）.pdf', '人员排班计划表'),
           rec('f35', '考勤记录表（9月）.pdf', '人员考勤记录表')]
  items = {i.seq: i for i in MONTHLY_ITEMS}
  names = paths.item_names('c1', '廊坊', 'A2')
  assert [f['fid'] for f in fetch.match_item(items[34], files, extra_names=names[34])] == ['f34']
  assert [f['fid'] for f in fetch.match_item(items[35], files, extra_names=names[35])] == ['f35']
  assert fetch.match_item(items[34], files) == [], '无别名时挑不出来'


def test_b1m1_branch_and_team_aliases():
  """B1M1 三个专业合订成「部门隐患排查记录表」；班组另有「班组隐患排查」。"""
  def rec(fid, leaf):
    segs = ['4.EHS管理', '4.1安全管理', '4.1.5隐患排查治理', '2026年', leaf]
    return _file(fid, leaf, [{'fid': f'v{i}', 'name': s} for i, s in enumerate(segs)], segs)

  files = [rec('fb', '部门隐患排查记录表2026年10月.pdf'),
           rec('ft', '班组隐患排查2026年10月.pdf')]
  items = {i.seq: i for i in MONTHLY_ITEMS}
  names = paths.item_names('c1', '廊坊', 'B1M1')
  for seq in (29, 30, 31):
    assert fetch.match_item(items[seq], files) == [], f'{seq} 无别名时挑不出来'
    got = fetch.match_item(items[seq], files, extra_names=names[seq])
    assert [f['fid'] for f in got] == ['fb'], seq
  assert [f['fid'] for f in fetch.match_item(items[32], files, extra_names=names[32])] == ['ft']


def test_number_matches_after_year_prefix():
  """年份版本化目录已退回父目录，那里文件名以「年份+分隔符」开头，编号仍要能命中。"""
  assert fetch._starts_with_number('2026-2.3.4.4门禁巡检报警记录表.xlsx', '2.3.4.4')
  assert fetch._starts_with_number('2026年2.3.4.4门禁巡检报警记录表.xlsx', '2.3.4.4')
  assert fetch._starts_with_number('2.3.4.4门禁巡检报警记录表.xlsx', '2.3.4.4')
  # 无分隔符的 `20262.3.4.4…` 不剥年份，也不命中（`20262` 不以 `2.3.4.4` 开头）
  assert not fetch._starts_with_number('20262.3.4.4门禁巡检报警记录表.xlsx', '2.3.4.4')
  # 回归：`1.6.6.10` 仍不命中 `1.6.6.1`
  assert not fetch._starts_with_number('1.6.6.10 别的表', '1.6.6.1')


def test_period_container_warning():
  async def lister(_dir):
    return [_row('d10', '10月'), _row('d2026', '2026')]

  out = asyncio.run(paths.resolve_path('root', ('10月',), lister=lister))
  assert out['fid'] == 'd10' and '期号容器' in out['warn']
  out = asyncio.run(paths.resolve_path('root', ('2026',), lister=lister))
  assert out['fid'] == 'd2026' and '期号容器' in out['warn']

  async def none_lister(_dir):
    return [_row('dn', '运行维护管理')]

  out = asyncio.run(paths.resolve_path('root', ('运行维护管理',), lister=none_lister))
  assert out['fid'] == 'dn' and out['warn'] == ''


def test_resolve_uses_cache_and_reports_missing():
  cache = {str(SEQ): {'segments': [CONF_DIR], 'fid': 'd4'}}

  async def boom(_dir):
    raise AssertionError('缓存命中时不应再列目录')

  out = asyncio.run(paths.resolve('root', SEQ, (CONF_DIR,), cache=cache, lister=boom))
  assert out['from_cache'] and out['fid'] == 'd4'

  async def lister(_dir):
    return [_row('dn', '运行维护管理')]

  out = asyncio.run(paths.resolve('root', SEQ, ('不存在的目录',), lister=lister))
  assert not out['from_cache'] and not out['fid']
  assert '配置路径不存在' in out['error']


def _fake_tree(tree):
  async def fake_list_dir(dir_id):
    return [dict(r) for r in tree.get(dir_id, [])]

  return fake_list_dir


def _stub_dept(root_fid='root'):
  async def resolve_departments(_names=None, _companies=None):
    return [{'company_id': 'c1', 'company_name': '某公司', 'dept_id': 'd1',
             'dept_name': 'A1', 'root_fid': root_fid}]

  async def noop(*_args, **_kwargs):
    return None

  async def empty_cache(*_args, **_kwargs):
    return {}

  originals = (fetch.depts.resolve_departments, fetch._load_index, fetch._save_index,
               paths.load_cache, paths.save_cache, share.list_dir)
  fetch.depts.resolve_departments = resolve_departments
  fetch._load_index = noop
  fetch._save_index = noop
  paths.load_cache = empty_cache
  paths.save_cache = noop
  return originals


def _restore(originals):
  (fetch.depts.resolve_departments, fetch._load_index, fetch._save_index,
   paths.load_cache, paths.save_cache, share.list_dir) = originals


def _tree_with_copy():
  return {
    'root': [_row('d1', '2.运行维护管理'), _row('dx', '备份')],
    'd1': [_row('d2', '2.3值班管理')],
    'd2': [_row('d3', '2.3.4巡检管理')],
    'd3': [_row('d4', CONF_DIR)],
    'd4': [_row('f1', '2026-11-03班长巡检表.pdf', False)],
    'dx': [_row('d4x', CONF_DIR)],
    'd4x': [_row('f9', '2026-11-03班长巡检表.pdf', False)],
  }


def test_snapshot_uses_configured_path_only():
  origins = _stub_dept()
  paths.PATHS[('c1', 'A1')] = {SEQ: FULL}
  share.list_dir = _fake_tree(_tree_with_copy())
  try:
    snap = asyncio.run(fetch.take_dept_snapshot(only=[_item()]))
  finally:
    _restore(origins)
    paths.PATHS.pop(('c1', 'A1'), None)

  item = snap['depts'][0]['items'][0]
  assert item['mapped_by'] == 'path'
  assert tuple(item['config_path']) == FULL
  assert {f['fid'] for f in item['files']} == {'f1'}, '别处同名副本不算'
  assert item['actual_dirs'] == [CONF_DIR]
  assert item['error'] == ''


def test_snapshot_missing_when_config_path_absent():
  origins = _stub_dept()
  paths.PATHS[('c1', 'A1')] = {SEQ: FULL + ('不存在的子目录',)}
  share.list_dir = _fake_tree(_tree_with_copy())
  try:
    snap = asyncio.run(fetch.take_dept_snapshot(only=[_item()]))
    out = evaluate(snap, None)['items'][0]
  finally:
    _restore(origins)
    paths.PATHS.pop(('c1', 'A1'), None)

  assert out['mapped_by'] == 'path'
  assert out['status'] == STATUS_MISSING, '解析失败判「未找到」，不回落启发式'
  assert '配置路径不存在' in out['error'], out['error']
  assert out['file_count'] == 0


def _tree_22(tpl_name, leaf, year_dir, rec_name):
  """2.2运行管理 下既有父级模板 docx，也有真正的记录目录（年月目录在更下一层）。"""
  return {
    'root': [_row('s1', '廊坊-A1-数据中心运维管理体系')],
    's1': [_row('d1', '2.运行维护管理')],
    'd1': [_row('d2', '2.2运行管理')],
    'd2': [_row('tpl', tpl_name, False), _row('d3', leaf)],
    'd3': [_row('d4', year_dir)],
    'd4': [_row('f1', rec_name, False)],
  }


def _assert_seq_ignores_parent_template(seq, full, tree):
  origins = _stub_dept()
  paths.PATHS[('c1', 'A1')] = {seq: full}
  share.list_dir = _fake_tree(tree)
  try:
    snap = asyncio.run(fetch.take_dept_snapshot(only=[_item(seq)]))
  finally:
    _restore(origins)
    paths.PATHS.pop(('c1', 'A1'), None)

  item = snap['depts'][0]['items'][0]
  assert item['mapped_by'] == 'path'
  assert tuple(item['config_path']) == full
  assert [f['fid'] for f in item['files']] == ['f1'], '父目录模板 docx 不得命中'
  assert item['error'] == ''


def test_a1_seq12_ignores_parent_template():
  """A1 seq12 钉到实测叶子：父目录那份 2.2.8 模板 docx 不得再被当成当期记录。"""
  _assert_seq_ignores_parent_template(12, FULL12, _tree_22(
    '2.2.8润泽科技-廊坊-A1-视频监控录像查询记录单.docx', MONITOR_LEAF,
    '2026年视频监控查询记录单', '2.2.8视频监控录像查询记录单2026年10月.pdf'))


def test_a1_seq13_ignores_parent_template():
  """A1 seq13 同理：父目录那份 2.2.9 模板 docx 不得再被当成当期记录。"""
  _assert_seq_ignores_parent_template(13, FULL13, _tree_22(
    '2.2.9润泽科技-廊坊-A1-视频监控录像拷贝记录单.docx', COPY_LEAF,
    '2026年视频监控拷贝记录单', '2.2.9视频监控录像拷贝记录单2026年10月.pdf'))


def _tree_chapter_leaf(tpl_name, leaf, year_dir, rec_name):
  """章节目录下既有模板 docx（编号命名、无期号），也有记录文件夹（年月在更下一层）。"""
  return {
    'root': [_row('s1', '廊坊-A1-数据中心运维管理体系')],
    's1': [_row('d1', '2.运行维护管理')],
    'd1': [_row('d2', '2.3值班管理')],
    'd2': [_row('dc', '2.3.2数据中心监控室管理')],
    'dc': [_row('tpl', tpl_name, False), _row('dl', leaf)],
    'dl': [_row('dy', year_dir)],
    'dy': [_row('dm', '10月')],
    'dm': [_row('f1', rec_name, False)],
  }


def _assert_leaf_excludes_chapter_template(seq, tree):
  full = paths.configured('', '廊坊', 'A1')[seq]
  origins = _stub_dept()
  paths.PATHS[('c1', 'A1')] = {seq: full}
  share.list_dir = _fake_tree(tree)
  try:
    snap = asyncio.run(fetch.take_dept_snapshot(only=[_item(seq)]))
  finally:
    _restore(origins)
    paths.PATHS.pop(('c1', 'A1'), None)

  item = snap['depts'][0]['items'][0]
  assert item['mapped_by'] == 'path'
  assert tuple(item['config_path']) == full
  assert [f['fid'] for f in item['files']] == ['f1'], '章节目录的模板 docx 不得命中'
  assert item['error'] == ''


def test_leaf_excludes_chapter_template():
  """A1 seq15/16 钉到记录文件夹：2.3.2 章节目录里以编号命名的模板 docx 不再被当成记录。"""
  _assert_leaf_excludes_chapter_template(15, _tree_chapter_leaf(
    '2.3.2.2应急卡使用登记表.docx', '应急卡使用登记表',
    '2026年', '2.3.2.2应急卡使用登记表2026年10月.pdf'))
  _assert_leaf_excludes_chapter_template(16, _tree_chapter_leaf(
    '2.3.2.3钥匙使用登记表.docx', '钥匙使用登记表',
    '2026年', '2.3.2.3钥匙使用登记表2026年10月.pdf'))


def main() -> int:
  tests = [v for k, v in sorted(globals().items()) if k.startswith('test_')]
  for fn in tests:
    fn()
    print(f'  ok  {fn.__name__}')
  print(f'全部通过：{len(tests)} 项')
  return 0


if __name__ == '__main__':
  raise SystemExit(main())

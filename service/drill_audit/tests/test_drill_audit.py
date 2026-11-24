"""演练合规审查纯函数自检：不联网、不连库、不调真实模型。

运行：`python -m service.drill_audit.tests.test_drill_audit`
"""

import asyncio
import re
import sys
import types

from service.drill_audit import deliver, eop_spec, judge, process
from service.drill_audit.codes import (
  FINISH_STATUS, group_file, normalize_groups, speciality_hint, speciality_name,
)
from service.drill_audit.pipeline import (
  _group_rows, _pick_week, severity_counts, week_summary,
)


def t_codes():
  assert speciality_name('285gLxiBnkb') == '制冷'
  assert speciality_name('xxxx') == ''
  assert '应急操作流程' in speciality_hint('制冷单元故障应急操作演练')
  assert speciality_hint('补水系统故障+制冷单元故障应急操作演练').startswith('补水系统故障')
  assert normalize_groups('') == ['A', 'B', 'C', 'D']
  assert normalize_groups('b,A,b') == ['B', 'A']
  assert normalize_groups('x,Y') == ['A', 'B', 'C', 'D']
  detail = {'agroupFileId': 'f1', 'agroupFileName': 'a.pdf'}
  assert group_file(detail, 'A') == ('f1', 'a.pdf')
  assert group_file(detail, 'B') == ('', '')
  assert FINISH_STATUS['3'] == '已完成'


def t_pick_week():
  weeks = [
    {'id': 'w1', 'week': '1', 'status': 0, 'fileStatus': ''},
    {'id': 'w2', 'week': '2', 'status': 0, 'fileStatus': 'A,B,C,D'},
    {'id': 'w3', 'week': '3', 'status': 1, 'fileStatus': ''},
    {'id': 'w4', 'week': '4', 'status': 0, 'fileStatus': 'A'},
  ]
  assert _pick_week(weeks, 0)['id'] == 'w4'          # 优先有记录的最新已安排周
  assert _pick_week(weeks, 2)['id'] == 'w2'
  try:
    _pick_week(weeks, 3)                              # 第 3 周未安排
  except ValueError:
    pass
  else:
    raise AssertionError('未安排的周应当报错')
  try:
    _pick_week([], 0)
  except ValueError:
    pass
  else:
    raise AssertionError('没有已安排周应当报错')


def t_judge_parse():
  assert judge._loads('```json\n{"summary":"整体执行到位"}\n```')['summary'] == '整体执行到位'
  assert judge._loads('not json') == {}
  assert judge._loads('[1,2]') == {}
  # JSON 前后夹解释文字时也能取出（模型常见形态）
  assert judge._loads('结果如下：\n{"summary":"ok"}\n以上。')['summary'] == 'ok'
  issues = judge._normalize_issues([
    {'aspect': '步骤', 'problem': '漏步骤', 'eop_basis': '9.3', 'drill_evidence': 'A班组'},
    {'problem': '没有依据'},
    'garbage',
  ], group='B')
  assert len(issues) == 1 and issues[0]['aspect'] == '步骤'
  assert issues[0]['group'] == 'B'
  prompt = judge._truncate('x' * 50, 10)
  assert prompt.startswith('x' * 10) and '已截断' in prompt


def t_judge_no_truncate():
  """单班组文本全量进 prompt，且只含本班组记录。"""
  long_text = '第' * 30000
  system, user = judge.build_prompt(
    {'name': '制冷单元故障'}, 'B', long_text,
    [{'file_name': 'eop.pdf', 'content': 'EOP 要点'}],
  )
  assert '某一班组' in system
  assert '【B班组演练记录】' in user
  assert long_text in user            # 未被截断
  assert '已截断' not in user
  assert 'A班组' not in user and 'C班组' not in user and 'D班组' not in user


def t_judge_no_eop():
  result = asyncio.run(judge.audit_group({'name': 'x'}, 'A', 'text', []))
  assert result['issues'] == [] and result['summary'] == ''
  assert result['error'] == '知识库无相关 EOP 文档'
  assert 'verdict' not in result


def t_judge_empty_text():
  """空文本直接返回无法评价，不调模型。"""
  import core.model_gateway as mg

  original = mg.model_gateway.chat

  async def fake_chat(**kwargs):
    raise AssertionError('空文本不应调用模型')

  mg.model_gateway.chat = fake_chat
  try:
    result = asyncio.run(judge.audit_group(
      {'name': 'x'}, 'A', '   ', [{'file_name': 'e', 'content': 'c'}],
    ))
  finally:
    mg.model_gateway.chat = original
  assert result['error'] == 'OCR 未产出有效文本'
  assert 'verdict' not in result


def t_judge_with_mock_model():
  import core.model_gateway as mg

  original = mg.model_gateway.chat

  async def fake_chat(**kwargs):
    assert kwargs.get('json_mode') is True
    user = kwargs['messages'][1]['content']
    assert '【A班组演练记录】' in user
    assert 'B班组演练记录' not in user and 'C班组演练记录' not in user
    assert 'D班组演练记录' not in user
    return ('{"issues":['
            '{"aspect":"时限","problem":"未按时限通报","eop_basis":"9.2",'
            '"drill_evidence":"A班组记录未提通报","severity":"高","suggestion":"补拨打"}],'
            '"summary":"存在通报时限问题，其余步骤执行到位"}')

  mg.model_gateway.chat = fake_chat
  try:
    result = asyncio.run(judge.audit_group(
      {'name': '制冷单元故障'}, 'A', '演练记录',
      [{'file_name': 'eop.pdf', 'content': '9.2…'}],
    ))
  finally:
    mg.model_gateway.chat = original
  assert 'verdict' not in result
  assert len(result['issues']) == 1 and result['issues'][0]['eop_basis'] == '9.2'
  assert result['issues'][0]['group'] == 'A'
  assert result['summary'] == '存在通报时限问题，其余步骤执行到位'


def t_judge_retry_when_unparsable():
  """输出被截断/无法解析时放宽 max_tokens 重试一次；两次都失败才兜底。"""
  import core.model_gateway as mg

  original = mg.model_gateway.chat
  calls = []

  async def fake_chat(**kwargs):
    calls.append(kwargs.get('max_tokens'))
    if len(calls) == 1:
      return '{"issues":[{"aspect":"时限","problem":"未按时限'   # 被截断
    return '{"issues":[],"summary":"重试后正常"}'

  mg.model_gateway.chat = fake_chat
  try:
    result = asyncio.run(judge.audit_group(
      {'name': 'x'}, 'D', '演练记录', [{'file_name': 'e', 'content': 'c'}],
    ))
  finally:
    mg.model_gateway.chat = original
  assert calls == [3000, 6000]
  assert result['summary'] == '重试后正常' and 'error' not in result

  calls.clear()

  async def fake_chat_bad(**kwargs):
    calls.append(kwargs.get('max_tokens'))
    return '不是 JSON'

  mg.model_gateway.chat = fake_chat_bad
  try:
    result = asyncio.run(judge.audit_group(
      {'name': 'x'}, 'D', '演练记录', [{'file_name': 'e', 'content': 'c'}],
    ))
  finally:
    mg.model_gateway.chat = original
  assert len(calls) == 2 and result['error'] == '模型输出无法解析'


def t_severity_counts():
  assert severity_counts([]) == {'高': 0, '中': 0, '低': 0}
  counts = severity_counts([
    {'severity': '高'}, {'severity': '高'}, {'severity': '中'}, {'severity': ''},
  ])
  assert counts == {'高': 2, '中': 1, '低': 0}


def t_group_rows():
  """缺文件 / OCR 失败 / 无 EOP 各占一行，且都带原因；维度/漏做/做错/时间轴透传。"""
  found = [('A', 'f1', 'a.pdf'), ('B', '', ''), ('C', 'f3', 'c.pdf'),
           ('D', 'f4', 'd.pdf')]
  rows = _group_rows(found, {'A': 'text', 'D': 'text'}, {
    'A': {'issues': [{'severity': '高'}, {'severity': '中'}], 'summary': '整体到位',
          'dimensions': [{'key': 'role_assignment', 'verdict': '达标'}],
          'missing_steps': [{'eop_step': '用万用表测量 PDU 插孔电压'}],
          'step_findings': [{'seq': 1, 'status': '超时'}],
          'timeline_stats': {'count': 3}},
    'D': {'status': 'no_eop', 'issues': [], 'summary': '',
          'error': '知识库无相关 EOP 文档'},
  })
  by = {r['letter']: r for r in rows}
  assert by['A']['status'] == 'ok' and by['A']['summary'] == '整体到位'
  assert by['A']['issue_count'] == 2
  assert by['A']['severity_counts'] == {'高': 1, '中': 1, '低': 0}
  assert by['A']['error'] == ''
  # 结构化判定透传（缺失键补默认）
  assert by['A']['dimensions'][0]['key'] == 'role_assignment'
  assert by['A']['missing_steps'][0]['eop_step'] == '用万用表测量 PDU 插孔电压'
  assert by['A']['step_findings'][0]['seq'] == 1
  assert by['A']['timeline_stats'] == {'count': 3}
  assert by['B']['status'] == 'missing'
  assert by['B']['error'] == '该周没有记录文件'
  assert by['B']['dimensions'] == [] and by['B']['timeline_stats'] == {}
  assert by['C']['status'] == 'ocr_failed'
  assert by['C']['error'] == 'OCR 未产出有效文本'
  assert by['C']['missing_steps'] == [] and by['C']['step_findings'] == []
  assert by['D']['status'] == 'no_eop'
  assert by['D']['error'] == '知识库无相关 EOP 文档'
  assert by['D']['dimensions'] == [] and by['D']['step_findings'] == []


def t_week_summary():
  rows = [
    {'letter': 'A', 'status': 'ok', 'issue_count': 0, 'error': '', 'summary': '',
     'severity_counts': {'高': 0, '中': 0, '低': 0}},
    {'letter': 'B', 'status': 'ok', 'issue_count': 2, 'error': '', 'summary': '',
     'severity_counts': {'高': 1, '中': 1, '低': 0}},
    {'letter': 'C', 'status': 'missing', 'issue_count': 0,
     'error': '该周没有记录文件', 'summary': '',
     'severity_counts': {'高': 0, '中': 0, '低': 0}},
    {'letter': 'D', 'status': 'judge_failed', 'issue_count': 0,
     'error': 'D班组记录解析失败（TimeoutError）', 'summary': '',
     'severity_counts': {'高': 0, '中': 0, '低': 0}},
  ]
  text = week_summary(rows)
  assert '全周共 2 项问题（高 1、中 1）' in text
  assert 'A班组未发现与 EOP 不符的环节' in text
  assert 'B班组 2 项问题（高 1、中 1）' in text
  assert 'C班组无法评价（该周没有记录文件）' in text
  # error 里重复的班组前缀被剥掉
  assert 'D班组无法评价（记录解析失败（TimeoutError））' in text


def t_week_summary_clean():
  """全周零问题时明确说明，不报「无法评价」以外的噪音。"""
  rows = [
    {'letter': 'A', 'status': 'ok', 'issue_count': 0, 'error': '', 'summary': '',
     'severity_counts': {'高': 0, '中': 0, '低': 0}},
  ]
  assert week_summary(rows) == '全周未发现与 EOP 不符的环节：A班组未发现与 EOP 不符的环节'


def t_judge_groups_isolation():
  """单组异常只影响该组，其余组照常出结论。"""
  from service.drill_audit import pipeline

  original = pipeline.judge.audit_group
  calls = []

  async def fake_audit_group(plan, letter, text, eop_docs):
    calls.append(letter)
    if letter == 'B':
      raise RuntimeError('boom')
    return {'issues': [], 'summary': f'{letter} ok'}

  pipeline.judge.audit_group = fake_audit_group
  try:
    out = asyncio.run(pipeline._judge_groups(
      {'name': 'x'}, {'A': 'ta', 'B': 'tb'},
      [{'file_name': 'e', 'content': 'c'}],
    ))
  finally:
    pipeline.judge.audit_group = original
  assert sorted(calls) == ['A', 'B']
  assert out['A']['summary'] == 'A ok' and not out['A'].get('error')
  assert out['B']['summary'] == ''
  assert '判定失败' in out['B']['error']


def t_deliver_rows():
  """附件表格行由服务端渲染：列序固定、缺值补「—」。"""
  result = {
    'issues': [
      {'group': 'A', 'aspect': '步骤', 'problem': '漏步|骤', 'eop_basis': '9.3',
       'drill_evidence': 'A班组原文', 'severity': '高', 'suggestion': '补演练'},
      {'group': 'B', 'problem': '未填依据'},
    ],
  }
  rows = deliver.build_issue_rows(result)
  assert [r['序号'] for r in rows] == [1, 2]
  assert rows[0]['班组'] == 'A班组' and rows[0]['涉及环节'] == '步骤'
  assert rows[0]['严重程度'] == '高' and rows[0]['建议'] == '补演练'
  assert rows[1]['班组'] == 'B班组'
  assert rows[1]['涉及环节'] == '—' and rows[1]['EOP 依据'] == '—'
  table = deliver.markdown_table(list(deliver.ISSUE_COLUMNS), rows)
  assert table.startswith('| 序号 | 班组 | 涉及环节 |')
  assert r'漏步\|骤' in table
  assert deliver.markdown_table(list(deliver.ISSUE_COLUMNS), []) == ''


def t_normalize_ocr():
  """OCR 表格折叠：相邻重复单元格、分隔行、HTML 单元格与标签、重复行。"""
  raw = ('| 演练场景 | 演练场景 | 处置措施 |\n'
         '| --- | --- | --- |\n'
         '| 市电中断 | 市电中断 | 倒闸 |\n'
         '\n'
         '<table><tr><td>现场检查</td><td>现场检查</td></tr></table>\n'
         '**粗体** 与 &lt;标签&gt;\n'
         '重复行\n重复行\n')
  text = process.normalize_ocr_text(raw)
  assert '|' not in text and '---' not in text
  assert '演练场景 处置措施' in text
  assert '市电中断 倒闸' in text
  assert '现场检查' in text
  assert '粗体 与 <标签>' in text
  assert text.count('重复行') == 1


def t_extract_timeline():
  """卡片时间不误判、粘连分钟取末两位、按时间排序、卡片正文截掉。"""
  text = ('通报卡片发生时间：2026年04月09日10时51分0秒\n'
          '1分05秒 发送速报【事件概述】卡片正文不该进步骤\n'
          '0分11秒 值班长发现UPS放电报警\n'
          '告警值25.9506分40秒 现场检查并关闭断路器\n'
          '16分60秒 演练结束\n')
  steps = process.extract_timeline(text)
  # 16分60秒 越界秒数进位成 17分0秒
  assert [s['time'] for s in steps] == ['0分11秒', '1分5秒', '6分40秒', '17分0秒']
  assert steps[1]['text'] == '发送速报'
  assert all('【' not in s['text'] for s in steps)
  assert process.extract_timeline('') == []


def t_flow_mermaid():
  """泳道图：每步一个节点、一条泳道一竖列、无跨泳道连线、标签清洗与 `#`→号。"""
  roles = ('A指挥通报组：郭俊迪 尹朝露 B高压操作组：张月明 李正阳 '
           'C制冷操作组：张国 宋祖强')
  steps = [
    {'seconds': 11, 'time': '0分11秒', 'text': '值班长中控室发现UPS放电报警'},
    {'seconds': 33, 'time': '0分33秒', 'text': '值班长发送两分钟速报'},
    {'seconds': 72, 'time': '1分12秒', 'text': '值班长通知高压操作组前往现场检查2#高压柜'},
    {'seconds': 85, 'time': '1分25秒', 'text': '值班长通知制冷操作组查看2#制冷单元'},
    {'seconds': 120, 'time': '2分0秒', 'text': '张月明现场确认2#高压柜已合闸'},
    {'seconds': 200, 'time': '3分20秒', 'text': '张国现场将冷冻出水设定值恢复'},
    {'seconds': 320, 'time': '5分20秒', 'text': '值班长宣布演练结束'},
  ]
  flow = process.build_flow_mermaid(steps, roles)
  assert flow.startswith('```mermaid\nflowchart LR')
  # 三条泳道按首次出现排序，指挥泳道在前
  assert 'subgraph L0[指挥通报组]' in flow
  assert 'subgraph L1[高压操作组]' in flow
  assert 'subgraph L2[制冷操作组]' in flow
  # 每个步骤都成为节点（不漏）
  assert flow.count('["') == len(steps)
  # `#` 换「号」，敏感字符剔除
  assert '2号高压柜' in flow and '2#高压柜' not in flow
  # 每条泳道竖排一列（subgraph 内 direction TB），泳道之间左右并排
  assert flow.count('direction TB') == 3
  # **不画跨泳道连线**：dagre 的全局 rank 会把泳道框撑满整张图
  assert not re.search(r'^ {4}(\w)\d+ --> (?!\1)', flow, re.M)
  assert flow.count(' --> ') == len(steps) - 3
  # 泳道之间补隐形连线：没有它 mermaid 会把几条互不相连的竖排泳道叠成一列
  assert re.findall(r'(L\d) ~~~ (L\d)', flow) == [('L0', 'L1'), ('L1', 'L2')]
  assert flow.count(' ~~~ ') == 2
  # 时间单独一行，节点宽度只由正文决定
  assert '0分33秒<br/>' in flow
  # 少于 2 步不出图
  assert process.build_flow_mermaid([]) == ''
  assert process.build_flow_mermaid(steps[:1], roles) == ''

  # 无人员分配 → 关键词回退泳道，仍是非线性多泳道
  fallback = [
    {'seconds': 1, 'time': '0分1秒', 'text': '值班长发现UPS放电报警'},
    {'seconds': 30, 'time': '0分30秒', 'text': '关闭高压断路器'},
    {'seconds': 60, 'time': '1分0秒', 'text': '制冷机组恢复运行'},
  ]
  fb = process.build_flow_mermaid(fallback)
  assert 'flowchart LR' in fb and 'subgraph' in fb
  assert '供配电' in fb and '制冷与末端' in fb

  # 标签里的引号/尖括号被剔除，只留标签自带的引号
  bad = [{'seconds': 1, 'time': '0分1秒', 'text': '发现"告警#1<abc>'},
         {'seconds': 2, 'time': '0分2秒', 'text': '演练结束'}]
  bad_flow = process.build_flow_mermaid(bad)
  assert '<abc>' not in bad_flow and '告警#1' not in bad_flow
  assert bad_flow.count('"') == 4


def t_analyze_lanes():
  """analyze_record 额外返回分车道步骤，且不漏步（总数 == 平铺 steps 数）。"""
  raw = ('演练场景：双路市电中断\n'
         '人员分配：A指挥通报组：张三 B组(倒闸操作)：王五 C组(制冷)：钱七\n'
         '演练实际情况\n'
         '0分10秒 值班长通知B组倒闸操作\n'
         '0分25秒 王五执行高压倒闸\n'
         '0分40秒 钱七检查冷冻水温度\n'
         '1分05秒 值班长上报\n')
  info = process.analyze_record(raw)
  assert info['lanes'], info['lanes']
  assert sum(len(l['steps']) for l in info['lanes']) == len(info['steps'])
  for lane in info['lanes']:
    assert set(lane.keys()) == {'name', 'steps'}
    assert lane['name']
  # flow 仍保留（聊天/附件/全屏查看用）
  assert info['flow'].startswith('```mermaid')


def t_deliver_records():
  """演练过程记录 Markdown：场景 / 人员分配 / 编号步骤 / 流程图，无表格。"""
  result = {
    'plan': {'name': '制冷单元故障', 'speciality': '制冷', 'week': '3'},
    'groups': [
      {'letter': 'A', 'file_name': 'a.pdf', 'status': 'ok', 'summary': '整体到位'},
      {'letter': 'B', 'status': 'missing', 'error': '该周没有记录文件'},
    ],
  }
  raw = ('演练场景：双路市电中断\n'
         '人员分配：值班长 1 人，电工 2 人\n'
         '演练实际情况\n'
         '0分11秒 值班长发现UPS放电报警\n'
         '3分20秒 关闭断路器并切换旁路\n'
         '5分0秒 设备恢复正常，演练结束\n')
  md = deliver.render_records_markdown(result, {'A': raw})
  assert md.startswith('# 演练记录（OCR 整理）')
  assert '- 计划名称：制冷单元故障' in md
  assert '## 各组综合评价' in md
  assert '- A班组：整体到位' in md
  assert '## A班组演练记录（a.pdf）' in md
  assert '**演练场景**：双路市电中断' in md
  assert '**人员分配**：值班长 1 人，电工 2 人' in md
  assert '**演练过程**' in md
  assert '1. 0分11秒 值班长发现UPS放电报警' in md
  assert '2. 3分20秒 关闭断路器并切换旁路' in md
  assert '3. 5分0秒 设备恢复正常，演练结束' in md
  assert '```mermaid' in md and 'flowchart LR' in md
  assert '|' not in md
  assert '## B班组演练记录' in md
  assert '（该周没有记录文件）' in md
  plain = deliver.render_records_markdown(result, {'A': '没有时间轴的记录正文'})
  assert '没有时间轴的记录正文' in plain


def t_build_flows():
  """流程图挂到 groups[].flow：有 2+ 步的组才挂，空文本 / 单步 / 无时间轴的组不出现该键。"""
  raw = ('0分11秒 值班长发现UPS放电报警\n'
         '3分20秒 关闭断路器并切换旁路\n'
         '5分0秒 设备恢复正常，演练结束\n')
  texts = {'A': raw, 'B': '', 'C': '只有一行没有时间戳', 'D': '0分11秒 唯一一步'}
  flows = deliver.build_flows(texts)
  assert set(flows) == {'A'}
  assert flows['A'].startswith('```mermaid\nflowchart LR')

  result = {'groups': [
    {'letter': 'A', 'summary': '到位'},
    {'letter': 'B', 'status': 'missing'},
    {'letter': 'D', 'summary': '无法评价'},
  ], 'summary': '全周'}
  deliver.attach_flows(result, flows)
  assert result['groups'][0]['flow'] == flows['A']
  assert 'flow' not in result['groups'][1]
  assert 'flow' not in result['groups'][2]
  assert result['summary'] == '全周'
  deliver.attach_flows(result, {})            # 无图时什么都不加
  assert 'flow' not in result['groups'][1]


def t_parse_empty():
  from service.drill_audit.parse import pdf_bytes_to_text
  assert asyncio.run(pdf_bytes_to_text(b'')) == ''


def t_find_eop_retrieval_error():
  """检索链路异常必须抛 EopRetrievalError，不能伪装成「无命中」。"""
  from service.drill_audit import retrieve

  fake = types.ModuleType('service.rag.pipeline.rag_service')

  class _Broken:
    def hybrid_retrieve(self, *args, **kwargs):
      raise RuntimeError('milvus down')

  fake.rag_service = _Broken()
  names = ['service.rag', 'service.rag.pipeline', fake.__name__]
  saved = {n: sys.modules.get(n) for n in names}
  for parent in names[:2]:
    sys.modules.setdefault(parent, types.ModuleType(parent))
  sys.modules[fake.__name__] = fake
  try:
    asyncio.run(retrieve.find_eop('制冷单元故障 应急操作流程'))
  except retrieve.EopRetrievalError:
    pass
  else:
    raise AssertionError('检索不可用应抛 EopRetrievalError')
  finally:
    for n, mod in saved.items():
      if mod is None:
        sys.modules.pop(n, None)
      else:
        sys.modules[n] = mod


def t_step_metrics():
  """每步补 seq/at/delta，末步 delta=None；时间轴汇总数值正确。"""
  steps = [
    {'seconds': 11, 'time': '0分11秒', 'text': 'a'},
    {'seconds': 33, 'time': '0分33秒', 'text': 'b'},
    {'seconds': 72, 'time': '1分12秒', 'text': 'c'},
  ]
  out = process.add_step_metrics(steps)
  assert [s['seq'] for s in out] == [1, 2, 3]
  assert [s['at'] for s in out] == [11, 33, 72]
  assert [s['delta'] for s in out] == [22, 39, None]
  stats = process._timeline_stats(out)
  assert stats == {'count': 3, 'first_seconds': 11, 'total_seconds': 72,
                   'max_delta': 39, 'min_delta': 22, 'avg_delta': 30.5}
  empty = process._timeline_stats([])
  assert empty['count'] == 0 and empty['max_delta'] is None
  assert empty['total_seconds'] == 0
  # analyze_record 端到端带上 timeline_stats
  info = process.analyze_record('0分11秒 a\n0分33秒 b\n')
  assert info['timeline_stats']['count'] == 2


def t_eop_highlight():
  """抽 §9.1/§9.2/§9.3.x/§10.x 条款行；排除目录行与 9.10/9.11；全角归一。"""
  eop = [{'file_name': 'e.pdf', 'content': (
    '9.1 岗位分配：第一组人员负责监控\n'
    '9.1 岗位分配 ............. 5\n'
    '9.2 通知调度中心\n'
    '9.3.1 用万用表测量PDU插孔电压 执行人：第一组人员 时间限定：10秒\n'
    '9.10 附录A 设备清单\n'
    '9.11 附录B\n'
    '10.1 演练执行后检查项目\n'
    '１０.２ 检查电路连接\n'
    '这是普通正文，不含章节号\n'
  )}]
  out = eop_spec.highlight_lines(eop)
  assert '9.1 岗位分配：第一组人员负责监控' in out
  assert '9.2 通知调度中心' in out
  assert '9.3.1 用万用表测量PDU插孔电压' in out
  assert '10.1 演练执行后检查项目' in out
  assert '10.2 检查电路连接' in out            # 全角数字/句点归一
  assert '9.10' not in out and '9.11' not in out
  assert '普通正文' not in out
  assert '.....' not in out                     # 目录点线行排除
  assert eop_spec.highlight_lines([]) == ''
  assert eop_spec.highlight_lines(
    [{'file_name': 'x', 'content': '没有章节号的正文'}]) == ''


def t_eop_steps():
  """§9.3 → 必需步骤清单：同行标签 / 表格分栏 / 续行三种形态都抽到，字段不编造。"""
  eop = [{'file_name': 'eop.pdf', 'content': (
    '9.3 应急实施步骤\n'
    '9.3.1 用万用表测量 PDU 插孔电压 执行人：第一组人员 时间限定：10秒\n'
    '| 9.3.2 | 关闭漏水阀门 | 第二组人员 | 机房 | 30秒 |\n'
    '9.3.3 上报调度中心\n'
    '执行人：值班长 时间限定：60秒\n'
    '10.1 演练执行后检查项目\n'
  )}]
  steps = eop_spec.extract_steps(eop)
  assert [s['no'] for s in steps] == ['9.3.1', '9.3.2', '9.3.3']
  a = steps[0]
  assert a['action'] == '用万用表测量 PDU 插孔电压'
  assert a['executor'] == '第一组人员' and a['time_limit'] == '10秒'
  b = steps[1]
  assert b['action'] == '关闭漏水阀门' and b['executor'] == '第二组人员'
  assert b['location'] == '机房' and b['time_limit'] == '30秒'
  c = steps[2]
  assert c['action'] == '上报调度中心' and c['executor'] == '值班长'
  assert c['time_limit'] == '60秒'
  # 无 §9.3 返回空（不编造）
  assert eop_spec.extract_steps(
    [{'file_name': 'x', 'content': '没有 9.3 的正文'}]) == []


def t_normalize_dimensions():
  """始终补齐 5 个维度；无 EOP 依据的偏差降为「无法判定」。"""
  eop_text = '9.3.1 用万用表测量PDU插孔电压 执行人：第一组人员 时间限定：10秒'
  raw = [
    {'key': 'role_assignment', 'verdict': '达标', 'detail': '岗位齐全', 'basis': '9.1'},
    {'key': 'time_limit', 'verdict': '偏差', 'detail': '某步超时', 'basis': '完全不相干的依据'},
    {'key': 'compliance', 'verdict': '偏差', 'detail': '顺序错', 'basis': '用万用表测量PDU插孔电压'},
    {'key': '未知维度', 'verdict': '达标'},
  ]
  dims = judge._normalize_dimensions(raw, has_steps=True, eop_text=eop_text)
  assert [d['key'] for d in dims] == [
    'role_assignment', 'step_completeness', 'time_limit', 'compliance', 'critical_items']
  by = {d['key']: d for d in dims}
  assert by['role_assignment']['verdict'] == '达标'
  assert by['role_assignment']['name'] == '岗位分配'
  assert by['time_limit']['verdict'] == '无法判定'      # 无依据降级
  assert by['compliance']['verdict'] == '偏差'          # 有依据保留
  assert by['step_completeness']['verdict'] == '无法判定'  # 缺失补默认
  assert by['critical_items']['verdict'] == '无法判定'
  # 非法 verdict 归「无法判定」
  bad = judge._normalize_dimensions(
    [{'key': 'role_assignment', 'verdict': '优秀', 'basis': '9.1'}],
    has_steps=True, eop_text='')
  assert bad[0]['verdict'] == '无法判定'
  # 步骤未结构化 → 步骤完整性加说明
  no_steps = judge._normalize_dimensions([], has_steps=False, eop_text='')
  sc = {d['key']: d for d in no_steps}['step_completeness']
  assert sc['verdict'] == '无法判定'
  assert sc['detail'] == 'EOP 步骤未结构化，仅粗判'


def t_missing_steps():
  """漏做步骤：能对上清单就回填执行人/时限；无法印证的丢弃；每条镜像进问题清单。"""
  eop_steps = [{'no': '9.3.1', 'action': '用万用表测量 PDU 插孔电压',
                'executor': '第一组人员', 'location': '机房', 'time_limit': '10秒'}]
  eop_text = ('9.3.1 用万用表测量 PDU 插孔电压 执行人：第一组人员 时间限定：10秒；'
              '禁止带负荷拉刀闸')
  raw = [
    {'eop_step': '用万用表测量 PDU 插孔电压', 'severity': '高', 'note': '未测电压'},
    {'eop_step': '禁止带负荷拉刀闸'},
    {'eop_step': '完全不存在的东西'},
  ]
  missing, mirrored = judge._normalize_missing(raw, 'A', eop_steps, eop_text)
  assert len(missing) == 2
  m0 = missing[0]
  assert m0['eop_step'] == '用万用表测量 PDU 插孔电压'
  assert m0['executor'] == '第一组人员' and m0['time_limit'] == '10秒'  # 清单回填
  assert m0['severity'] == '高' and m0['note'] == '未测电压'
  m1 = missing[1]
  assert m1['eop_step'] == '禁止带负荷拉刀闸'
  assert m1['executor'] == '' and m1['severity'] == '中'   # 无对照默认中
  assert len(mirrored) == 2
  assert all(m['dimension'] == 'step_completeness' for m in mirrored)
  assert all('漏做' in m['problem'] for m in mirrored)


def t_step_findings_override():
  """做错的步：actual 由服务端按 metric 覆盖，越界/非法丢弃，上限 12 条。"""
  steps = [
    {'seq': 1, 'at': 11, 'delta': 22, 'text': 'a'},
    {'seq': 2, 'at': 33, 'delta': 39, 'text': 'b'},
    {'seq': 3, 'at': 72, 'delta': None, 'text': 'c'},
  ]
  eop_text = '时间限定：10秒'
  raw = [
    {'seq': 1, 'status': '超时', 'metric': '本步', 'limit': '10秒'},
    {'seq': 2, 'status': '超时', 'metric': '累计', 'limit': '10秒'},
    {'seq': 99, 'status': '超时', 'metric': '本步', 'limit': '10秒'},   # 越界
    {'seq': 1, 'status': '错误', 'metric': '本步', 'limit': '10秒'},    # status 非法
    {'seq': 1, 'status': '违规', 'metric': '本步', 'limit': '不存在的东西'},  # limit 无依据
  ]
  out = judge._normalize_findings(raw, steps, eop_text)
  assert len(out) == 2
  by_seq = {f['seq']: f for f in out}
  assert by_seq[1]['metric'] == '本步' and by_seq[1]['actual'] == '22秒'
  assert by_seq[2]['metric'] == '累计' and by_seq[2]['actual'] == '33秒'
  # 超过 12 条截断
  many_steps = [{'seq': i, 'at': i, 'delta': 1, 'text': 't'} for i in range(1, 21)]
  many_raw = [{'seq': i, 'status': '超时', 'metric': '本步', 'limit': '10秒'}
              for i in range(1, 16)]
  capped = judge._normalize_findings(many_raw, many_steps, eop_text)
  assert len(capped) == 12
  # 无步骤直接返回空
  assert judge._normalize_findings(raw, [], eop_text) == []


def t_judge_legacy_output():
  """旧结构（仅 issues+summary）也被接受：维度补齐、漏做/做错为空，时间轴仍在。"""
  import core.model_gateway as mg

  original = mg.model_gateway.chat

  async def fake_chat(**kwargs):
    return ('{"issues":[{"aspect":"时限","problem":"通报晚","eop_basis":"9.2",'
            '"drill_evidence":"记录无通报","severity":"中","suggestion":"补"}],'
            '"summary":"整体可接受"}')

  mg.model_gateway.chat = fake_chat
  try:
    result = asyncio.run(judge.audit_group(
      {'name': 'x'}, 'B', '0分11秒 发现告警\n0分33秒 上报\n',
      [{'file_name': 'e.pdf', 'content': '9.2 通知调度中心'}],
    ))
  finally:
    mg.model_gateway.chat = original
  assert len(result['dimensions']) == 5
  assert all(d['verdict'] == '无法判定' for d in result['dimensions'])
  assert result['missing_steps'] == [] and result['step_findings'] == []
  assert len(result['issues']) == 1 and result['issues'][0]['group'] == 'B'
  assert result['summary'] == '整体可接受'
  assert result['timeline_stats']['count'] == 2


def main():
  checks = [t_codes, t_pick_week, t_judge_parse, t_judge_no_truncate,
            t_judge_no_eop, t_judge_empty_text, t_judge_with_mock_model,
            t_judge_retry_when_unparsable, t_judge_legacy_output,
            t_severity_counts, t_group_rows, t_week_summary, t_week_summary_clean,
            t_judge_groups_isolation, t_deliver_rows, t_normalize_ocr,
            t_extract_timeline, t_step_metrics, t_eop_highlight, t_eop_steps,
            t_normalize_dimensions, t_missing_steps, t_step_findings_override,
            t_flow_mermaid, t_analyze_lanes, t_deliver_records,
            t_build_flows, t_parse_empty, t_find_eop_retrieval_error]
  for fn in checks:
    fn()
    print(f'ok {fn.__name__}')
  print(f'\n{len(checks)} 项自检全部通过')


if __name__ == '__main__':
  main()

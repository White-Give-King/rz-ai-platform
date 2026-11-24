"""LLM 逐班组评价：**每个班组一次** JSON 模式调用，按维度对照 EOP 判、并查漏做步骤。

原则：
- **按业务维度判，不抠文字字眼**：岗位分配 / 步骤完整性（核心·查漏）/ 时限执行 /
  操作合规 / 关键项；
- **步骤完整性优先**：把 EOP §9.3 拆成必需步骤清单（`eop_spec.extract_steps`），逐条在
  演练时间轴里找对应动作 —— 语义对应即可，**EOP 有而演练没做就是漏做**（写进
  `missing_steps` 并镜像进问题清单）；
- 时限口径：EOP 多数按**累计**时间，措辞为「用时」时按**单步**；时间轴每步都给两个值，
  由判定按 EOP 措辞选口径；
- 只依据给定 EOP 判断；无依据的偏差/缺失降为「无法判定」，不凭空调低；
- 不输出「合规 / 不合规」二元档位；调用/解析失败 fail-open。

schemas 见 `_SYSTEM`；解析后的归一化见 `_parse` 及各 `_normalize_*`。
"""

import json
import logging
import re

from config.settings import settings

from . import eop_spec, process

logger = logging.getLogger(__name__)

_EOP_BUDGET = 8000
_HIGHLIGHT_BUDGET = 2000
_FINDINGS_LIMIT = 12
_FENCE_RE = re.compile(r'^```[a-zA-Z]*\s*|\s*```$')

# 五个考核维度（有序；页面与落库都按这个顺序）
_DIMENSIONS = (
  ('role_assignment', '岗位分配'),
  ('step_completeness', '步骤完整性'),
  ('time_limit', '时限执行'),
  ('compliance', '操作合规'),
  ('critical_items', '关键项'),
)
_DIM_LABEL = dict(_DIMENSIONS)
_VERDICTS = ('达标', '偏差', '缺失', '无法判定')
_FINDING_STATUS = ('超时', '违规')
_SEVERITY = ('高', '中', '低')

_SYSTEM = (
  '你是数据中心应急演练审查专家。用户会给出一项应急演练的计划信息、知识库中对应的 EOP'
  '（应急操作流程）标尺（岗位分配 / 应急实施步骤 / 执行后检查）、EOP 必需步骤清单，以及'
  '**某一班组**演练记录的时间轴（每步带累计时间与本步耗时）与原文（来自扫描件 OCR，可能有'
  '缺漏）。请**按业务维度**对照 EOP 判定，而不是抠文字字眼。\n'
  '五个维度（dimensions）：\n'
  '- role_assignment 岗位分配：EOP 要求的岗位/班组是否齐全、职责是否落实；\n'
  '- step_completeness 步骤完整性【核心·查漏】：逐条核对下面「EOP 必需步骤清单」里的每个'
  '步骤，在演练时间轴里找对应动作——**语义对应即可**（不要求字面相同，如「用万用表测量 PDU'
  '插孔电压」演练里做了万用表测压就算做到）；**EOP 有而演练没做 = 漏做**，逐一写进'
  'missing_steps（关键步骤给「高」，其余给「中」）；\n'
  '- time_limit 时限执行：对照 EOP 的时间限定判本步是否超时；\n'
  '- compliance 操作合规：执行人/位置/顺序是否符合 EOP 及安全规程；\n'
  '- critical_items 关键项：EOP 标注的关键/禁止项是否做到。\n'
  '规则：\n'
  '1. 只能依据给定的 EOP 判断；EOP 未提及的要求不得作为问题依据。每个维度与每条判定都要'
  '给出 basis（引用的 EOP 具体条款/要点）；无 EOP 依据时该维度判「无法判定」，不要凭空下结论。\n'
  '2. 时限口径：EOP 的时间限定多数是从事件开始的**累计**时间，措辞为「用时/本步耗时」时按'
  '**单步**耗时。时间轴每步都给了「累计」与「本步」两个值，按 EOP 措辞选对应口径比较。\n'
  '3. 漏做（missing_steps）与做错（step_findings）分开：漏做没有演练序号；step_findings 只放'
  '演练里**实际做了但超时/违规**的步（带演练 seq，status 只能是 超时 或 违规）。\n'
  '4. 记录缺失、OCR 失败或无法辨认的部分不要臆测，也不要把它们当作漏做或问题。\n'
  '5. 不输出「合规 / 不合规」这种二元档位；只给维度判定、漏做与问题清单，以及 2~4 句综合'
  '评价。评价要与问题数量和严重程度相称：问题少或只是个别细节偏差时先肯定已做到的部分、'
  '说明影响范围，不要因个别小问题否定整个班组。\n'
  '只输出 JSON：'
  '{"dimensions":[{"key":"role_assignment|step_completeness|time_limit|compliance|'
  'critical_items","verdict":"达标|偏差|缺失|无法判定","detail":"...","basis":"..."}],'
  '"missing_steps":[{"eop_step":"EOP 步骤原文","executor":"...","time_limit":"...",'
  '"severity":"高|中|低","note":"影响/说明","basis":"..."}],'
  '"step_findings":[{"seq":1,"status":"超时|违规","metric":"累计|本步","limit":"...",'
  '"actual":"...","note":"..."}],'
  '"issues":[{"dimension":"role_assignment|step_completeness|time_limit|compliance|'
  'critical_items","aspect":"...","problem":"...","eop_basis":"...",'
  '"drill_evidence":"...","severity":"高|中|低","suggestion":"...","seq":1}],'
  '"summary":"..."}'
  '；没有问题时 issues / missing_steps / step_findings 都为空数组。'
)


def _truncate(text: str, limit: int) -> str:
  text = text or ''
  if len(text) <= limit:
    return text
  return text[:limit] + f'\n（已截断，原文共 {len(text)} 字）'


def _eop_text(eop_docs: list[dict]) -> str:
  return '\n'.join(str((doc or {}).get('content') or '') for doc in eop_docs or [])


def _eop_block(eop_docs: list[dict], steps: list[dict]) -> str:
  """EOP 输入块：要点高亮（≤2000）置顶 + 必需步骤清单 + 原始 EOP（合计 ≤8000）。"""
  parts: list[str] = []
  used = 0
  highlight = eop_spec.highlight_lines(eop_docs, _HIGHLIGHT_BUDGET)
  if highlight:
    parts.append('【EOP 岗位/时限/步骤 要点】\n' + highlight)
    used += len(highlight)
  checklist = _checklist_text(steps)
  if checklist:
    parts.append('【EOP 必需步骤清单（逐条查漏）】\n' + checklist)
    used += len(checklist)

  budget = max(_EOP_BUDGET - used, 0)
  raw_parts: list[str] = []
  for doc in eop_docs or []:
    name = doc.get('file_name') or '未知文档'
    body = _truncate(str(doc.get('content') or ''), max(budget, 0))
    raw_parts.append(f'【{name}】\n{body}')
    budget -= len(body)
    if budget <= 0:
      break
  raw = '\n\n'.join(raw_parts) or '（知识库无相关 EOP 文档）'
  parts.append('【EOP 原文】\n' + raw)
  return '\n\n'.join(parts)


def _checklist_text(steps: list[dict]) -> str:
  lines: list[str] = []
  for step in steps or []:
    extra = []
    if step.get('executor'):
      extra.append(f"执行人：{step['executor']}")
    if step.get('location'):
      extra.append(f"执行位置：{step['location']}")
    if step.get('time_limit'):
      extra.append(f"时间限定：{step['time_limit']}")
    tail = '（' + '，'.join(extra) + '）' if extra else ''
    lines.append(f"{step.get('no') or ''} {step.get('action') or ''}{tail}".strip())
  return '\n'.join(lines)


def _timeline_block(analyzed: dict | None) -> str:
  """压缩时间轴：每步 `#seq 累计X秒 本步X秒 [车道] 摘要`，让判定直接拿到耗时。"""
  steps = (analyzed or {}).get('steps') or []
  if not steps:
    return ''
  lane_of: dict = {}
  for lane in (analyzed or {}).get('lanes') or []:
    for step in lane.get('steps') or []:
      if step.get('seq') is not None:
        lane_of[step['seq']] = lane.get('name') or ''
  lines: list[str] = []
  for step in steps:
    at = step.get('at', step.get('seconds'))
    delta = step.get('delta')
    delta_text = '—' if delta is None else f'{delta}秒'
    lane = lane_of.get(step.get('seq'), '') or '其他'
    summary = (step.get('text') or '')[:30]
    lines.append(
      f"#{step.get('seq')} 累计{at}秒 本步{delta_text} [{lane}] {summary}")
  return '\n'.join(lines)


def build_prompt(plan: dict, letter: str, text: str, eop_docs: list[dict],
                 analyzed: dict | None = None,
                 eop_steps: list[dict] | None = None) -> tuple[str, str]:
  """→ `(system, user)`。一次只喂**一个班组**的演练记录，不截断。"""
  if eop_steps is None:
    eop_steps = eop_spec.extract_steps(eop_docs)
  eop_block = _eop_block(eop_docs, eop_steps)

  payload: dict = {
    '计划信息': {
      '名称': plan.get('name') or '',
      '专业': plan.get('speciality') or '',
      '部门': plan.get('dept') or '',
      '年份': plan.get('year') or '',
      '周次': plan.get('week') or '',
      '起止日期': plan.get('dates') or '',
      '完成状态': plan.get('finish_status') or '',
    },
    '班组': f'{letter}班组',
    'EOP': eop_block,
  }
  timeline = _timeline_block(analyzed)
  if timeline:
    payload['时间轴（每步：seq / 累计 / 本步 / 车道 / 摘要）'] = timeline
  payload['演练记录'] = f'【{letter}班组演练记录】\n{text or "（无有效文本）"}'
  user = json.dumps(payload, ensure_ascii=False, indent=1)
  return _SYSTEM, user


def _extract_json(text: str) -> dict:
  """模型偶尔在 JSON 前后夹解释文字：取首个 { 到末个 } 的片段再解析。"""
  start = text.find('{')
  end = text.rfind('}')
  if start < 0 or end <= start:
    return {}
  try:
    data = json.loads(text[start:end + 1])
  except (TypeError, ValueError):
    return {}
  return data if isinstance(data, dict) else {}


def _loads(raw: str) -> dict:
  text = _FENCE_RE.sub('', (raw or '').strip())
  try:
    data = json.loads(text)
  except (TypeError, ValueError):
    return _extract_json(text)
  return data if isinstance(data, dict) else {}


def _grounded(text: str, corpus: str) -> bool:
  """`text` 能否在 `corpus`（EOP 原文）里找到凭据；空 corpus 视为无法核验 → 放行。"""
  text = (text or '').strip()
  if not text:
    return False
  if not corpus:
    return True
  if text in corpus:
    return True
  tokens = [t for t in re.split(r'[\s，,；;、。.!！?？（）()]+', text) if len(t) >= 2]
  return any(t in corpus for t in tokens)


def _strip_ws(text: str) -> str:
  return re.sub(r'\s+', '', text or '')


def _match_step(eop_step: str, eop_steps: list[dict]) -> dict | None:
  """把模型写的漏做步骤对上 EOP 清单里的一条（双向子串，取最长匹配）。"""
  target = _strip_ws(eop_step)
  best: dict | None = None
  for step in eop_steps or []:
    action = _strip_ws(step.get('action') or '')
    if not action:
      continue
    if action in target or target in action:
      if best is None or len(action) > len(_strip_ws(best.get('action') or '')):
        best = step
  return best


def _normalize_issues(raw_issues, group: str = '') -> list[dict]:
  out: list[dict] = []
  for item in raw_issues if isinstance(raw_issues, list) else []:
    if not isinstance(item, dict):
      continue
    problem = str(item.get('problem') or '').strip()
    basis = str(item.get('eop_basis') or '').strip()
    evidence = str(item.get('drill_evidence') or '').strip()
    if not (problem and basis and evidence):
      continue
    entry = {
      'group': group,
      'dimension': str(item.get('dimension') or '').strip(),
      'aspect': str(item.get('aspect') or '').strip(),
      'problem': problem,
      'eop_basis': basis,
      'drill_evidence': evidence,
      'severity': str(item.get('severity') or '').strip(),
      'suggestion': str(item.get('suggestion') or '').strip(),
    }
    seq = item.get('seq')
    if isinstance(seq, int):
      entry['seq'] = seq
    out.append(entry)
  return out


def _empty_dims() -> list[dict]:
  return [{'key': key, 'name': name, 'verdict': '无法判定',
           'detail': '', 'basis': ''} for key, name in _DIMENSIONS]


def _normalize_dimensions(raw_dims, has_steps: bool = True,
                          eop_text: str = '') -> list[dict]:
  """始终补齐 5 个维度；偏差/缺失无 EOP 依据的降为「无法判定」。"""
  by_key: dict[str, dict] = {}
  for item in raw_dims if isinstance(raw_dims, list) else []:
    if not isinstance(item, dict):
      continue
    key = str(item.get('key') or '').strip()
    if key not in _DIM_LABEL:
      continue
    verdict = str(item.get('verdict') or '').strip()
    if verdict not in _VERDICTS:
      verdict = '无法判定'
    basis = str(item.get('basis') or '').strip()
    if verdict in ('偏差', '缺失') and not _grounded(basis, eop_text):
      verdict = '无法判定'
    by_key[key] = {
      'key': key, 'name': _DIM_LABEL[key], 'verdict': verdict,
      'detail': str(item.get('detail') or '').strip(), 'basis': basis,
    }
  out: list[dict] = []
  for key, name in _DIMENSIONS:
    entry = dict(by_key.get(key) or {
      'key': key, 'name': name, 'verdict': '无法判定', 'detail': '', 'basis': ''})
    if key == 'step_completeness' and not has_steps:
      note = 'EOP 步骤未结构化，仅粗判'
      entry['detail'] = (f"{entry['detail']}（{note}）".strip()
                         if entry['detail'] else note)
    out.append(entry)
  return out


def _normalize_missing(raw_missing, group: str, eop_steps: list[dict],
                       eop_text: str) -> tuple[list[dict], list[dict]]:
  """漏做步骤：能对上清单就把执行人/时限回填（不信模型抄写），并镜像进问题清单。"""
  missing: list[dict] = []
  mirrored: list[dict] = []
  for item in raw_missing if isinstance(raw_missing, list) else []:
    if not isinstance(item, dict):
      continue
    eop_step = str(item.get('eop_step') or item.get('step') or '').strip()
    basis = str(item.get('basis') or item.get('eop_basis') or '').strip()
    if not eop_step:
      continue
    matched = _match_step(eop_step, eop_steps)
    grounded = (matched is not None or _grounded(eop_step, eop_text)
                or _grounded(basis, eop_text))
    if not grounded:
      continue
    executor = (matched.get('executor') if matched else '') or ''
    time_limit = (matched.get('time_limit') if matched else '') or ''
    severity = str(item.get('severity') or '').strip()
    if severity not in _SEVERITY:
      severity = '中'
    note = str(item.get('note') or '').strip()
    final_basis = basis or eop_step
    missing.append({
      'group': group, 'eop_step': eop_step, 'executor': executor,
      'time_limit': time_limit, 'severity': severity, 'note': note,
      'basis': final_basis,
    })
    mirrored.append({
      'group': group, 'dimension': 'step_completeness', 'aspect': '漏做步骤',
      'problem': f'漏做 EOP 步骤：{eop_step}',
      'eop_basis': final_basis,
      'drill_evidence': note or '演练记录中未见对应操作',
      'severity': severity, 'suggestion': '补充演练该步骤并留痕',
    })
  return missing, mirrored


def _normalize_findings(raw_findings, steps: list[dict], eop_text: str) -> list[dict]:
  """做错的步（超时/违规）：seq 落在有效范围，actual 按 metric 由服务端覆盖。"""
  if not steps:
    return []
  seq_map = {s.get('seq'): s for s in steps if s.get('seq') is not None}
  limit_n = len(steps)
  out: list[dict] = []
  for item in raw_findings if isinstance(raw_findings, list) else []:
    if not isinstance(item, dict):
      continue
    try:
      seq = int(item.get('seq'))
    except (TypeError, ValueError):
      continue
    if seq < 1 or seq > limit_n or seq not in seq_map:
      continue
    status = str(item.get('status') or '').strip()
    if status not in _FINDING_STATUS:
      continue
    limit = str(item.get('limit') or '').strip()
    if not _grounded(limit, eop_text):
      continue
    step = seq_map[seq]
    metric = str(item.get('metric') or '').strip()
    if metric not in ('累计', '本步'):
      metric = '本步' if step.get('delta') is not None else '累计'
    if metric == '累计':
      actual = f"{step.get('at')}秒"
    else:
      actual = '—' if step.get('delta') is None else f"{step.get('delta')}秒"
    out.append({
      'seq': seq, 'status': status, 'metric': metric, 'limit': limit,
      'actual': actual, 'note': str(item.get('note') or '').strip(),
    })
  return out[:_FINDINGS_LIMIT]


def _fallback(reason: str) -> dict:
  return {'issues': [], 'summary': '', 'dimensions': _empty_dims(),
          'missing_steps': [], 'step_findings': [], 'timeline_stats': {},
          'error': reason}


async def audit_group(plan: dict, letter: str, text: str,
                      eop_docs: list[dict]) -> dict:
  """评价**一个班组** → `{dimensions, missing_steps, step_findings, timeline_stats,
  issues[带 group], summary}`（失败多一个 `error`）。"""
  if not (text or '').strip():
    return _fallback('OCR 未产出有效文本')
  if not eop_docs:
    return {'issues': [], 'summary': '', 'error': '知识库无相关 EOP 文档'}

  from core.model_gateway import model_gateway

  analyzed = process.analyze_record(text)
  eop_steps = eop_spec.extract_steps(eop_docs)
  eop_text = _eop_text(eop_docs)
  system, user = build_prompt(plan, letter, text, eop_docs,
                              analyzed=analyzed, eop_steps=eop_steps)
  messages = [{'role': 'system', 'content': system},
              {'role': 'user', 'content': user}]

  async def _invoke(limit: int) -> str:
    return await model_gateway.chat(
      messages=messages, temperature=0, max_tokens=limit, json_mode=True,
      timeout=settings.DRILL_AUDIT_LLM_TIMEOUT, caller='drill_audit',
    )

  def _parse(raw: str) -> dict | None:
    data = _loads(raw)
    raw_issues = data.get('issues')
    raw_dims = data.get('dimensions')
    raw_missing = data.get('missing_steps')
    raw_findings = data.get('step_findings')
    summary = str(data.get('summary') or '').strip()
    # 有效性用**原始载荷**判定：归一化后的 dimensions 永远 5 条，会吞掉重试
    if not (raw_issues or summary or raw_dims is not None
            or raw_missing is not None or raw_findings is not None):
      return None
    steps = analyzed.get('steps') or []
    issues = _normalize_issues(raw_issues, group=letter)
    missing, mirrored = _normalize_missing(raw_missing, letter, eop_steps, eop_text)
    issues = issues + mirrored
    findings = _normalize_findings(raw_findings, steps, eop_text)
    dims = _normalize_dimensions(raw_dims, has_steps=bool(eop_steps),
                                 eop_text=eop_text)
    return {'dimensions': dims, 'missing_steps': missing,
            'step_findings': findings, 'issues': issues, 'summary': summary,
            'timeline_stats': analyzed.get('timeline_stats') or {}}

  try:
    raw = await _invoke(3000)
  except Exception as e:  # noqa: BLE001 - 判定失败不拖垮整轮
    logger.warning('[DrillAudit] %s班组判定模型调用失败: %s', letter, str(e)[:200])
    return _fallback(f'模型调用失败：{type(e).__name__}')

  result = _parse(raw)
  if result is not None:
    return result

  # JSON 被 max_tokens 截断或前后夹了说明文字：放宽输出上限重试一次（成功路径只调一次）
  logger.warning('[DrillAudit] %s班组判定输出无法解析，放宽上限重试 raw=%s',
                 letter, (raw or '')[:200])
  try:
    raw = await _invoke(6000)
  except Exception as e:  # noqa: BLE001
    logger.warning('[DrillAudit] %s班组判定重试失败: %s', letter, str(e)[:200])
    return _fallback('模型输出无法解析')
  result = _parse(raw)
  if result is not None:
    return result
  logger.warning('[DrillAudit] %s班组判定输出无法解析 raw=%s', letter, (raw or '')[:200])
  return _fallback('模型输出无法解析')

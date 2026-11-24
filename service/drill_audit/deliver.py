"""把演练审查结果做成对话内可交互查看的附件（服务端生成，模型只原样输出）。

两类附件：

1. **问题明细交互表** —— 每条问题一行（班组 / 涉及环节 / 问题 / EOP 依据 /
   演练记录证据 / 严重程度 / 建议）；≤20 条时同时给服务端渲染的文字表。
2. **演练过程记录** —— 各组 OCR 文本确定性整理成「演练场景 / 人员分配 / 演练过程
   （时间轴文字）/ 流程图」的 Markdown，不转贴 OCR 表格。演练记录是扫描件，原始
   PDF 不便阅读，在线查看用整理后的文本；不暴露原始 PDF。

每组流程图同时写进 `result['groups'][].flow`（与附件同源），由模型原样贴在回答正文里，
前端本地渲染 —— 用户不用打开附件就能直观看到演练过程。

检索命中的 EOP 文档清单不做附件，只以 `eop_refs` 随返回下发（模型按需口头说明）。

文件落在本会话目录（数据集 / workspace），文件名纯 ASCII（中文展示名放
`<file name>`）；任一附件生成失败只记日志，**不影响审查结论**。
"""

import asyncio
import logging
import uuid
from urllib.parse import quote

from core.sandbox.datasets import resolve_artifact_url, save_dataset, thread_dir

from . import store
from .process import analyze_record, normalize_ocr_text

logger = logging.getLogger(__name__)

_WORKSPACE_ID = 'workspace'
_INLINE_MAX_ROWS = 20          # ≤20 条同时给文字表，>20 条只给交互表格

ISSUE_COLUMNS = ('序号', '班组', '涉及环节', '问题', 'EOP 依据', '演练记录证据',
                 '严重程度', '建议')
_STATUS_LABELS = {
  'missing': '该周没有记录文件',
  'ocr_failed': '记录解析失败',
  'no_eop': '知识库无相关 EOP 文档',
  'judge_failed': '模型判定失败',
}


def build_issue_rows(result: dict) -> list[dict]:
  """问题清单 → 交互表行（列与列序固定，模型不得增删）。"""
  rows: list[dict] = []
  for idx, item in enumerate(result.get('issues') or [], start=1):
    item = item if isinstance(item, dict) else {}
    letter = str(item.get('group') or '').strip()
    rows.append({
      '序号': idx,
      '班组': f'{letter}班组' if letter else '—',
      '涉及环节': item.get('aspect') or '—',
      '问题': item.get('problem') or '—',
      'EOP 依据': item.get('eop_basis') or '—',
      '演练记录证据': item.get('drill_evidence') or '—',
      '严重程度': item.get('severity') or '—',
      '建议': item.get('suggestion') or '—',
    })
  return rows


def markdown_table(columns: tuple[str, ...] | list[str], rows: list[dict]) -> str:
  """服务端渲染文字表，模型只负责原样复制（不让它自己拼表、自选列）。"""
  if not columns or not rows:
    return ''

  def cell(value) -> str:
    text = '' if value is None else str(value)
    text = ' '.join(text.split())
    return text.replace('|', r'\|') if text else '—'

  head = '| ' + ' | '.join(columns) + ' |'
  sep = '| ' + ' | '.join('---' for _ in columns) + ' |'
  body = ['| ' + ' | '.join(cell(row.get(c)) for c in columns) + ' |' for row in rows]
  return '\n'.join([head, sep] + body)


def _group_body(raw: str) -> list[str]:
  """单组 OCR 文本 → 场景 / 人员分配 / 演练过程（时间轴） / 流程图。"""
  info = analyze_record(raw)
  out: list[str] = []
  if info['scene']:
    out += [f'**演练场景**：{info["scene"]}', '']
  if info['roles']:
    out += [f'**人员分配**：{info["roles"]}', '']
  if info['steps']:
    out += ['**演练过程**', '']
    out += [f'{i}. {step["time"]} {step["text"]}'
            for i, step in enumerate(info['steps'], start=1)]
    out.append('')
    if info['flow']:
      out += [info['flow'], '']
    return out
  prose = normalize_ocr_text(raw)
  if prose:
    out += [prose, '']
  return out


def render_records_markdown(result: dict, texts: dict[str, str]) -> str:
  """各组 OCR 文本 → 演练过程（文字时间轴 + 流程图）Markdown，不出现 OCR 表格。"""
  plan = result.get('plan') or {}
  lines = ['# 演练记录（OCR 整理）', '']
  for label, value in (
    ('计划名称', plan.get('name')), ('专业', plan.get('speciality')),
    ('部门', plan.get('dept')), ('周次', plan.get('week')),
    ('起止日期', plan.get('dates')),
  ):
    if value:
      lines.append(f'- {label}：{value}')
  lines += ['', '> 由扫描件 OCR 识别整理为文字与流程图，可能有识别错漏；仅供比对查看。',
            '']

  rows = result.get('groups') or []
  evaluated = [r for r in rows if str(r.get('summary') or '').strip()]
  if evaluated:
    lines += ['## 各组综合评价', '']
    for row in evaluated:
      lines.append(f'- {row.get("letter")}班组：{str(row.get("summary")).strip()}')
    lines.append('')

  for row in rows:
    letter = str(row.get('letter') or '')
    fname = str(row.get('file_name') or '')
    head = f'## {letter}班组演练记录'
    lines += [head + (f'（{fname}）' if fname else ''), '']
    text = str(texts.get(letter) or '').strip()
    if text:
      lines += _group_body(text)
      continue
    reason = str(row.get('error') or '').strip() or _STATUS_LABELS.get(
      str(row.get('status') or ''), '没有可查看的记录文本')
    lines += [f'（{reason}）', '']
  return '\n'.join(lines).rstrip() + '\n'


def build_flows(texts: dict[str, str]) -> dict[str, str]:
  """各组 OCR 文本 → `{班组字母: mermaid 文本}`；空文本 / 不足 2 步（无图）不收。"""
  flows: dict[str, str] = {}
  for letter, raw in (texts or {}).items():
    text = str(raw or '').strip()
    if not text:
      continue
    flow = analyze_record(text).get('flow') or ''
    if flow:
      flows[str(letter)] = flow
  return flows


def attach_flows(result: dict, flows: dict[str, str]) -> None:
  """按 `groups[].letter` 把 flow 写进该组条目；没图的组不加这个键（不给空串）。"""
  if not flows:
    return
  for row in result.get('groups') or []:
    if not isinstance(row, dict):
      continue
    flow = flows.get(str(row.get('letter') or ''))
    if flow:
      row['flow'] = flow


def _records_url(thread_id: str, dataset_id: str) -> str:
  return (f'/api/v1/chat/agent/artifacts/{quote(thread_id, safe="")}/'
          f'{quote(dataset_id, safe="")}/records')


def _write_text(path, content: str) -> None:
  path.parent.mkdir(parents=True, exist_ok=True)
  path.write_text(content, encoding='utf-8')


async def _issue_attachment(result: dict, thread_id: str) -> dict:
  rows = build_issue_rows(result)
  if not rows:
    return {}
  info = await asyncio.to_thread(
    save_dataset, thread_id, 'drill_audit',
    {'week_id': result.get('week_id') or ''}, rows, 'drill-issues',
  )
  url = _records_url(thread_id, info['dataset_id'])
  item = {
    'rows': len(rows), 'records_url': url,
    'file_tag': f'<file url="{url}" name="演练问题明细"/>',
  }
  if len(rows) <= _INLINE_MAX_ROWS:
    item['table_markdown'] = markdown_table(list(ISSUE_COLUMNS), rows)
  return item


async def _records_attachment(result: dict, thread_id: str,
                              texts: dict[str, str]) -> dict:
  if not result.get('groups'):
    return {}
  content = render_records_markdown(result, texts)
  filename = f'drill-records-{uuid.uuid4().hex[:8]}.md'
  path = thread_dir(thread_id) / _WORKSPACE_ID / filename
  await asyncio.to_thread(_write_text, path, content)
  url = resolve_artifact_url(thread_id, filename)
  if not url:
    return {}
  return {
    'file_name': filename, 'url': url,
    'file_tag': f'<file url="{url}" name="演练过程记录"/>',
  }


async def build_attachments(result: dict, thread_id: str) -> dict:
  """→ 附件字段（能生成几项给几项）；每段独立兜底，绝不抛给调用方。"""
  texts: dict[str, str] = {}
  for row in result.get('groups') or []:
    file_id = str(row.get('file_id') or '')
    if not file_id:
      continue
    try:
      text = await store.load_text(file_id)
    except Exception as e:  # noqa: BLE001 - 附件只是附加价值
      logger.warning('[DrillAudit] 读取班组记录文本失败 %s: %s',
                     row.get('letter'), str(e)[:160])
      continue
    if text:
      texts[str(row.get('letter') or '')] = text

  # 流程图同时随 groups[].flow 下发，模型原样贴在回答里（前端本地渲染）
  try:
    attach_flows(result, build_flows(texts))
  except Exception as e:  # noqa: BLE001 - 图拿不到不影响结论与附件
    logger.warning('[DrillAudit] 生成班组流程图失败: %s', str(e)[:200])

  out: dict[str, dict] = {}
  for key, builder in (
    ('issues', lambda: _issue_attachment(result, thread_id)),
    ('records', lambda: _records_attachment(result, thread_id, texts)),
  ):
    try:
      item = await builder()
    except Exception as e:  # noqa: BLE001 - 单项失败不影响其它附件
      logger.warning('[DrillAudit] 生成附件 %s 失败: %s', key, str(e)[:200])
      continue
    if item:
      out[key] = item
  return out

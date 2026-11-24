"""单周详情（页面用）：周记录 + 逐组演练过程重算。

流程图与步骤**不落 Mongo**，页面读时从内容库的 OCR 文本重算
（`store.load_text` 命中即零下载零 OCR，`process.analyze_record` 是纯函数）。
文本已被容量淘汰时该组 `steps_available=False`，页面回退只展示已存的评价与问题。
"""

import logging

from . import store
from .process import analyze_record

logger = logging.getLogger(__name__)


def strip_fence(flow: str) -> str:
  """去掉 `process` 产出的 ```mermaid 围栏，前端 `MermaidRenderer` 吃裸代码。"""
  text = (flow or '').strip()
  if not text.startswith('```'):
    return text
  lines = text.splitlines()
  if lines and lines[0].startswith('```'):
    lines = lines[1:]
  if lines and lines[-1].strip() == '```':
    lines = lines[:-1]
  return '\n'.join(lines).strip()


async def _group_detail(row: dict) -> dict:
  """单组：原记录字段 + `scene/roles/steps/lanes/flow/steps_available`。"""
  out = dict(row)
  out['steps_available'] = False
  out['scene'] = ''
  out['roles'] = ''
  out['steps'] = []
  out['lanes'] = []
  out['flow'] = ''
  out['raw_text'] = ''
  out['dimensions'] = out.get('dimensions') or []
  out['missing_steps'] = out.get('missing_steps') or []
  out['step_findings'] = out.get('step_findings') or []
  out['timeline_stats'] = out.get('timeline_stats') or {}
  file_id = str(row.get('file_id') or '')
  if not file_id:
    return out
  try:
    text = await store.load_text(file_id)
  except Exception as e:  # noqa: BLE001 - 详情只是展示，失败按不可用
    logger.warning('[DrillAudit] 详情读取班组文本失败 %s: %s',
                   row.get('letter'), str(e)[:160])
    return out
  if not text:
    return out
  # 原始解析文本随详情下发供页面查阅（仅此 REST 端点，不进模型上下文）
  out['raw_text'] = text
  try:
    info = analyze_record(text)
  except Exception as e:  # noqa: BLE001
    logger.warning('[DrillAudit] 详情解析班组文本失败 %s: %s',
                   row.get('letter'), str(e)[:160])
    return out
  out['steps_available'] = True
  out['scene'] = info.get('scene') or ''
  out['roles'] = info.get('roles') or ''
  out['steps'] = info.get('steps') or []
  out['lanes'] = info.get('lanes') or []
  out['flow'] = strip_fence(info.get('flow') or '')
  # 时间轴汇总按重算值覆盖；步数与存档不一致（refresh 后旧判定）则清空步骤标注，防标错步
  fresh_stats = info.get('timeline_stats') or {}
  out['timeline_stats'] = fresh_stats
  stored = (row.get('timeline_stats') or {}).get('count')
  if stored is not None and stored != fresh_stats.get('count'):
    out['step_findings'] = []
  return out


async def build_week_detail(week_id: str) -> dict | None:
  """→ `{week, groups, issues, summary, note}`；周记录不存在返回 None。"""
  rec = await store.get_week(week_id)
  if not rec:
    return None
  week = {k: v for k, v in rec.items()
          if k not in ('groups', 'issues', 'summary', 'note', '_id')}
  week['week_id'] = str(rec.get('_id') or week_id)
  groups = [await _group_detail(row)
            for row in (rec.get('groups') or []) if isinstance(row, dict)]
  return {
    'week': week,
    'groups': groups,
    'issues': rec.get('issues') or [],
    'summary': rec.get('summary') or '',
    'note': rec.get('note') or '',
  }

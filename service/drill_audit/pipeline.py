"""演练审查主流水线。

定位演练周 → 并发下载 4 份班组 PDF → OCR → 检索 EOP（每周一次共享）
→ **每个班组独立一次 LLM 评价** → 周级按组确定性拼接统计与综合评价 → 结构化结果。
不判「合规 / 不合规」档位：只给问题清单、按严重程度的统计与各组综合评价。
原始 OCR 文本不进返回体（只回评价与证据摘录），但会落本地内容库（见 store.py）；
命中内容库则零下载零 OCR，`refresh=True` 绕过读取重下重解析。
"""

import asyncio
import logging
from datetime import datetime
from typing import Awaitable, Callable

from config.settings import settings

from . import fetch, judge, parse, retrieve, store
from .codes import (
  FINISH_STATUS, group_file, normalize_groups, speciality_hint, speciality_name,
)

logger = logging.getLogger(__name__)

Progress = Callable[[str], Awaitable[None]] | None


async def _emit(progress: Progress, message: str) -> None:
  if not progress:
    return
  try:
    await progress(message)
  except Exception:  # noqa: BLE001 - 进度只是反馈
    pass


def _pick_week(weeks: list[dict], week: int) -> dict:
  """按周号选周；`week=0` 时优先最近一个已安排且有记录的周。"""
  scheduled = [w for w in weeks if w.get('status') == 0 and w.get('id')]
  if week:
    for w in scheduled:
      if str(w.get('week')) == str(week):
        return w
    raise ValueError(f'计划里没有第 {week} 周的已安排记录')
  if not scheduled:
    raise ValueError('该计划没有任何已安排的演练周')
  # 优先已完成/已逾期且有记录的周（未来的已安排周 fileStatus 也会预填，但没有真实文件）
  done = [w for w in scheduled
          if str(w.get('finishStatus') or '') in ('3', '4')
          and str(w.get('fileStatus') or '').strip()]
  with_files = [w for w in scheduled if str(w.get('fileStatus') or '').strip()]
  pool = done or with_files or scheduled
  return max(pool, key=lambda w: _week_no(w))


def _week_no(week: dict) -> int:
  try:
    return int(week.get('week'))
  except (TypeError, ValueError):
    return 0


def _eop_fingerprint(refs: list[dict]) -> str:
  """结论基于哪批 EOP：命中文件名排序拼接（只用于展示，不作跳过重算的条件）。"""
  names = sorted(str(r.get('file_name') or '') for r in refs if r.get('file_name'))
  return '|'.join(names)[:500]


SEVERITY_LEVELS = ('高', '中', '低')


def severity_counts(issues) -> dict[str, int]:
  """按严重程度计数（高 / 中 / 低）；未标注或无法识别的严重程度不计入。"""
  counts = {level: 0 for level in SEVERITY_LEVELS}
  for item in issues or []:
    level = str((item or {}).get('severity') or '').strip()
    if level in counts:
      counts[level] += 1
  return counts


def _severity_text(counts: dict) -> str:
  return '、'.join(f'{level} {counts[level]}' for level in SEVERITY_LEVELS
                   if counts.get(level))


def _group_rows(found: list[tuple], drill_texts: dict[str, str],
                group_results: dict[str, dict]) -> list[dict]:
  """每班组一行：文件状态 + 评价结果。缺文件 / OCR 失败 / 无 EOP 也各占一行。"""
  rows: list[dict] = []
  for letter, fid, fname in found:
    text = drill_texts.get(letter)
    res = group_results.get(letter) or {}
    issues = res.get('issues') or []
    if not fid:
      status, default_err = 'missing', '该周没有记录文件'
    elif not text:
      status, default_err = 'ocr_failed', 'OCR 未产出有效文本'
    else:
      status = str(res.get('status') or ('judge_failed' if res.get('error') else 'ok'))
      default_err = ''
    rows.append({
      'letter': letter, 'file_id': fid, 'file_name': fname,
      'status': status, 'chars': len(text or ''),
      'issue_count': len(issues),
      'severity_counts': severity_counts(issues),
      'summary': res.get('summary') or '',
      'error': res.get('error') or default_err,
      'dimensions': res.get('dimensions') or [],
      'missing_steps': res.get('missing_steps') or [],
      'step_findings': res.get('step_findings') or [],
      'timeline_stats': res.get('timeline_stats') or {},
    })
  return rows


def week_summary(group_rows: list[dict]) -> str:
  """确定性拼接各组统计与评价（零额外 LLM 调用），如
  「全周共 2 项问题（高 1、中 1）：A班组未发现与 EOP 不符的环节；B班组 2 项问题（高 1、中 1）；
  C班组无法评价（该周没有记录文件）」。"""
  total = sum(int(row.get('issue_count') or 0) for row in group_rows)
  counts = {level: 0 for level in SEVERITY_LEVELS}
  for row in group_rows:
    for level, n in (row.get('severity_counts') or {}).items():
      if level in counts:
        counts[level] += int(n or 0)
  remaining = [row for row in group_rows if row.get('status') != 'ok']
  if total:
    detail = _severity_text(counts)
    head = f'全周共 {total} 项问题（{detail}）' if detail else f'全周共 {total} 项问题'
  elif remaining:
    head = '已评价的班组未发现与 EOP 不符的环节'
  else:
    head = '全周未发现与 EOP 不符的环节'

  parts: list[str] = []
  for row in group_rows:
    letter = str(row.get('letter') or '')
    count = int(row.get('issue_count') or 0)
    if row.get('status') == 'ok':
      if count:
        detail = _severity_text(row.get('severity_counts') or {})
        label = f'{letter}班组 {count} 项问题'
        parts.append(f'{label}（{detail}）' if detail else label)
      else:
        parts.append(f'{letter}班组未发现与 EOP 不符的环节')
      continue
    reason = str(row.get('error') or row.get('summary') or '').strip()
    prefix = f'{letter}班组'
    if reason.startswith(prefix):
      reason = reason[len(prefix):].lstrip('：: ')
    parts.append(f'{letter}班组无法评价' + (f'（{reason}）' if reason else ''))
  if not parts:
    return head
  return f'{head}：' + '；'.join(parts)


async def _resolve_plan_week(
  plan_name: str, week_id: str, dept_id: str, year: int, week: int,
  user_id: str | None,
) -> tuple[dict, dict, dict]:
  """→ `(plan, week_in_list, detail)`；直接给 week_id 时 plan/week_in_list 可能为空壳。"""
  if week_id:
    detail = await fetch.week_detail(week_id, user_id)
    plan: dict = {}
    if dept_id:
      try:
        for p in await fetch.list_plans(dept_id, year, user_id):
          if str(p.get('id')) == str(detail.get('drillPlanId')):
            plan = p
            break
      except Exception as e:  # noqa: BLE001 - 仅用于补专业信息
        logger.info('[DrillAudit] 补计划信息失败，忽略: %s', str(e)[:120])
    return plan, {}, detail

  if not dept_id:
    raise ValueError('请提供部门（dept_id）或演练周 id（week_id）之一')

  plans = await fetch.list_plans(dept_id, year, user_id)
  keyword = (plan_name or '').strip()
  if not keyword:
    names = '、'.join(str(p.get('name') or '') for p in plans[:15])
    raise ValueError(f'请给出演练计划名称；该部门 {year} 年的计划有：{names}')
  matches = [p for p in plans if keyword in str(p.get('name') or '')]
  if not matches:
    names = '、'.join(str(p.get('name') or '') for p in plans[:15])
    raise ValueError(f'没有名称包含「{keyword}」的演练计划；该部门 {year} 年的计划有：{names}')
  if len(matches) > 1:
    names = '、'.join(str(p.get('name') or '') for p in matches)
    raise ValueError(f'「{keyword}」匹配到多项计划，请更精确：{names}')

  plan = matches[0]
  chosen = _pick_week(plan.get('weeks') or [], week)
  detail = await fetch.week_detail(str(chosen['id']), user_id)
  return plan, chosen, detail


async def _fetch_and_parse(
  letter: str, file_id: str, dept_id: str, user_id: str | None,
  sem: asyncio.Semaphore, *, refresh: bool, stats: dict,
  plan_id: str = '', week_key: str = '',
) -> tuple[str, str, str]:
  """→ `(letter, text, error)`；命中内容库时零下载零 OCR。"""
  async with sem:
    if not refresh:
      cached = await store.load_text(file_id)
      if cached:
        stats['files_hit'] += 1
        return letter, cached, ''
    try:
      data = await fetch.download_pdf(file_id, dept_id=dept_id or None, user_id=user_id)
      text = await parse.pdf_bytes_to_text(data)
    except Exception as e:  # noqa: BLE001 - 单份失败不拖垮整轮
      logger.warning('[DrillAudit] %s班组 PDF 处理失败: %s', letter, str(e)[:200])
      await store.mark_ocr_failed(
        file_id, error=f'{type(e).__name__}: {e}', dept_id=dept_id,
        plan_id=plan_id, week_id=week_key, letter=letter,
      )
      return letter, '', f'{letter}班组记录解析失败（{type(e).__name__}）'
  if not text:
    await store.mark_ocr_failed(
      file_id, error='ocr_empty', dept_id=dept_id,
      plan_id=plan_id, week_id=week_key, letter=letter,
    )
    return letter, '', f'{letter}班组记录 OCR 未产出有效文本'
  stats['files_missed'] += 1
  await store.save_file(
    file_id, pdf=data, text=text, dept_id=dept_id,
    plan_id=plan_id, week_id=week_key, letter=letter,
  )
  return letter, text, ''


async def _judge_groups(
  plan_meta: dict, drill_texts: dict[str, str], eop_docs: list[dict],
  progress: Progress = None,
) -> dict[str, dict]:
  """每个有文本的班组独立判定一次；单组异常只影响该组。"""
  sem = asyncio.Semaphore(max(1, settings.DRILL_AUDIT_JUDGE_CONCURRENCY))

  async def one(letter: str) -> tuple[str, dict]:
    async with sem:
      await _emit(progress, f'正在判定 {letter}班组演练记录')
      try:
        res = await judge.audit_group(plan_meta, letter, drill_texts[letter], eop_docs)
      except Exception as e:  # noqa: BLE001 - 单组失败不拖垮整轮
        logger.warning('[DrillAudit] %s班组判定异常: %s', letter, str(e)[:200])
        res = {'issues': [], 'summary': '', 'error': f'判定失败：{type(e).__name__}'}
    out = dict(res)
    out['letter'] = letter
    return letter, out

  pairs = await asyncio.gather(*(one(letter) for letter in sorted(drill_texts)))
  return dict(pairs)


async def run_audit(
  plan_name: str = '',
  week_id: str = '',
  dept_id: str = '',
  year: int | str = 0,
  week: int = 0,
  groups: str = '',
  user_id: str | None = None,
  progress: Progress = None,
  refresh: bool = False,
  audit: bool = True,
  store_max_mb: int | None = None,
  audit_source: str = 'dialog',
  run_id: str = '',
  period_key: str = '',
  period_label: str = '',
) -> dict:
  """执行一次合规审查，返回结构化结果（见模块 docstring / 设计文档）。

  Args:
    refresh: True 时绕过内容库读取，重下重解析（怀疑上游记录已更新时用）。
    audit: False 时只采集文本、不检索 EOP 也不判定（周任务批量采集用）。
    store_max_mb: 临时覆盖容量上限（便于本机验证淘汰）。
    audit_source: 落库来源标记（`dialog` / `weekly`），默认对话链路。
    run_id / period_key / period_label: 周任务透传的运行信息，非空才写入周记录。
  """
  year_val = int(year) if str(year).strip().isdigit() else datetime.now().year
  await _emit(progress, '正在定位演练计划与周次')

  plan, week_row, detail = await _resolve_plan_week(
    plan_name, week_id, dept_id, year_val, int(week or 0), user_id,
  )

  name = str(detail.get('drillPlanName') or plan.get('name') or '')
  speciality = speciality_name(str(plan.get('drillSpecialityId') or ''))
  plan_meta = {
    'name': name,
    'speciality': speciality,
    'dept': str(plan.get('deptName') or ''),
    'year': year_val,
    'week': str(detail.get('week') or ''),
    'dates': f"{detail.get('stime') or ''} ~ {detail.get('etime') or ''}".strip(' ~'),
    'finish_status': FINISH_STATUS.get(str(detail.get('finishStatus') or ''), ''),
    'percent': str(detail.get('percent') or ''),
  }

  plan_id = str(plan.get('id') or detail.get('drillPlanId') or '')
  week_key = str(week_id or detail.get('id') or week_row.get('id') or '')

  letters = normalize_groups(groups)
  found = [(g, *group_file(detail, g)) for g in letters]
  jobs = [(g, fid) for g, fid, _ in found if fid]
  skipped = [g for g, fid, _ in found if not fid]

  notes: list[str] = []
  cache_stats = {'files_hit': 0, 'files_missed': 0}
  if skipped:
    notes.append(f'{"/".join(skipped)}班组该周没有记录文件')

  async def _finish(result: dict, group_rows: list[dict]) -> dict:
    result['week_id'] = week_key
    result['cache'] = dict(cache_stats)
    result['groups'] = group_rows
    if audit:
      record = {
        '_id': week_key,
        'week_id': week_key,
        'plan_id': plan_id,
        'plan_name': name,
        'speciality': speciality,
        'dept_id': dept_id,
        'dept_name': plan_meta['dept'],
        'year': year_val,
        'week_no': plan_meta['week'],
        'stime': str(detail.get('stime') or ''),
        'etime': str(detail.get('etime') or ''),
        'finish_status': plan_meta['finish_status'],
        'groups': group_rows,
        'issues': result.get('issues') or [],
        'summary': result.get('summary') or '',
        'note': result.get('note') or '',
        'eop_refs': result.get('eop_refs') or [],
        'eop_fingerprint': _eop_fingerprint(result.get('eop_refs') or []),
        'cache': dict(cache_stats),
        'audit_source': audit_source or 'dialog',
      }
      # 周任务额外的运行信息只在非空时写入，对话链路周记录形态不变
      for key, value in (('run_id', run_id), ('period_key', period_key),
                         ('period_label', period_label)):
        if value:
          record[key] = value
      await store.save_week(record)
    await store.evict_if_needed(store_max_mb)
    return result

  if not jobs:
    return await _finish({
      'plan': plan_meta, 'groups_audited': [], 'eop_refs': [],
      'issues': [],
      'summary': '所选演练周没有任何班组记录文件，无法评价。',
      'note': '；'.join(notes),
    }, _group_rows(found, {}, {}))

  sem = asyncio.Semaphore(max(1, settings.DRILL_AUDIT_PARSE_CONCURRENCY))
  await _emit(progress, f'正在下载并 OCR {len(jobs)} 份班组记录（约需数十秒/份）')
  results = await asyncio.gather(
    *(_fetch_and_parse(
      g, fid, dept_id, user_id, sem,
      refresh=refresh, stats=cache_stats, plan_id=plan_id, week_key=week_key,
    ) for g, fid in jobs)
  )
  drill_texts: dict[str, str] = {}
  group_results: dict[str, dict] = {}
  for letter, text, err in results:
    if err:
      notes.append(err)
      group_results[letter] = {'issues': [], 'summary': '', 'error': err}
    else:
      drill_texts[letter] = text

  if cache_stats['files_hit']:
    notes.append(f'本次复用已解析记录 {cache_stats["files_hit"]} 份')

  groups_audited = sorted(drill_texts)
  if not drill_texts:
    return await _finish({
      'plan': plan_meta, 'groups_audited': [], 'eop_refs': [],
      'issues': [],
      'summary': '所有班组记录都无法解析出文本，无法评价。',
      'note': '；'.join(notes),
    }, _group_rows(found, drill_texts, group_results))

  if not audit:
    notes.append('本次仅采集记录文本，未执行评价')
    return await _finish({
      'plan': plan_meta, 'groups_audited': groups_audited, 'eop_refs': [],
      'issues': [], 'summary': '',
      'note': '；'.join(notes),
    }, _group_rows(found, drill_texts, group_results))

  query = speciality_hint(name)
  await _emit(progress, '正在检索对应的 EOP 文档')
  eop_refs: list[dict] = []
  try:
    eop_docs = await retrieve.find_eop(query)
  except retrieve.EopRetrievalError as e:
    logger.warning('[DrillAudit] EOP 检索不可用: %s', str(e)[:200])
    notes.append('知识库检索不可用（Milvus 连接失败），本次未能评价')
    for letter in drill_texts:
      group_results[letter] = {'status': 'no_eop', 'issues': [], 'summary': '',
                               'error': '知识库检索不可用（Milvus 连接失败）'}
  else:
    eop_refs = [{'file_name': d.get('file_name') or '', 'score': d.get('score')}
                for d in eop_docs]
    if not eop_docs:
      notes.append('知识库无相关 EOP 文档，未能评价')
      for letter in drill_texts:
        group_results[letter] = {'status': 'no_eop', 'issues': [], 'summary': '',
                                 'error': '知识库无相关 EOP 文档'}
    else:
      judged = await _judge_groups(plan_meta, drill_texts, eop_docs, progress)
      for letter, res in judged.items():
        group_results[letter] = res
        if res.get('error'):
          notes.append(f'{letter}班组判定失败：{res["error"]}')
  notes.append('演练记录为扫描件 OCR 文本，仅用于判定')

  rows = _group_rows(found, drill_texts, group_results)
  issues: list[dict] = []
  for letter in sorted(drill_texts):
    issues.extend((group_results.get(letter) or {}).get('issues') or [])
  return await _finish({
    'plan': plan_meta,
    'groups_audited': groups_audited,
    'eop_refs': eop_refs,
    'issues': issues,
    'summary': week_summary(rows),
    'note': '；'.join(notes),
  }, rows)

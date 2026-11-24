"""演练审查的每周任务：扫描「到点该审的演练周」→ 逐周走一次审查流水线 → 落结论。

与对话链路共用 `pipeline.run_audit`，只是多传几个落库字段（`audit_source='weekly'`
与期号）；**不产附件**，结论只落 Mongo `drill_audit_weeks`，页面直接读库展示。

「该审哪些周」口径（纯确定性）：
已安排（`status==0`）且 `fileStatus` 非空，且 `etime` 落在 `[今天−回溯天数, 今天]`。
幂等：该周记录 `audited_at`（转 Asia/Shanghai）的日期 **≥** 该周 `etime` 当天、
且没有 `ocr_failed`/`missing` 的组 → 跳过（内容库命中本身已零下载零 OCR，跳过只为省掉重复判定）。

已知盲区：服务账号跨公司可见范围未知，某些公司可能拿不到计划 —— 只记 warning，
不当作失败（用 `cli.py --weekly-dry-run` 暴露）。
"""

import logging
import time
import uuid
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from config.settings import settings
from models.scheduled_task import RunStatus
from service.scheduled_task import repository as repo

from . import fetch, pipeline, store
from .codes import speciality_name

logger = logging.getLogger(__name__)


def _tz() -> ZoneInfo:
  try:
    return ZoneInfo(settings.SCHEDULED_TASK_TIMEZONE or 'Asia/Shanghai')
  except Exception:  # noqa: BLE001 - 缺 tzdata 时回退固定 UTC+8
    return ZoneInfo('Asia/Shanghai')


def _today() -> date:
  return datetime.now(_tz()).date()


# ==================== 纯函数（可单测，无 IO） ====================


def parse_day(value) -> date | None:
  """`2026-04-09` / `2026-04-09 10:51:00` / 秒或毫秒时间戳字符串 → `date`。"""
  if value is None:
    return None
  s = str(value).strip()
  if not s:
    return None
  if s.isdigit():
    try:
      n = int(s)
    except ValueError:
      return None
    if len(s) >= 13:  # 毫秒
      n = n / 1000.0
    try:
      return datetime.fromtimestamp(n, tz=_tz()).date()
    except (OverflowError, OSError, ValueError):
      return None
  try:
    return datetime.fromisoformat(s.replace('Z', '+00:00')).date()
  except ValueError:
    pass
  for fmt in ('%Y/%m/%d', '%Y/%m/%d %H:%M:%S'):
    try:
      return datetime.strptime(s, fmt).date()
    except ValueError:
      continue
  return None


def is_due(week_meta: dict, today: date, lookback_days: int) -> bool:
  """已安排 + 有记录文件 + `etime` 落在 `[today−N, today]`。"""
  meta = week_meta if isinstance(week_meta, dict) else {}
  if str(meta.get('status')) != '0':
    return False
  if not str(meta.get('fileStatus') or '').strip():
    return False
  day = parse_day(meta.get('etime'))
  if day is None:
    return False
  return today - timedelta(days=max(0, int(lookback_days))) <= day <= today


def period_of(day: date) -> tuple[str, str]:
  """→ `('2026-W46', '2026 第 46 周')`（ISO 周，与报告期号同口径）。"""
  iso = day.isocalendar()
  return f'{iso.year}-W{iso.week:02d}', f'{iso.year} 第 {iso.week} 周'


def years_to_scan(today: date) -> list[int]:
  """当年；1 月上旬补上一年（跨年的演练周仍在上一年计划里）。"""
  years = [today.year]
  if today.month == 1 and today.day <= 14:
    years.append(today.year - 1)
  return years


def week_key(year, week_no) -> str:
  """`(2026, '45')` → `'2026-W45'`（与前端矩阵列 key 同口径）；脏输入返回 `''`。"""
  no = str(week_no or '').strip()
  if not no:
    return ''
  try:
    y = int(year)
  except (TypeError, ValueError):
    return ''
  if y <= 0:
    return ''
  try:
    n = int(no)
  except ValueError:
    n = 0
  return f'{y}-W{n if n > 0 else no}'


def plan_roster(plan: dict, dept: dict, year: int) -> dict | None:
  """计划 → 看板矩阵的行（只留已排期 `status==0` 且周号有效的周）。

  无 `plan_id` 返回 None；`weeks` 每项
  `{week_id, week_no, key, stime, etime, finish_status, file_status}`，
  未到期的未来周也算排期（列要显示出来）；执行状态原样来自上游
  （`finishStatus` 见 `codes.FINISH_STATUS`，`fileStatus` 是已上传记录的班组合集）。
  """
  plan = plan if isinstance(plan, dict) else {}
  plan_id = str(plan.get('id') or '')
  if not plan_id:
    return None
  dept = dept if isinstance(dept, dict) else {}
  weeks: list[dict] = []
  for week in plan.get('weeks') or []:
    week = week if isinstance(week, dict) else {}
    if str(week.get('status')) != '0':
      continue
    week_id = str(week.get('id') or '')
    key = week_key(year, week.get('week'))
    if not week_id or not key:
      continue
    weeks.append({
      'week_id': week_id,
      'week_no': str(week.get('week') or ''),
      'key': key,
      'stime': str(week.get('stime') or ''),
      'etime': str(week.get('etime') or ''),
      'finish_status': str(week.get('finishStatus') or ''),
      'file_status': str(week.get('fileStatus') or ''),
    })
  return {
    'dept_id': str(dept.get('dept_id') or ''),
    'dept_name': str(dept.get('dept_name') or ''),
    'company_name': str(dept.get('company_name') or ''),
    'plan_id': plan_id,
    'plan_name': str(plan.get('name') or ''),
    'speciality': speciality_name(str(plan.get('drillSpecialityId') or '')),
    'year': int(year),
    'weeks': weeks,
  }


def week_candidates(plans: list[dict], today: date, lookback_days: int) -> list[dict]:
  """摊平各计划的 `weeks[]` → 过滤 + 按 `week_id` 去重 + 按 `etime` 升序。"""
  out: list[dict] = []
  seen: set[str] = set()
  for plan in plans or []:
    plan = plan if isinstance(plan, dict) else {}
    for week in plan.get('weeks') or []:
      if not is_due(week, today, lookback_days):
        continue
      week_id = str(week.get('id') or '')
      if not week_id or week_id in seen:
        continue
      seen.add(week_id)
      out.append({
        'plan_id': str(plan.get('id') or ''),
        'plan_name': str(plan.get('name') or ''),
        'dept_id': str(plan.get('dept_id') or ''),
        'dept_name': str(plan.get('dept_name') or ''),
        'year': int(plan.get('year') or 0),   # 传给 run_audit，周记录年份与计划一致
        'speciality': speciality_name(str(plan.get('drillSpecialityId') or '')),
        'week_id': week_id,
        'week': str(week.get('week') or ''),
        'stime': str(week.get('stime') or ''),
        'etime': str(week.get('etime') or ''),
      })
  out.sort(key=lambda c: (parse_day(c['etime']) or date.min, c['week_id']))
  return out


def summarize(records: list[dict]) -> str:
  """周级统计 + 每组一句话，纯确定性拼接。"""
  records = [r for r in (records or []) if isinstance(r, dict)]
  total = len(records)
  issues = sum(len(r.get('issues') or []) for r in records)
  head = (f'本轮审查 {total} 个演练周，共 {issues} 项问题'
          if total else '本轮没有需要审查的演练周')
  lines = [head]
  for r in records:
    name = str(r.get('plan_name') or '').strip()
    dept = str(r.get('dept_name') or '').strip()
    week = str(r.get('week_no') or r.get('week') or '').strip()
    label = ' · '.join(x for x in (dept, name, f'第 {week} 周' if week else '') if x)
    summary = str(r.get('summary') or '').strip()
    lines.append(f'- {label}：{summary}' if label and summary else f'- {summary or label}')
  return '\n'.join(lines)


# ==================== 部门解析 ====================


async def resolve_depts(names: list[str] | None = None) -> list[dict]:
  """取「公司 × 部门」清单（含 `dept_id`）。

  `names` 显式指定部门名；否则读 `DRILL_AUDIT_WEEKLY_DEPTS`（空 = 复用 `DOC_UPDATE_DEPTS`）。
  复用 doc_update 的注册表缓存（`ensure_registry`），不额外调用接口。
  """
  from service.doc_update import depts as doc_depts

  if names:
    return await doc_depts.resolve_departments(names=list(names))

  raw = (settings.DRILL_AUDIT_WEEKLY_DEPTS or '').strip()
  if not raw:
    return await doc_depts.resolve_departments()

  dept_names: list[str] = []
  companies: list[str] = []
  for part in raw.split(','):
    part = part.strip()
    if not part:
      continue
    if ':' in part:
      company, dept = part.split(':', 1)
      if company.strip():
        companies.append(company.strip())
      if dept.strip():
        dept_names.append(dept.strip())
    else:
      dept_names.append(part)
  return await doc_depts.resolve_departments(
    names=dept_names or None, companies=companies or None,
  )


# ==================== 编排 ====================


def _audit_day(value) -> date | None:
  """`audited_at`（UTC datetime）→ Asia/Shanghai 的日期。"""
  if isinstance(value, datetime):
    dt = value if value.tzinfo else value.replace(tzinfo=ZoneInfo('UTC'))
    return dt.astimezone(_tz()).date()
  return parse_day(value)


async def _already_audited(week_id: str) -> bool:
  rec = await store.get_week(week_id)
  if not rec:
    return False
  etime_day = parse_day(rec.get('etime'))
  audit_day = _audit_day(rec.get('audited_at'))
  if not etime_day or not audit_day or audit_day < etime_day:
    return False
  for group in rec.get('groups') or []:
    if str((group or {}).get('status') or '') in ('ocr_failed', 'missing'):
      return False
  return True


async def _emit(progress, message: str) -> None:
  if not progress:
    return
  try:
    result = progress(message)
    if hasattr(result, '__await__'):
      await result
  except Exception:  # noqa: BLE001 - 进度只是反馈
    pass


async def _collect_plans(
  depts: list[dict], today: date, *, dry_run: bool, progress=None,
) -> tuple[list[tuple[dict, int, list[dict]]], list[str]]:
  """部门 × 年 → 拉演练计划 + 顺手落清单缓存，返回 `([(dept, year, plans)], errors)`。

  一次 `fetch.list_plans` 同时服务「落清单」与「找候选周」，不增加接口调用；
  单部门 / 单年失败只记 warning，不拖垮其余。
  """
  errors: list[str] = []
  collected: list[tuple[dict, int, list[dict]]] = []
  for dept in depts:
    dept_id = str(dept.get('dept_id') or '')
    dept_name = str(dept.get('dept_name') or '')
    if not dept_id:
      reason = str(dept.get('error') or '缺少 dept_id')
      errors.append(f'{dept.get("company_name") or ""}{dept_name}：{reason}'.strip())
      continue
    for year in years_to_scan(today):
      await _emit(progress, f'正在拉取 {dept_name} {year} 年演练计划')
      try:
        plans = await fetch.list_plans(dept_id, year)
      except Exception as e:  # noqa: BLE001 - 单部门/单年失败不拖垮整轮
        logger.warning('[DrillAudit] 拉取演练计划失败 dept=%s year=%s: %s',
                       dept_name or dept_id, year, str(e)[:200])
        errors.append(f'{dept_name} {year} 年计划拉取失败（{type(e).__name__}）')
        continue
      rows = list(plans or [])
      roster = [r for r in (plan_roster(p, dept, year) for p in rows) if r]
      if not dry_run:
        await store.save_plans(dept_id, year, roster)
      collected.append((dept, year, rows))
  return collected, errors


async def refresh_plan_roster(
  *, dept_names: list[str] | None = None, progress=None,
) -> dict:
  """只刷新看板用的演练计划清单（不下载、不 OCR、不判定）。"""
  started_at = datetime.now(_tz()).isoformat()
  depts = await resolve_depts(dept_names)
  collected, errors = await _collect_plans(
    depts, _today(), dry_run=False, progress=progress,
  )
  return {
    'depts': len(collected),
    'plans': sum(len(plans) for _, _, plans in collected),
    'errors': errors,
    'started_at': started_at,
    'finished_at': datetime.now(_tz()).isoformat(),
  }


async def run_weekly_audit(
  *, dept_names: list[str] | None = None, refresh: bool = False,
  progress=None, run_id: str = '', dry_run: bool = False,
) -> dict:
  """扫描并审查到点的演练周。`dry_run=True` 只列候选、不下载不 OCR 不判定。"""
  started_at = datetime.now(_tz()).isoformat()
  depts = await resolve_depts(dept_names)
  today = _today()
  lookback = int(settings.DRILL_AUDIT_WEEKLY_LOOKBACK_DAYS or 0)

  # 同一份计划清单顺手落库（零额外接口），供看板列出全部演练任务。
  collected, errors = await _collect_plans(depts, today, dry_run=dry_run)

  candidates: list[dict] = []
  seen: set[str] = set()
  for dept, year, plans in collected:
    dept_id = str(dept.get('dept_id') or '')
    dept_name = str(dept.get('dept_name') or '')
    for plan in plans:
      plan = {**plan, 'dept_id': dept_id, 'dept_name': dept_name, 'year': year}
      for cand in week_candidates([plan], today, lookback):
        if cand['week_id'] in seen:
          continue
        seen.add(cand['week_id'])
        candidates.append(cand)
  candidates.sort(key=lambda c: (parse_day(c['etime']) or date.min, c['week_id']))

  if dry_run:
    return {
      'weeks': len(candidates), 'audited': 0, 'skipped': 0, 'issues_total': 0,
      'candidates': candidates, 'errors': errors,
      'started_at': started_at, 'finished_at': datetime.now(_tz()).isoformat(),
    }

  audited: list[dict] = []
  skipped = 0
  for cand in candidates:
    if not refresh and await _already_audited(cand['week_id']):
      skipped += 1
      continue
    await _emit(progress, f'正在审查 {cand["dept_name"]} {cand["plan_name"]} 第 {cand["week"]} 周')
    day = parse_day(cand['etime']) or today
    period_key, period_label = period_of(day)
    try:
      # 带上计划年份：周记录 `year` 与看板列 key（`{year}-W{n}`）必须同口径
      year_arg = {'year': cand['year']} if cand.get('year') else {}
      result = await pipeline.run_audit(
        week_id=cand['week_id'], dept_id=cand['dept_id'], audit=True,
        audit_source='weekly', period_key=period_key, period_label=period_label,
        run_id=run_id, refresh=refresh, progress=progress, **year_arg,
      )
    except Exception as e:  # noqa: BLE001 - 单周失败不拖垮整轮
      logger.warning('[DrillAudit] 周审查失败 week_id=%s: %s',
                     cand['week_id'], str(e)[:200])
      errors.append(f'{cand["plan_name"]} 第 {cand["week"]} 周审查失败（{type(e).__name__}）')
      continue
    audited.append(result)

  issues_total = sum(len(r.get('issues') or []) for r in audited)
  return {
    'weeks': len(candidates), 'audited': len(audited), 'skipped': skipped,
    'issues_total': issues_total, 'records': audited, 'errors': errors,
    'summary': summarize(audited),
    'started_at': started_at, 'finished_at': datetime.now(_tz()).isoformat(),
  }


async def run_selected_weeks(
  items: list[dict], *, refresh: bool = False, progress=None, run_one=None,
) -> dict:
  """逐周审查页面上点选/勾选的演练周（单周也可）。

  `items=[{week_id, dept_id?, year?}]`；单周失败只记 `errors`、不中断其余周。
  不查幂等（用户点的就是这一周；已审周重跑命中内容库，很快）。
  """
  started_at = datetime.now(_tz()).isoformat()
  runner = run_one or pipeline.run_audit
  audited: list[dict] = []
  errors: list[str] = []
  seen: set[str] = set()
  for item in items or []:
    item = item if isinstance(item, dict) else {}
    week_id = str(item.get('week_id') or '')
    if not week_id or week_id in seen:
      continue
    seen.add(week_id)
    kwargs: dict = {}
    if str(item.get('dept_id') or ''):
      kwargs['dept_id'] = str(item.get('dept_id'))
    try:
      year = int(item.get('year') or 0)
    except (TypeError, ValueError):
      year = 0
    if year > 0:
      kwargs['year'] = year
    await _emit(progress, f'正在审查演练周 {week_id}')
    try:
      result = await runner(
        week_id=week_id, audit=True, audit_source='manual',
        refresh=refresh, progress=progress, **kwargs,
      )
    except Exception as e:  # noqa: BLE001 - 单周失败不拖垮其余周
      logger.warning('[DrillAudit] 选中周审查失败 week_id=%s: %s', week_id, str(e)[:200])
      errors.append(f'演练周 {week_id} 审查失败（{type(e).__name__}）')
      continue
    audited.append(result)
  return {
    'weeks': len(seen), 'audited': len(audited),
    'issues_total': sum(len(r.get('issues') or []) for r in audited),
    'records': audited, 'errors': errors, 'summary': summarize(audited),
    'started_at': started_at, 'finished_at': datetime.now(_tz()).isoformat(),
  }


# ==================== 定时任务入口 ====================


async def _notify_outcome(task, run, status, final_text: str, error: str) -> None:
  recipients = [task.created_by]
  batch_key = ''
  if status == RunStatus.FAILED and task.created_by == repo.SYSTEM_USER_ID:
    recipients = await repo.alert_user_ids() or [task.created_by]
    batch_key = repo.alert_batch_key()

  if batch_key:
    title = '演练审查周任务执行失败'
    body = f'「{task.name}」{(final_text or error).strip()[:120]}'
  else:
    title = (f"定时任务「{task.name}」执行"
             f"{'成功' if status == RunStatus.SUCCESS else '失败'}")
    body = (final_text or error).strip()[:80]

  for user_id in recipients:
    try:
      await repo.create_notification(
        user_id=user_id, task_id=task.task_id, task_name=task.name, run_id=run.run_id,
        status=status.value, title=title, body=body, batch_key=batch_key,
      )
    except Exception as e:  # noqa: BLE001 - 通知失败不影响运行记录
      logger.warning('[DrillAudit] 通知写入失败: %s', str(e)[:160])


async def run_weekly_task(task) -> None:
  """跑一轮每周审查。**不抛异常**（全部落库）。"""
  run_id = uuid.uuid4().hex
  thread_id = f'rz-agent-sched-{task.task_id}-{run_id}'
  period_key, period_label = period_of(_today())
  run = await repo.create_run(
    task.task_id, run_id=run_id, thread_id=thread_id, preset_key=task.template_id,
    period_key=period_key, period_label=period_label,
  )
  start_ts = time.time()
  error, final_text = '', ''
  counts: dict[str, int] | None = None

  try:
    result = await run_weekly_audit(run_id=run_id)
    counts = {
      'weeks': int(result.get('weeks') or 0),
      'audited': int(result.get('audited') or 0),
      'issues': int(result.get('issues_total') or 0),
    }
    final_text = result.get('summary') or ''
  except Exception as e:  # noqa: BLE001
    logger.exception('[DrillAudit] 每周审查任务失败 task_id=%s', task.task_id)
    error = f'{type(e).__name__}: {str(e)[:400]}'

  duration_ms = int((time.time() - start_ts) * 1000)
  status = RunStatus.FAILED if error else RunStatus.SUCCESS
  await repo.finish_run(run.run_id, status=status, final_reply=final_text,
                        artifact_urls=[], error=error, duration_ms=duration_ms,
                        thread_id=thread_id, check_counts=counts)
  await _notify_outcome(task, run, status, final_text, error)
  logger.info('[DrillAudit] 每周审查完成 task_id=%s status=%s duration_ms=%s',
              task.task_id, status.value, duration_ms)


# ==================== 页面数据 ====================


def _is_running() -> bool:
  try:
    from config.redis_conn import redis_manager
    return bool(redis_manager.client.get(redis_manager.key('drill_audit:weekly')))
  except Exception:  # noqa: BLE001 - Redis 抖动按「未在跑」处理
    return False


def _is_roster_running() -> bool:
  """清单刷新是否在跑（锁 key 与 `POST /drill-audit/roster` 一致）。"""
  try:
    from config.redis_conn import redis_manager
    return bool(redis_manager.client.get(redis_manager.key('drill_audit:roster')))
  except Exception:  # noqa: BLE001
    return False


_PROGRESS_KEY = 'drill_audit:progress'
_PROGRESS_TTL = 900


def set_progress(message: str) -> None:
  """写当前审查分步进度（页面轮询读）。Redis 抖动只当没有进度，绝不打断审查。"""
  try:
    from config.redis_conn import redis_manager
    redis_manager.client.set(redis_manager.key(_PROGRESS_KEY), str(message or ''),
                             ex=_PROGRESS_TTL)
  except Exception:  # noqa: BLE001
    pass


def clear_progress() -> None:
  try:
    from config.redis_conn import redis_manager
    redis_manager.client.delete(redis_manager.key(_PROGRESS_KEY))
  except Exception:  # noqa: BLE001
    pass


def _get_progress() -> str:
  try:
    from config.redis_conn import redis_manager
    return str(redis_manager.client.get(redis_manager.key(_PROGRESS_KEY)) or '')
  except Exception:  # noqa: BLE001
    return ''


async def _latest_run() -> dict:
  try:
    from config.mongodb_conn import mongodb_manager
    from .presets import KEY as PRESET_KEY
    doc = await mongodb_manager.db[repo.RUNS_COLLECTION].find_one(
      {'preset_key': PRESET_KEY}, sort=[('triggered_at', -1)],
    )
  except Exception:  # noqa: BLE001
    return {}
  if not doc:
    return {}
  return {
    'status': str(doc.get('status') or ''),
    'triggered_at': str(doc.get('triggered_at') or ''),
    'final_reply': str(doc.get('final_reply') or '')[:300],
    'error': str(doc.get('error') or '')[:300],
    'check_counts': doc.get('check_counts') or {},
  }


async def _plan_matrix() -> tuple[list[dict], list[dict]]:
  """计划清单缓存 → `(plan_rows, plan_columns)`；同部门计划跨年合并成一行。

  行带每周 `{key, week_id, finish_status, file_status}`（前端按状态展示 + 单周执行），
  列元信息（周号/起止）单独发一份，控制 3s 轮询载荷。
  """
  records = await store.list_plans()
  rows: dict[str, dict] = {}
  cols: dict[str, dict] = {}
  for rec in records:
    rec = rec if isinstance(rec, dict) else {}
    plan_id = str(rec.get('plan_id') or '')
    if not plan_id:
      continue
    dept_id = str(rec.get('dept_id') or '')
    row = rows.get(f'{dept_id}|{plan_id}')
    if row is None:
      row = {
        'plan_id': plan_id,
        'plan_name': str(rec.get('plan_name') or ''),
        'dept_id': dept_id,
        'dept_name': str(rec.get('dept_name') or ''),
        'company_name': str(rec.get('company_name') or ''),
        'speciality': str(rec.get('speciality') or ''),
        'year': int(rec.get('year') or 0),
        'weeks': [],
      }
      rows[f'{dept_id}|{plan_id}'] = row
    for week in rec.get('weeks') or []:
      week = week if isinstance(week, dict) else {}
      key = str(week.get('key') or '')
      if not key:
        continue
      if all(w['key'] != key for w in row['weeks']):
        row['weeks'].append({
          'key': key,
          'week_id': str(week.get('week_id') or ''),
          'finish_status': str(week.get('finish_status') or ''),
          'file_status': str(week.get('file_status') or ''),
        })
      meta = {
        'key': key,
        'week_no': str(week.get('week_no') or ''),
        'stime': str(week.get('stime') or ''),
        'etime': str(week.get('etime') or ''),
      }
      prev = cols.get(key)
      if prev is None or (meta['stime'] and meta['stime'] < (prev.get('stime') or '~')):
        cols[key] = meta
  out_rows = sorted(rows.values(), key=lambda r: (r['dept_name'], r['plan_name'], r['plan_id']))
  out_cols = sorted(cols.values(), key=lambda c: (c['stime'] or '9999-12-31', c['key']))
  return out_rows, out_cols


async def build_overview(period: str = '', limit: int = 200) -> dict:
  """页面用：开关、部门列、可选月份、已审周摘要、全部演练任务、是否在跑、最近一次运行。"""
  if not settings.DRILL_AUDIT_ENABLED:
    return {'enabled': False, 'depts': [], 'periods': [], 'weeks': [],
            'plan_rows': [], 'plan_columns': [], 'running': False, 'progress': '',
            'latest_run': {}, 'plan_refreshing': False, 'plan_updated_at': ''}

  depts = await resolve_depts()
  weeks = await store.list_weeks(limit=limit)
  plan_rows, plan_columns = await _plan_matrix()

  target = (period or '').strip()
  if target:
    weeks = [w for w in weeks
             if str(w.get('stime') or w.get('etime') or '')[:7] == target]

  months = sorted({str(w.get('stime') or w.get('etime') or '')[:7]
                   for w in await store.list_weeks(limit=limit) if w.get('stime')},
                  reverse=True)
  periods = [{'key': m, 'label': m} for m in months if m]

  return {
    'enabled': True,
    'depts': [
      {'company_id': str(d.get('company_id') or ''),
       'company_name': str(d.get('company_name') or ''),
       'dept_id': str(d.get('dept_id') or ''),
       'dept_name': str(d.get('dept_name') or ''),
       'error': str(d.get('error') or '')}
      for d in depts
    ],
    'periods': periods,
    'weeks': weeks,
    'plan_rows': plan_rows,
    'plan_columns': plan_columns,
    'running': _is_running(),
    'progress': _get_progress() if _is_running() else '',
    'latest_run': await _latest_run(),
    'plan_refreshing': _is_roster_running(),
    'plan_updated_at': await store.latest_plan_update(),
  }

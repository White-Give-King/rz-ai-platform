"""日常运维：每日变更统计工作流（流式无 LLM）。

窗口＝**当天**（`Asia/Shanghai` 00:00 ~ 运行时刻，按消息 `create_time` 归属）。
定时预设 18:10 跑时即「当天截至 18:10」；当天更晚的消息次日那轮不再回头统计。
只统计人发的消息：机器人（`app_id`）消息**不入库**，WS 与回补两路都丢弃；
读侧仍要求 `sender_id_type='open_id'` 作纵深防御。

落库与产物路径对齐事件统计表 / 不合规检查：`finish_run` 必写
status/artifact_urls/error/duration_ms；产物落 `{DATA_DIR}/{thread_id}/{dataset_id}/`，
**文件名纯 ASCII**。0 行**不算失败**：照出空表，摘要写「本日无变更记录」。
"""

import asyncio
import json
import logging
import time
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from config.redis_conn import redis_manager
from config.settings import settings
from core.sandbox.datasets import load_dataset_dir, save_dataset
from models.scheduled_task import RunStatus
from service.scheduled_task import repository as repo
from service.scheduled_task.change_notice import (
  build, config, extract, parse, people, store, xlsx,
)
from service.scheduled_task.change_notice import source as change_source
from service.scheduled_task.incident_stats import feishu, schema

logger = logging.getLogger(__name__)

_TZ = ZoneInfo('Asia/Shanghai')
_ARTIFACT_PREFIX = '/api/v1/chat/agent/artifacts/'

# 建表 DDL 进程内只跑一次（原先每次请求都执行 CREATE TABLE/INDEX，两个并发请求还会等锁）
_schema_ready = False

# 结果缓存与单飞（同一时刻 overview + records 两个请求只算一次）
_EVENTS_PREFIX = 'change:events:'
_LOCK_PREFIX = 'change:events:lock:'
_LOCK_TTL = 120
_WAIT_POLL = 0.5
_WAIT_MAX = 30.0
_RELEASE_LUA = (
  "if redis.call('get', KEYS[1]) == ARGV[1] then"
  " return redis.call('del', KEYS[1]) else return 0 end"
)


def today_window(now: datetime | None = None) -> dict:
  """当天：[今日 00:00, 当前时刻)。"""
  current = (now or datetime.now(_TZ)).astimezone(_TZ)
  start = current.replace(hour=0, minute=0, second=0, microsecond=0)
  return {
    'start': start, 'end': current,
    'start_s': int(start.timestamp()), 'end_s': int(current.timestamp()),
    'key': start.strftime('%Y-%m-%d'), 'label': start.strftime('%Y-%m-%d'),
  }


def file_name(window: dict) -> str:
  return f"change_notice_{window['start'].strftime('%Y%m%d')}.xlsx"


def _human_only(rows: list[dict]) -> list[dict]:
  """机器人（`id_type=app_id`）消息不入变更表；WS 已在入口丢弃，这里兜住回补/回捞。"""
  return [r for r in rows if (r.get('sender_id_type') or '') != 'app_id']


async def sync_window(start_s: int, end_s: int, parks: list[str] | None = None) -> dict:
  """按 `CHANGE_NOTICE_FEISHU_PARK_CHATS` 回补窗口内群消息（只入库，不做告警匹配）。"""
  mapping = config.park_chat_map(parks)
  # 实时入库关闭时表可能还没建（三条入口都经这里兜底）；建表进程内只跑一次
  await asyncio.to_thread(_ensure_schema)
  saved = recalled_n = 0
  failed: list[str] = []
  for park, chat_id in mapping.items():
    try:
      rows, recalled = await feishu.fetch_messages(chat_id, start_s, end_s)
      for message_id in recalled:
        change_source.delete_report(chat_id, message_id)
      recalled_n += len(recalled)
      saved += change_source.save_reports(_human_only(rows))
    except Exception as e:  # 单群失败不拖垮其余园区
      logger.warning('[ChangeNotice] 同步群消息失败 park=%s chat=%s: %s', park, chat_id, e)
      failed.append(park)
  return {'saved': saved, 'recalled': recalled_n, 'failed': failed}


def _ensure_schema() -> None:
  global _schema_ready
  if _schema_ready:
    return
  schema.ensure_change_schema(settings.DCIM_PG_DSN)
  _schema_ready = True


def _day_of(start_s: int) -> str:
  return datetime.fromtimestamp(start_s, _TZ).strftime('%Y-%m-%d')


def _parks_key(parks: list[str] | None) -> str:
  names = sorted({p for p in (parks or []) if p})
  return '|'.join(names) if names else 'all'


def _events_key(day: str, parks: list[str] | None, count: int, max_id: int) -> str:
  """缓存键带数据水位：新消息入库 / 撤回删行都会让键变化 → 下次必重算。"""
  return f'{_EVENTS_PREFIX}{day}:{_parks_key(parks)}:{count}:{max_id}'


def _cache_ttl() -> int:
  return int(getattr(settings, 'CHANGE_NOTICE_PAGE_CACHE_SECONDS', 0) or 0)


def _cache_get(key: str) -> list[dict] | None:
  if not key or _cache_ttl() <= 0:
    return None
  try:
    raw = redis_manager.client.get(redis_manager.key(key))
  except Exception as e:
    logger.warning('[ChangeNotice] 读结果缓存失败: %s', e)
    return None
  if not raw:
    return None
  try:
    return store.decode(json.loads(raw))
  except (TypeError, ValueError):
    return None


def _cache_put(key: str, events: list[dict]) -> None:
  ttl = _cache_ttl()
  if not key or ttl <= 0:
    return
  try:
    redis_manager.client.set(
      redis_manager.key(key),
      json.dumps(store.encode(events), ensure_ascii=False), ex=ttl)
  except Exception as e:
    logger.warning('[ChangeNotice] 写结果缓存失败: %s', e)


def _lock_acquire(key: str) -> str | None:
  """抢单飞锁：`None` = Redis 不可用（调用方直接算）；`''` = 别人在算（等缓存）。"""
  token = uuid.uuid4().hex
  try:
    ok = redis_manager.client.set(
      redis_manager.key(_LOCK_PREFIX + key), token, nx=True, ex=_LOCK_TTL)
  except Exception as e:
    logger.warning('[ChangeNotice] 抢计算锁失败（直接计算）: %s', e)
    return None
  return token if ok else ''


def _lock_release(key: str, token: str) -> None:
  try:
    redis_manager.client.eval(
      _RELEASE_LUA, 1, redis_manager.key(_LOCK_PREFIX + key), token)
  except Exception as e:
    logger.warning('[ChangeNotice] 释放计算锁失败: %s', e)


async def _wait_cache(key: str) -> list[dict] | None:
  """等别的请求算完并写缓存（最多约 30s，等不到就自己算）。"""
  deadline = time.monotonic() + _WAIT_MAX
  while True:
    events = _cache_get(key)
    if events is not None:
      return events
    if time.monotonic() >= deadline:
      return None
    await asyncio.sleep(_WAIT_POLL)


def _parse_rows(start_s: int, end_s: int, parks: list[str] | None = None) -> list[dict]:
  """读库 → 只留人发消息 → 确定性解析（同步；不调模型、不配对）。"""
  mapping = config.park_chat_map(parks)
  if not mapping:
    raise RuntimeError('未配置 CHANGE_NOTICE_FEISHU_PARK_CHATS')
  _ensure_schema()
  chat_to_park = {chat_id: park for park, chat_id in mapping.items()}
  messages = change_source.load_messages(start_s, end_s, chat_ids=list(chat_to_park))
  parsed: list[dict] = []
  for row in messages:
    if (row.get('sender_id_type') or '') != 'open_id':
      continue
    park = chat_to_park.get(row.get('chat_id') or '', '')
    event = parse.parse_message(
      row.get('text') or '', row.get('create_time') or 0, park=park,
      sender=(row.get('sender') or row.get('sender_id') or ''))
    if event:
      parsed.append(event)
  return parsed


async def _compute(start_s: int, end_s: int,
                   parks: list[str] | None = None) -> list[dict]:
  parsed = await asyncio.to_thread(_parse_rows, start_s, end_s, parks)
  await extract.enrich(parsed)
  events = build.build_events(parsed)
  await people.attach_levels(events)
  return events


async def collect_events(start_s: int, end_s: int,
                         parks: list[str] | None = None) -> list[dict]:
  """读库解析 →（确定性取不到时）模型补字段 → 配对去重。

  页面/对话/定时/CLI 共用这一个口径：模型兜底只在 `parse` 记下未识别的
  语义标签时触发，且只填空白字段。负责人职级在**配对去重之后**补（`contact`
  已合并成最终值），内部 fail-open，查不到只留空。

  结果按**数据水位缓存 + Mongo 快照**：Redis 缓存只在 `CHANGE_NOTICE_PAGE_CACHE_SECONDS`
  内有效；跨过它（或换日期）后先读 Mongo 快照——同一天、同园区集合、同 count/max_id
  且版本一致就直接复用（历史日期的数据不再变，因此第二次起秒开）。水位变了才重算。
  页面 overview 与 records 并发发出时由单飞锁保证只算一次，另一个等结果。
  Redis / Mongo 不可用一律照常计算。
  """
  mapping = config.park_chat_map(parks)
  if not mapping:
    raise RuntimeError('未配置 CHANGE_NOTICE_FEISHU_PARK_CHATS')

  day = _day_of(start_s)
  parks_key = _parks_key(parks)

  key = ''
  count = max_id = None
  try:
    count, max_id = await asyncio.to_thread(
      change_source.message_stats, start_s, end_s, list(mapping.values()))
    key = _events_key(day, parks, count, max_id)
  except Exception as e:
    logger.warning('[ChangeNotice] 取数据水位失败（本次不缓存）: %s', e)

  if not key:
    return await _compute(start_s, end_s, parks)

  async def compute_persist() -> list[dict]:
    """算一次并写回 Redis + Mongo（任一路失败都不影响返回）。"""
    events = await _compute(start_s, end_s, parks)
    _cache_put(key, events)
    await store.save(day, parks_key, count, max_id, events)
    return events

  cached = _cache_get(key)
  if cached is not None:
    return cached

  stored = await store.get(day, parks_key)
  if (stored is not None and stored.get('count') == count
      and stored.get('max_id') == max_id):
    _cache_put(key, stored['events'])
    return stored['events']

  token = _lock_acquire(key)
  if token:
    try:
      return await compute_persist()
    finally:
      _lock_release(key, token)
  if token is None:
    return await compute_persist()
  # 另一个请求正在算：等它的结果，等不到再自己算一次（fail-open）
  cached = await _wait_cache(key)
  if cached is not None:
    return cached
  return await compute_persist()


def summary_text(window: dict, rows: list[dict], groups: list[dict],
                 name: str, thread_id: str, dataset_id: str) -> str:
  url = f'{_ARTIFACT_PREFIX}{thread_id}/{dataset_id}/{name}'
  lines = [f'《每日变更统计》{window["label"]}。']
  if rows:
    counts = ' · '.join(f"{g['park']} {g['total']}" for g in groups)
    lines.append(f'- 共 {len(rows)} 条：{counts}')
  else:
    lines.append('- 本日无变更记录。')
  lines.append(f'完整表格见 xlsx 产物：<file url="{url}" name="{name}"/>')
  return '\n'.join(lines)


async def _notify_outcome(task, run, status, final_text: str, error: str) -> None:
  recipients = [task.created_by]
  batch_key = ''
  if status == RunStatus.FAILED and task.created_by == repo.SYSTEM_USER_ID:
    recipients = await repo.alert_user_ids() or [task.created_by]
    batch_key = repo.alert_batch_key()

  if batch_key:
    title = '每日变更统计执行失败'
    body = f'「{task.name}」{(final_text or error).strip()[:120]}'
  else:
    title = (f"定时任务「{task.name}」执行"
             f"{'成功' if status == RunStatus.SUCCESS else '失败'}")
    body = (final_text or error).strip()[:80]

  for user_id in recipients:
    await repo.create_notification(
      user_id=user_id, task_id=task.task_id, task_name=task.name, run_id=run.run_id,
      status=status.value, title=title, body=body, batch_key=batch_key,
    )


async def run_change_notice_task(task) -> None:
  """跑一期每日变更统计。**不抛异常**（全部落库），成败由运行记录与通知体现。"""
  run_id = uuid.uuid4().hex
  thread_id = f'rz-agent-sched-{task.task_id}-{run_id}'
  window = today_window()
  run = await repo.create_run(task.task_id, run_id=run_id, thread_id=thread_id,
                              preset_key=task.template_id, period_key=window['key'],
                              period_label=window['label'])
  start_ts = time.time()
  error, final_text, artifacts = '', '', []
  counts: dict[str, int] | None = None

  try:
    events = await collect_events(window['start_s'], window['end_s'])
    rows = build.to_rows(events)
    groups = build.overview(events)
    params = {
      'period': window['key'],
      'start': window['start'].isoformat(),
      'end': window['end'].isoformat(),
    }
    info = save_dataset(thread_id, 'change_notice', params, rows,
                        dataset_name='change_notice')
    dataset_id = info['dataset_id']
    ds_dir = load_dataset_dir(thread_id, dataset_id)
    name = file_name(window)
    xlsx.write_workbook(ds_dir / name, rows, groups,
                        title='变更统计明细', period=window['label'])

    artifacts = [f'{_ARTIFACT_PREFIX}{thread_id}/{dataset_id}/{name}']
    final_text = summary_text(window, rows, groups, name, thread_id, dataset_id)
    counts = {g['park']: g['total'] for g in groups}
  except Exception as e:
    logger.exception('每日变更统计工作流失败 task_id=%s', task.task_id)
    error = f'{type(e).__name__}: {str(e)[:400]}'

  duration_ms = int((time.time() - start_ts) * 1000)
  status = RunStatus.FAILED if error else RunStatus.SUCCESS
  await repo.finish_run(run.run_id, status=status, final_reply=final_text,
                        artifact_urls=artifacts, error=error, duration_ms=duration_ms,
                        thread_id=thread_id, check_counts=counts)
  await _notify_outcome(task, run, status, final_text, error)
  logger.info('每日变更统计工作流完成 task_id=%s period=%s status=%s duration_ms=%s',
              task.task_id, window['key'], status.value, duration_ms)

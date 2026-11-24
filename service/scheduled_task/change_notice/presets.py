"""每日变更统计的定时任务预设（单条 all 每日任务）。

`CHANGE_NOTICE_ENABLED=true` 才建，cron 读 `CHANGE_NOTICE_CRON`（默认当天 18:10 统计
当天 00:00 ~ 运行时刻）。所有已配置园区合并成一张表，只建一条 `change_notice__all__daily`。
与报告/事件/不合规是**各自的 system 任务族**，`disable_stale_system_tasks(prefix=…)`
只清理本族，互不误停。
"""

import logging

from config.settings import settings
from service.scheduled_task.change_notice import config

logger = logging.getLogger(__name__)

_FAMILY = 'change_notice'
PREFIX = f'{_FAMILY}__'
ALL_PARKS = 'all'
DAILY = 'daily'
KEY = f'{PREFIX}{ALL_PARKS}__{DAILY}'


def enabled() -> bool:
  return config.enabled()


def preset_cron() -> str:
  return config.cron()


def preset_name() -> str:
  return '每日变更统计 · 全部园区 · 每日'


def render_prompt() -> str:
  """预设任务必填的指令字段。实际执行走确定性工作流，不消费这段文本。"""
  return ('统计当天各园区日常运维群中**人发送**的变更通知，'
          '记录园区 × 数据中心的变更类型 / 维护事项 / 执行状态，输出 xlsx'
          '（变更统计明细 + 统计概览）。机器人卡片消息不计入。')


def _warn_ingest_drift() -> None:
  """change 群必须在 ingest 白名单里，否则实时入库收不到 —— 两处配置别漂移。"""
  chats = set(config.park_chat_map().values())
  if not chats:
    return
  whitelist = {
    part.strip() for part in
    (getattr(settings, 'FEISHU_GROUP_INGEST_CHATS', '') or '').split(',') if part.strip()
  }
  missing = chats - whitelist
  if missing:
    logger.warning('变更统计群未在 FEISHU_GROUP_INGEST_CHATS 白名单内，实时入库收不到: %s',
                   sorted(missing))


async def ensure_change_notice_presets() -> list[str]:
  """启动时幂等建/同步每日变更统计预设（多 worker 用 Redis 选主）。"""
  from service.channels.leader import LeaderLock
  from service.scheduled_task import repository as repo

  if not enabled():
    try:
      stale = await repo.disable_stale_system_tasks([], prefix=PREFIX)
      if stale:
        logger.warning('每日变更统计已关闭，停用旧预设: %s', stale)
    except Exception as e:
      logger.warning('停用旧变更统计预设失败（不影响启动）: %s', e)
    return []

  _warn_ingest_drift()

  lock = LeaderLock('sched:change-notice-preset-ensure', ttl=300)
  try:
    acquired = lock.try_acquire()
  except Exception as e:
    logger.warning('变更统计预设初始化锁获取失败（继续执行）: %s', e)
    acquired = True
  if not acquired:
    logger.info('变更统计预设已由其他 worker 初始化，跳过')
    return []

  try:
    planned = [KEY]
    created: list[str] = []
    try:
      await repo.ensure_system_task(
        KEY,
        name=preset_name(),
        description='每日变更统计：园区日常运维群人发变更通知 → 园区×数据中心明细，产出 xlsx',
        cron_expr=preset_cron(),
        prompt=render_prompt(),
        task_mode='normal',
        thread_strategy='new',
      )
      created.append(KEY)
    except Exception as e:
      logger.warning('变更统计预设 %s 初始化失败（跳过）: %s', KEY, e)

    try:
      stale = await repo.disable_stale_system_tasks(planned, prefix=PREFIX)
      if stale:
        logger.warning('已停用下线的变更统计预设: %s', stale)
    except Exception as e:
      logger.warning('清理下线变更预设失败（不影响启动）: %s', e)
    try:
      dup = await repo.disable_duplicate_system_tasks(planned)
      if dup:
        logger.warning('已停用 %d 条重复的变更预设: %s', len(dup), dup)
    except Exception as e:
      logger.warning('清理重复变更预设失败（不影响启动）: %s', e)
    return created
  finally:
    lock.release()

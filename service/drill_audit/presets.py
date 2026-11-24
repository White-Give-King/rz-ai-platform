"""应急演练合规审查的每周定时预设（默认周一 07:10 跑一轮）。

**默认关闭**（`DRILL_AUDIT_WEEKLY_ENABLED=false`，且需 `DRILL_AUDIT_ENABLED=true`）：
`ensure_drill_audit_presets()` 直接返回空，不建任务、不跑 cron。

预设是**独立一族** system 任务（前缀 `drill_audit__`）：`report_templates` 的
stale 清理必须排除本前缀，否则启动时会被当成下线预设停用。

与报告预设不同：这是**确定性流水线**，不消费 `render_prompt` 的文本（该字段只是
schema 必填），实际执行见 `service/drill_audit/weekly.py::run_weekly_task`。
"""

import logging

from config.settings import settings

logger = logging.getLogger(__name__)

PREFIX = 'drill_audit__'
KEY = f'{PREFIX}weekly'
NAME = '应急演练记录合规审查 · 每周'


def enabled() -> bool:
  # 总开关与每周开关都要开：只开每周而关掉总开关时，工具与流水线都不该出现。
  return bool(settings.DRILL_AUDIT_ENABLED and settings.DRILL_AUDIT_WEEKLY_ENABLED)


def is_preset_key(key: str) -> bool:
  return (key or '').startswith(PREFIX)


def preset_cron(_key: str = KEY) -> str:
  """只依赖固定时刻，ensure 幂等、重启不抖动。"""
  return settings.DRILL_AUDIT_WEEKLY_CRON or '10 7 * * 1'


def render_prompt(_key: str = KEY) -> str:
  """预设任务必填的指令字段。实际执行走确定性流水线，不消费这段文本。"""
  return ('扫描各公司各部门到点该审的演练周（已安排、有记录、etime 在回溯窗口内），'
          '逐周下载班组记录 OCR 后比对知识库 EOP，落每周合规结论；不产出附件。')


async def ensure_drill_audit_presets() -> list[str]:
  """启动时幂等建/同步演练审查周预设（多 worker 用 Redis 选主）。"""
  if not enabled():
    return []
  from service.channels.leader import LeaderLock
  from service.scheduled_task import repository as repo

  lock = LeaderLock('sched:drill-audit-preset-ensure', ttl=300)
  try:
    acquired = lock.try_acquire()
  except Exception as e:
    logger.warning('演练审查周预设初始化锁获取失败（继续执行）: %s', e)
    acquired = True
  if not acquired:
    logger.info('演练审查周预设已由其他 worker 初始化，跳过')
    return []

  try:
    try:
      await repo.ensure_system_task(
        KEY,
        name=NAME,
        description='每周扫描到点该审的演练周并落合规结论',
        cron_expr=preset_cron(KEY),
        prompt=render_prompt(KEY),
        task_mode='normal',
        thread_strategy='new',
      )
    except Exception as e:
      logger.warning('演练审查周预设初始化失败（跳过）: %s', e)
      return []

    try:
      stale = await repo.disable_stale_system_tasks([KEY], prefix=PREFIX)
      if stale:
        logger.warning('已停用下线的演练审查周预设: %s', stale)
    except Exception as e:
      logger.warning('清理下线演练审查周预设失败（不影响启动）: %s', e)
    try:
      dup = await repo.disable_duplicate_system_tasks([KEY])
      if dup:
        logger.warning('已停用 %d 条重复的演练审查周预设: %s', len(dup), dup)
    except Exception as e:
      logger.warning('清理重复演练审查周预设失败（不影响启动）: %s', e)
    return [KEY]
  finally:
    lock.release()

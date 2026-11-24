"""日常运维：每日变更通知统计的配置读取。

园区与群映射写在 `CHANGE_NOTICE_FEISHU_PARK_CHATS`（JSON `{园区: chat_id}`，
与 `INCIDENT_FEISHU_PARK_CHATS` 刻意分开：日常群绝不能进事件通报配置）。
解析统一委托 `incident_stats.feishu.change_park_chats()`（单一解析源，入口路由与
统计读的是同一份映射），解析失败一律返回空字典，由调用方按「未配置」处理（不抛异常）。
"""

from config.settings import settings
from service.scheduled_task.incident_stats import feishu


def enabled() -> bool:
  return bool(getattr(settings, 'CHANGE_NOTICE_ENABLED', False))


def park_chat_map(parks: list[str] | None = None) -> dict[str, str]:
  """`CHANGE_NOTICE_FEISHU_PARK_CHATS` → `{园区: chat_id}`；`parks` 非空时按园区过滤。"""
  return feishu.change_park_chats(parks)


def configured_parks() -> list[str]:
  return list(park_chat_map())


def cron() -> str:
  return (getattr(settings, 'CHANGE_NOTICE_CRON', '') or '10 18 * * *').strip()

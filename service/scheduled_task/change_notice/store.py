"""变更统计结果持久化（Mongo），避免历史日期每次重算。

`collect_events` 的 Redis 结果缓存只有 `CHANGE_NOTICE_PAGE_CACHE_SECONDS`（默认 120s）
且按数据水位换键，换日期就是换键 → 冷启动要重读库、调模型补字段、查负责人等级。
历史日期的数据不会再变，所以把「某天 + 某园区集合」算好的事件落一份快照：
水位（count/max_id）与版本号都对得上就直接复用。

时间字段一律走 `encode`/`decode` 的 `{'__dt__': iso}`：**不能存原生 BSON datetime**
——它读出是 naive UTC，会把北京时间显示早 8 小时。Mongo 不可用一律 fail-open。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from config.mongodb_conn import mongodb_manager

logger = logging.getLogger(__name__)

COLLECTION = 'change_notice_snapshots'
SNAPSHOT_VERSION = 1
_OP_TIMEOUT = 3
_DT_TAG = '__dt__'


def encode(value):
  """datetime → `{'__dt__': iso}`（事件里的计划/实际时间要原样还原给 `build`）。"""
  if isinstance(value, datetime):
    return {_DT_TAG: value.isoformat()}
  if isinstance(value, dict):
    return {k: encode(v) for k, v in value.items()}
  if isinstance(value, (list, tuple)):
    return [encode(v) for v in value]
  return value


def decode(value):
  if isinstance(value, dict):
    if list(value.keys()) == [_DT_TAG]:
      try:
        return datetime.fromisoformat(value[_DT_TAG])
      except (TypeError, ValueError):
        return None
    return {k: decode(v) for k, v in value.items()}
  if isinstance(value, list):
    return [decode(v) for v in value]
  return value


def _key(day: str, parks_key: str) -> str:
  return f'{day}|{parks_key}'


async def get(day: str, parks_key: str) -> dict | None:
  """读快照：`{count, max_id, events}`；版本不符/空/异常一律返回 None（fail-open）。"""
  try:
    doc = await asyncio.wait_for(
      mongodb_manager.db[COLLECTION].find_one({'_id': _key(day, parks_key)}),
      timeout=_OP_TIMEOUT)
  except Exception as e:
    logger.warning('[ChangeNotice] 读结果快照失败 day=%s parks=%s: %s', day, parks_key, e)
    return None
  if not isinstance(doc, dict) or doc.get('version') != SNAPSHOT_VERSION:
    return None
  events = decode(doc.get('events'))
  if not isinstance(events, list):
    return None
  return {'count': doc.get('count'), 'max_id': doc.get('max_id'), 'events': events}


async def save(day: str, parks_key: str, count: int | None, max_id: int | None,
               events: list[dict]) -> None:
  """落快照（按 `(day, parks_key)` 覆盖）；任何异常只记日志，绝不影响出表。"""
  now = datetime.now(timezone.utc)
  try:
    await asyncio.wait_for(
      mongodb_manager.db[COLLECTION].update_one(
        {'_id': _key(day, parks_key)},
        {'$set': {
            'day': day, 'parks_key': parks_key, 'version': SNAPSHOT_VERSION,
            'count': count, 'max_id': max_id, 'events': encode(events),
            'updated_at': now,
          },
         '$setOnInsert': {'created_at': now}},
        upsert=True,
      ),
      timeout=_OP_TIMEOUT)
  except Exception as e:
    logger.warning('[ChangeNotice] 写结果快照失败 day=%s parks=%s: %s', day, parks_key, e)

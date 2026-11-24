"""变更报文字段抽取兜底：确定性解析取不到时，让模型从原文抽。

现场报文写法一直在变（实测 2026-11-19 廊坊 53 条里有 5 条带未登记的
`实际开始时间 / 实际结束时间 / 完成时间 / 变更完成描述`）。**不再逐个人工补别名**，
改为：`parse` 把未识别但带语义提示词的标签记进 `unknown`，这里交给模型抽。

三条约束（同 `incident_stats/extract.py`）：
1. 只在确定性解析缺字段时调用（`needs_llm`），已有值**绝不覆盖**；
2. 抽到的时间必须过 `parse.parse_dt` 且落在消息时间附近，否则丢弃 —— 宁可留空；
3. 同样的报文只算一次：结果按 `sha1` 缓存 Redis。页面/对话/定时任务共用同一份缓存。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from config.redis_conn import redis_manager
from config.settings import settings
from service.scheduled_task.change_notice import parse

logger = logging.getLogger(__name__)

_TZ = ZoneInfo('Asia/Shanghai')
_MAX_TEXT = 2000
_MAX_TOKENS = 400
_CACHE_PREFIX = 'change:extract:'
_CACHE_TTL = 30 * 86400
_PLAN_DAYS = 60       # 计划时间允许偏离消息时间的范围
_ACTUAL_DAYS = 7      # 实际开始/完成时间

_SYSTEM = (
  '你是变更报文信息抽取器。只从用户给的原文里摘录字段，禁止推测、换算、补全。\n'
  '输出 JSON：{"planned_start": str|null, "planned_end": str|null,'
  ' "actual_start": str|null, "actual_end": str|null,'
  ' "contact": str|null, "reason": str|null, "title": str|null,'
  ' "dc": str|null, "level": str|null, "scope": str|null,'
  ' "ticket_no": str|null, "progress": str|null}。\n'
  '时间一律写成 "YYYY-MM-DD HH:MM"。actual_start/actual_end 是实际开始/结束时间，'
  '「完成时间」算 actual_end；不要拿计划时间顶替。contact 只填负责人姓名，'
  '不要填电话号码（电话不是负责人）。抽不到就返回 null，不要编造。'
)

_FENCE_RE = re.compile(r'^```[a-zA-Z]*\s*|\s*```$')
_JSON_RE = re.compile(r'\{[\s\S]*\}')
_TEXT_KEYS = ('contact', 'reason', 'title', 'scope', 'progress', 'level', 'ticket_no')
# 原文出现这些词 → 说明本该有负责人字段；确定性解析却没抽出姓名时交给模型兜底
_PERSON_CUE = re.compile(r'负责人|工程师|联系人|值班人员|姓名')


def _loads(raw: str) -> dict:
  text = _FENCE_RE.sub('', (raw or '').strip())
  try:
    data = json.loads(text)
  except (TypeError, ValueError):
    match = _JSON_RE.search(text)
    if not match:
      return {}
    try:
      data = json.loads(match.group())
    except (TypeError, ValueError):
      return {}
  return data if isinstance(data, dict) else {}


def needs_llm(event: dict) -> bool:
  """未登记标签，或原文有负责人字段却没解析出姓名时，才值得调模型。

  后者是安全网：像「现场联系人：值班电话 …」这种，标签都登记了、`unknown` 为空，
  但确定性解析归一后一个姓名都没有 —— 只有模型能从非标写法里捞出人名。
  """
  if event.get('unknown'):
    return True
  return not event.get('contact') and bool(_PERSON_CUE.search(event.get('_text', '')))


def _valid_ts(value, ref_ts: int | None, days: int) -> int:
  """模型给的时间 → epoch；解析不了或偏离消息时间太远，丢弃（返回 0）。"""
  if value is None or isinstance(value, bool):
    return 0
  dt = parse.parse_dt(str(value))
  if dt is None:
    logger.warning('[ChangeNotice] 抽取的时间解析不出来，丢弃：%s', str(value)[:60])
    return 0
  ts = int(dt.timestamp())
  if ref_ts and abs(ts - int(ref_ts)) > days * 86400:
    logger.warning('[ChangeNotice] 抽取的时间偏离消息时间太远，丢弃：%s（消息 %s）',
                   str(value)[:60], ref_ts)
    return 0
  return ts


def _text(value) -> str:
  return value.strip() if isinstance(value, str) else ''


def normalize(event: dict, data: dict) -> dict:
  """模型 JSON → 已校验、可 JSON 序列化（时间存 epoch）的中间结果。"""
  ref = event.get('msg_time') or None
  ticket = _text(data.get('ticket_no'))
  dc_raw = _text(data.get('dc'))
  return {
    'planned_start': _valid_ts(data.get('planned_start'), ref, _PLAN_DAYS),
    'planned_end': _valid_ts(data.get('planned_end'), ref, _PLAN_DAYS),
    'actual_start': _valid_ts(data.get('actual_start'), ref, _ACTUAL_DAYS),
    'actual_end': _valid_ts(data.get('actual_end'), ref, _ACTUAL_DAYS),
    **{key: _text(data.get(key)) for key in _TEXT_KEYS},
    'dc': parse._dc_of(event.get('ticket_no') or ticket, dc_raw) if dc_raw else '',
  }


def apply(event: dict, norm: dict) -> bool:
  """把校验过的值写回事件：**只填空字段**，返回是否有改动。"""
  changed = False
  for key in ('planned_start', 'planned_end', 'actual_start', 'actual_end'):
    ts = norm.get(key) or 0
    if ts and event.get(key) is None:
      event[key] = datetime.fromtimestamp(int(ts), _TZ)
      changed = True
  if norm.get('ticket_no') and not event.get('ticket_no'):
    event['ticket_no'] = norm['ticket_no']
    changed = True
  if norm.get('dc') and not event.get('dc'):
    event['dc'] = norm['dc']
    changed = True
  # 负责人只接受能归一成姓名的值（模型可能又填回电话串），且绝不覆盖已有姓名
  if norm.get('contact') and not event.get('contact'):
    names = parse.contact_names(str(norm['contact']))
    if names:
      event['contact'] = '、'.join(names)
      changed = True
  for key in ('reason', 'title', 'scope', 'progress'):
    if norm.get(key) and not event.get(key):
      event[key] = norm[key]
      changed = True
  if norm.get('level') and not event.get('level_raw'):
    event['level_raw'] = norm['level']
    event['type'] = parse.change_type(norm['level'])
    changed = True
  return changed


def _cache_key(event: dict) -> str:
  seed = f"{event.get('fmt')}|{event.get('msg_time')}|{event.get('_text')}"
  return _CACHE_PREFIX + hashlib.sha1(seed.encode('utf-8')).hexdigest()


def _cache_read(event: dict) -> dict | None:
  try:
    raw = redis_manager.client.get(redis_manager.key(_cache_key(event)))
  except Exception:
    return None          # Redis 不可用 → fail-open，照常调模型
  if not raw:
    return None
  try:
    data = json.loads(raw)
  except (TypeError, ValueError):
    return None
  return data if isinstance(data, dict) else None


def _cache_write(event: dict, norm: dict) -> None:
  """只缓存**正常返回**的结果（含「模型也抽不到」）；传输失败不写，下一轮还要再试。"""
  try:
    redis_manager.client.set(redis_manager.key(_cache_key(event)),
                             json.dumps(norm, ensure_ascii=False), ex=_CACHE_TTL)
  except Exception as e:
    logger.debug('[ChangeNotice] 抽取结果缓存写入失败：%s', e)


def _prompt(event: dict) -> str:
  text = str(event.get('_text') or '')[:_MAX_TEXT]
  msg_time = event.get('msg_time')
  stamp = ''
  if msg_time:
    stamp = datetime.fromtimestamp(int(msg_time), _TZ).strftime('%Y-%m-%d %H:%M:%S')
  return (f"园区：{event.get('park') or '未知'}\n"
          f'消息发送时间：{stamp or "未知"}\n原文：\n{text}')


async def _one(event: dict) -> bool:
  """单条：缓存 → 模型 → 校验 → 写回。返回是否真的补上了字段。"""
  norm = _cache_read(event)
  if norm is None:
    from core.model_gateway import model_gateway

    try:
      raw = await model_gateway.chat(
        messages=[{'role': 'system', 'content': _SYSTEM},
                  {'role': 'user', 'content': _prompt(event)}],
        temperature=0, max_tokens=_MAX_TOKENS, json_mode=True,
        timeout=int(settings.CHANGE_NOTICE_EXTRACT_LLM_TIMEOUT),
        caller='change_notice_extract',
      )
    except Exception as e:
      logger.warning('[ChangeNotice] 报文字段抽取调用失败 msg_time=%s: %s',
                     event.get('msg_time'), str(e)[:200])
      return False
    data = _loads(raw)
    if not data:
      logger.warning('[ChangeNotice] 报文抽取返回非 JSON msg_time=%s: %s',
                     event.get('msg_time'), str(raw)[:120])
      return False
    norm = normalize(event, data)
    _cache_write(event, norm)

  changed = apply(event, norm)
  if changed:
    logger.info('[ChangeNotice] 模型补全报文字段 msg_time=%s unknown=%s',
                event.get('msg_time'), event.get('unknown'))
  return changed


async def enrich(events: list[dict], max_calls: int | None = None) -> int:
  """批量补字段（有界并发、有上限）。返回被补全的条数；任何异常都不影响出表。

  预算仍按「每轮最多 N 条」裁前 N 个需要兜底的报文，但改为并发调用
  （`CHANGE_NOTICE_EXTRACT_LLM_CONCURRENCY`），避免串行时 5 条 × 15s 拖成 75s。
  """
  if not settings.CHANGE_NOTICE_EXTRACT_LLM_ENABLED or not events:
    return 0
  budget = int(max_calls if max_calls is not None
               else settings.CHANGE_NOTICE_EXTRACT_LLM_MAX_PER_ROUND)
  if budget <= 0:
    return 0
  targets = [e for e in events if needs_llm(e)][:budget]
  if not targets:
    return 0

  conc = max(1, int(getattr(settings, 'CHANGE_NOTICE_EXTRACT_LLM_CONCURRENCY', 4) or 1))
  sem = asyncio.Semaphore(conc)

  async def _run(event: dict) -> bool:
    async with sem:
      try:
        return bool(await _one(event))
      except Exception:
        logger.exception('[ChangeNotice] 报文字段抽取异常 msg_time=%s',
                         event.get('msg_time'))
        return False

  results = await asyncio.gather(*(_run(e) for e in targets))
  return sum(1 for ok in results if ok)

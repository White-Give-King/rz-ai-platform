"""现场负责人职级查询（综合管理平台人员档案，fail-open）。

变更报文只有负责人姓名，职级只在综合管理平台人员档案里，所以出表链路补一次查询。

**两个接口、各管一件事**（2026-11-20 实测）：
- `GET /user/info/v2/{name}`：按姓名模糊查人，只用来拿 `uid` / `deptId` / `ownDeptId`。
  它**没有**职级字段；那个 `professionLevel`（1/2/3）实测与职级不一致
  （崔海朋 3 对「中级」、张作霖 1 对「中级」），**不能用**。
- `GET /user/dept/v4/page`：`deptId` 必填、`name` 可选模糊过滤，`users[].level`
  才是职级（实测「初级 / 中级 / 无」）。

约束：接口失败/超时/歧义一律留空，**绝不影响出表**；候选人列表按姓名缓存 Redis，
同一姓名跨页面/对话/定时共用。重名且等级冲突时用园区名匹配候选部门名，仍分不出就不猜。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from urllib.parse import quote

from config.redis_conn import redis_manager
from config.settings import settings
from service.scheduled_task.change_notice import parse

logger = logging.getLogger(__name__)

_CACHE_PREFIX = 'change:level:'
_EMPTY_TTL = 86400           # 查无此人只缓存 1 天：档案补录后能自愈
_LOOKUP_TIMEOUT = 8          # 单人查询超时（秒）
_TOTAL_TIMEOUT = 20          # 整批并发查询超时

_LEVEL_NUMBERS = {'1': '初级', '2': '中级', '3': '高级'}


def extract_names(contact: str) -> list[str]:
  """负责人字段 → 姓名列表（复用 `parse.contact_names`，剥号码/标签前缀、保序去重）。"""
  return parse.contact_names(contact)


def decode_level(raw) -> str:
  """档案等级 → 展示值：`无`/空 → ''；`初级/中级/高级` 原样；数字 1/2/3 映射；其余原样。"""
  text = str(raw).strip() if raw is not None else ''
  if text in ('', '无'):
    return ''
  if text in _LEVEL_NUMBERS:
    return _LEVEL_NUMBERS[text]
  return text


def _cache_seed(name: str) -> str:
  return _CACHE_PREFIX + hashlib.sha1(name.encode('utf-8')).hexdigest()


def _cache_read(name: str) -> list[dict] | None:
  try:
    raw = redis_manager.client.get(redis_manager.key(_cache_seed(name)))
  except Exception:
    return None
  if not raw:
    return None
  try:
    data = json.loads(raw)
  except (TypeError, ValueError):
    return None
  return data if isinstance(data, list) else None


def _cache_write(name: str, candidates: list[dict]) -> None:
  """只缓存完整查到的结果；任一步调用失败不写，下一轮重试。"""
  ttl = int(settings.CHANGE_NOTICE_CONTACT_LEVEL_TTL) if candidates else _EMPTY_TTL
  try:
    redis_manager.client.set(redis_manager.key(_cache_seed(name)),
                             json.dumps(candidates, ensure_ascii=False), ex=ttl)
  except Exception as e:
    logger.debug('[ChangeNotice] 负责人等级缓存写入失败 name=%s: %s', name, e)


async def _dept_user(http, dept_id: str, uid: str, name: str,
                     user_id: str | None) -> dict | None:
  """在部门人员页里定位同一人（先按 uid，再按姓名）。"""
  data = await http.request(
    'GET', '/user/dept/v4/page',
    params={'deptId': dept_id, 'name': name, 'page': 1, 'pageCount': 20},
    user_id=user_id,
  )
  users = data.get('users') if isinstance(data, dict) else None
  users = [u for u in (users or []) if isinstance(u, dict)]
  if uid:
    for user in users:
      if str(user.get('uid') or '') == uid:
        return user
  for user in users:
    if str(user.get('name') or '').strip() == name:
      return user
  return None


async def _candidate(http, user: dict, name: str, user_id: str | None) -> dict | None:
  """单个精确候选 → 补上职级；接口异常返回 None（调用方据此不写缓存）。"""
  uid = str(user.get('uid') or '')
  dept_id = str(user.get('deptId') or user.get('ownDeptId') or '')
  found = None
  if dept_id:
    found = await _dept_user(http, dept_id, uid, name, user_id)
  own_dept_id = str(user.get('ownDeptId') or '')
  if found is None and own_dept_id and own_dept_id != dept_id:
    found = await _dept_user(http, own_dept_id, uid, name, user_id)

  dept_name = ''
  if found:
    dept_name = str(found.get('deptName') or found.get('ownDeptName') or '')
  if not dept_name:
    dept = user.get('dept') if isinstance(user.get('dept'), dict) else {}
    dept_name = str(dept.get('deptName') or user.get('ownDeptName') or user.get('garden') or '')

  if found is None or 'level' not in found:
    return {'uid': uid, 'name': name, 'dept_name': dept_name,
            'level': '', 'level_known': False}
  return {'uid': uid, 'name': name, 'dept_name': dept_name,
          'level': decode_level(found.get('level')), 'level_known': True}


async def lookup(name: str, user_id: str | None = None) -> list[dict]:
  """姓名 → 候选人列表 `[{uid,name,dept_name,level,level_known}]`（带缓存）。

  `/user/info/v2/{name}` 是模糊匹配，只保留 **姓名精确相等** 的候选。
  """
  if not name:
    return []
  cached = _cache_read(name)
  if cached is not None:
    return cached

  from service.query.mgmt import http

  try:
    found = await http.request('GET', f'/user/info/v2/{quote(name, safe="")}',
                               user_id=user_id)
  except Exception as e:
    logger.warning('[ChangeNotice] 按姓名查人失败 name=%s: %s', name, str(e)[:200])
    return []

  users = found if isinstance(found, list) else []
  exact = [u for u in users if isinstance(u, dict)
           and str(u.get('name') or '').strip() == name]
  candidates: list[dict] = []
  failed = False
  for user in exact:
    try:
      candidates.append(await _candidate(http, user, name, user_id))
    except Exception as e:
      logger.warning('[ChangeNotice] 查负责人职级失败 name=%s uid=%s: %s',
                     name, user.get('uid'), str(e)[:200])
      failed = True
  if not failed:
    _cache_write(name, candidates)
  return candidates


def resolve(candidates: list[dict], park: str = '') -> tuple[str, str]:
  """候选人 → `(等级, 状态)`；状态 `ok` / `none`（档案确无等级）/ `unknown`（查不出或歧义）。"""
  if not candidates:
    return '', 'unknown'

  known: dict[str, list[dict]] = {}
  for cand in candidates:
    level = cand.get('level') or ''
    if level:
      known.setdefault(level, []).append(cand)

  if len(known) == 1:
    return next(iter(known)), 'ok'
  if len(known) > 1:
    if park:
      hits = {level for level, items in known.items()
              if any(park in (c.get('dept_name') or '') for c in items)}
      if len(hits) == 1:
        return next(iter(hits)), 'ok'
    return '', 'unknown'
  if all(c.get('level_known') for c in candidates):
    return '', 'none'
  return '', 'unknown'


async def _default_resolver(name: str, park: str) -> tuple[str, str]:
  return resolve(await lookup(name), park)


async def attach_levels(events: list[dict], *, resolver=None) -> None:
  """给每条事件写 `contact_level`。任何异常都吞掉（列留空），不影响出表。

  `resolver` 可注入（测试用）：`async (name, park) -> (level, status)`。
  """
  if not events:
    return
  if not settings.CHANGE_NOTICE_CONTACT_LEVEL_ENABLED or not settings.MGMT_ENABLED:
    return

  pairs: list[tuple[str, str]] = []
  for ev in events:
    park = ev.get('park', '')
    for name in extract_names(ev.get('contact', '')):
      pairs.append((name, park))
  unique = list(dict.fromkeys(pairs))
  if not unique:
    for ev in events:
      ev['contact_level'] = ''
    return

  resolve_fn = resolver or _default_resolver

  async def one(name: str, park: str) -> tuple[str, str]:
    try:
      return await asyncio.wait_for(resolve_fn(name, park), timeout=_LOOKUP_TIMEOUT)
    except Exception as e:
      logger.warning('[ChangeNotice] 负责人等级查询失败 name=%s: %s', name, str(e)[:200])
      return '', 'unknown'

  try:
    results = await asyncio.wait_for(
      asyncio.gather(*(one(n, p) for n, p in unique), return_exceptions=True),
      timeout=_TOTAL_TIMEOUT,
    )
  except asyncio.TimeoutError:
    logger.warning('[ChangeNotice] 负责人等级查询整体超时（%ss），该列留空', _TOTAL_TIMEOUT)
    results = [('', 'unknown')] * len(unique)

  outcome: dict[tuple[str, str], tuple[str, str]] = {}
  for pair, result in zip(unique, results):
    outcome[pair] = result if isinstance(result, tuple) else ('', 'unknown')

  for ev in events:
    park = ev.get('park', '')
    levels: list[str] = []
    statuses: list[str] = []
    for name in extract_names(ev.get('contact', '')):
      level, status = outcome.get((name, park), ('', 'unknown'))
      if level and level not in levels:
        levels.append(level)
      statuses.append(status)
    if levels:
      ev['contact_level'] = '、'.join(levels)
    elif 'none' in statuses:
      ev['contact_level'] = '无评级'
    else:
      ev['contact_level'] = ''

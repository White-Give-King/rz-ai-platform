"""文档更新检查的同步租约（Redis `doc_update:sync`）。

锁值是 JSON `{token, mode, user_id, started_at, done, total, current, phase}`：
- `phase` 区分「遍历目录」（`walk`，整部门几十秒到几分钟、`done` 不动）与「逐项判定」
  （`items`）—— 前端据此渲染不确定性进度条，避免长时间停在 0%；
- TTL 120s，后台心跳每 30s 续期 → 进程被 kill 后 **≤2 分钟**锁自动释放，
  页面自动恢复「立即检查」（旧实现 TTL 1800s 且 `finally` 不执行时会一直显示「检查中」）；
- 续期/删除用 Lua 比对 token，防被 kill 的旧任务在恢复后覆盖新租约；
- 旧格式（纯 user_id 字符串）在 `read()` 里容错成 `{mode: 'legacy'}`。

**终止**：`request_cancel()` 把「目标租约的 token」写进 `doc_update:cancel`，
`Lease.update()` 每次进度回调都比对一次，命中即抛 `SyncCancelled`（终止不落半份快照）。
旗标放 Redis 而不是内存：生产是多 worker，取消请求可能落在另一个进程，拿不到本进程的
`asyncio.Task`。token 比对保证旧旗标不会误杀新一轮；`release()` 按 token 清掉旗标。
"""

import asyncio
import json
import logging
import time
import uuid

from config.redis_conn import redis_manager

logger = logging.getLogger(__name__)

LOCK_KEY = 'doc_update:sync'
CANCEL_KEY = 'doc_update:cancel'
LEASE_TTL = 120
HEARTBEAT_SECONDS = 30
# 取消旗标的兜底寿命：正常由 `release()` 按 token 清掉，这里只防极端情况下残留
CANCEL_TTL = 600
# 进度更新节流：进度回调每项都会调一次，没必要每次都往返 Redis
UPDATE_THROTTLE = 1.0

_RENEW_LUA = """
local v = redis.call('GET', KEYS[1])
if not v then return 0 end
local ok, cur = pcall(cjson.decode, v)
if not ok or type(cur) ~= 'table' or cur['token'] ~= ARGV[1] then return 0 end
redis.call('SET', KEYS[1], ARGV[2], 'EX', ARGV[3])
return 1
"""

_RELEASE_LUA = """
local v = redis.call('GET', KEYS[1])
if not v then return 0 end
local ok, cur = pcall(cjson.decode, v)
if not ok or type(cur) ~= 'table' or cur['token'] ~= ARGV[1] then return 0 end
redis.call('DEL', KEYS[1])
return 1
"""


def _redis_key() -> str:
  return redis_manager.key(LOCK_KEY)


def _cancel_key() -> str:
  return redis_manager.key(CANCEL_KEY)


def _as_text(value) -> str:
  if isinstance(value, bytes):
    return value.decode('utf-8', 'replace')
  return '' if value is None else str(value)


class SyncCancelled(BaseException):
  """本次同步被用户终止（正常流程之外的控制流，**不是失败**）。

  **故意继承 `BaseException`**（不是 `Exception`）：`share` / `fetch` 里多处
  `except Exception` 是「单个目录/文件失败不拖垮整轮」的兜底，若继承 Exception，
  取消会被吞成部门 `error` 继续跑（一个部门要遍历 8~10 分钟）。继承 BaseException
  后这些兜底自然放行。代价是各调用方必须显式 `except SyncCancelled` 记日志，否则
  异步任务会留下「Task exception was never retrieved」噪音。
  """


class Lease:
  """一次同步的执行权：带心跳、进度与 token 保护的租约。"""

  def __init__(self, token: str, mode: str, user_id: str):
    self.token = token
    self.mode = mode
    self.user_id = user_id
    self.started_at = time.time()
    self.done = 0
    self.total = 0
    self.current = ''
    self.phase = ''
    self._last_update = 0.0
    self._task: asyncio.Task | None = None
    self.cancelled = False

  def _payload(self) -> str:
    return json.dumps({
      'token': self.token,
      'mode': self.mode,
      'user_id': self.user_id,
      'started_at': round(self.started_at, 3),
      'done': self.done,
      'total': self.total,
      'current': self.current,
      'phase': self.phase,
    }, ensure_ascii=False)

  def update(self, done: int, total: int, current: str = '',
             phase: str = '') -> None:
    """进度 + 续期（节流 1s）。可作为 `perform_sync(progress=…)` 直接传入。

    `phase`：`walk`＝正在并发遍历共享目录（`done` 暂不推进）、`items`＝逐项判定。
    每次回调先看有没有人点了「终止」：命中即抛 `SyncCancelled`（见 `request_cancel`）。
    """
    self._check_cancel()
    self.done = int(done or 0)
    self.total = int(total or 0)
    self.current = current or ''
    self.phase = phase or ''
    now = time.monotonic()
    if now - self._last_update < UPDATE_THROTTLE:
      return
    self._last_update = now
    self._renew()

  def _check_cancel(self) -> None:
    """取消旗标是否指向本次租约；是则抛 `SyncCancelled`。Redis 抖动不取消。"""
    try:
      flag = redis_manager.client.get(_cancel_key())
    except Exception as e:  # noqa: BLE001
      logger.debug('文档更新检查读取取消旗标失败: %s', e)
      return
    if _as_text(flag) == self.token:
      self.cancelled = True
      raise SyncCancelled('文档更新检查已被用户终止')

  def _renew(self) -> None:
    try:
      redis_manager.client.eval(_RENEW_LUA, 1, _redis_key(),
                                self.token, self._payload(), LEASE_TTL)
    except Exception as e:  # noqa: BLE001  Redis 抖动只丢一次续期，下次心跳补
      logger.debug('文档更新检查租约续期失败: %s', e)

  def _start_heartbeat(self) -> None:
    try:
      loop = asyncio.get_running_loop()
    except RuntimeError:
      return
    self._task = loop.create_task(self._beat())

  async def _beat(self) -> None:
    while True:
      await asyncio.sleep(HEARTBEAT_SECONDS)
      self._renew()

  def release(self) -> None:
    """取消心跳、按 token 删除锁与取消旗标（被 kill 的旧任务不会删掉新租约）。"""
    if self._task is not None:
      self._task.cancel()
      self._task = None
    try:
      redis_manager.client.eval(_RELEASE_LUA, 1, _redis_key(), self.token)
    except Exception as e:  # noqa: BLE001
      logger.debug('文档更新检查租约释放失败: %s', e)
    self._clear_cancel()

  def _clear_cancel(self) -> None:
    """只清指向本次租约的取消旗标（别人的不动）。"""
    try:
      if _as_text(redis_manager.client.get(_cancel_key())) == self.token:
        redis_manager.client.delete(_cancel_key())
    except Exception as e:  # noqa: BLE001
      logger.debug('文档更新检查取消旗标清理失败: %s', e)


def acquire(mode: str = 'manual', user_id: str = '') -> 'Lease | None':
  """抢锁；已有租约返回 None。Redis 不可用时 fail-open（与旧行为一致）。"""
  lease = Lease(uuid.uuid4().hex, mode, user_id)
  try:
    ok = bool(redis_manager.client.set(_redis_key(), lease._payload(),
                                       nx=True, ex=LEASE_TTL))
  except Exception as e:  # noqa: BLE001
    logger.warning('文档更新检查抢锁失败（Redis 不可用，按未上锁继续）: %s', e)
    return lease
  if not ok:
    return None
  lease._start_heartbeat()
  return lease


def request_cancel() -> bool:
  """请求终止当前租约：把它的 token 写进取消旗标。没有在跑的检查返回 False。"""
  raw = read()
  token = ''
  if raw:
    try:
      data = json.loads(redis_manager.client.get(_redis_key()) or '')
      if isinstance(data, dict):
        token = str(data.get('token') or '')
    except (ValueError, TypeError, Exception):  # noqa: BLE001  旧格式/Redis 异常都按不可取消
      token = ''
  if not token:
    return False
  try:
    redis_manager.client.set(_cancel_key(), token, ex=CANCEL_TTL)
  except Exception as e:  # noqa: BLE001
    logger.warning('文档更新检查取消旗标写入失败: %s', e)
    return False
  return True


def _cancel_pending(token: str) -> bool:
  """取消旗标是否指向该租约（token 比对，旧旗标不算）。"""
  if not token:
    return False
  try:
    return _as_text(redis_manager.client.get(_cancel_key())) == token
  except Exception:
    return False


def read() -> dict | None:
  """当前租约（含进度与是否正在终止）；无锁返回 None，旧格式容错成 `{mode: 'legacy'}`。"""
  try:
    raw = redis_manager.client.get(_redis_key())
  except Exception:
    return None
  if not raw:
    return None
  try:
    data = json.loads(raw)
  except (ValueError, TypeError):
    data = None
  if not isinstance(data, dict):
    return {
      'mode': 'legacy', 'user_id': str(raw), 'started_at': None,
      'done': 0, 'total': 0, 'current': '', 'phase': '', 'cancelling': False,
    }
  return {
    'mode': data.get('mode') or 'manual',
    'user_id': data.get('user_id') or '',
    'started_at': data.get('started_at'),
    'done': int(data.get('done') or 0),
    'total': int(data.get('total') or 0),
    'current': data.get('current') or '',
    'phase': data.get('phase') or '',
    'cancelling': _cancel_pending(str(data.get('token') or '')),
  }

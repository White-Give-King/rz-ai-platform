"""未找到项的模型复核：在已解析出的查找范围里，从真实文件清单中挑出该项的文件。

确定性匹配（编号 / 登记别名 / 独占目录名）优先，本模块只在 `matched` 为空时兜底。
同一目录下常同时存放多条清单项的登记表（A2 的记录表目录就是一个钉点挂 5 项），
**逐项单独问模型会互相串**（实测：「客户IT库房应急卡」把「应急卡使用登记表」算进来），
故 `recheck_many` 把同一范围里的未找到项**合并成一次调用**按编号分派。

判据是**同一主题即可**，不要求文件名逐字相同（「客户IT库房应急卡」↔
「字节IT库房应急卡领取登记记录表」，「登记表」写成「记录表」也照算）。
模型**只能在给定候选清单里挑**：名字必须与候选一致（归一空白后），否则丢弃；
拿不准的放 `uncertain`，下游判「待复核」，**不当已命中**（不改期号/哈希判定）。
同一个文件名被两条清单项同时选中（任一路）→ 判不清，谁都不算。

缓存按版本 + 提示词 + 输入哈希（Redis 30 天）：改 `_SYSTEM`/结构或候选集变化 ⇒ 键变化，
不会拿旧结论挡住新文件或新逻辑。调用/解析失败不缓存，下一轮重试。异常一律不外抛。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re

from config.redis_conn import redis_manager
from config.settings import settings

logger = logging.getLogger(__name__)

_CACHE_PREFIX = 'doc_update:match:'
_CACHE_TTL = 30 * 86400
_MAX_TOKENS = 2048
# 提示词/输出结构或比对逻辑变了就 +1：缓存键随之变化，旧键不删、30 天自然过期
_CACHE_VERSION = 2

_SYSTEM = (
  '你是运维共享盘文件的分派器。同一目录下可能同时存放多条清单项的登记表——'
  '「应急卡使用登记表」「客户IT库房应急卡领取记录表」「钥匙使用登记表」是不同的表，'
  '不要互相算进来。\n'
  '给定「清单项清单」（编号/名称/说明/关键词）和该目录下的真实文件名清单'
  '（同名文件在多个月份子目录重复出现时只列一次），把文件名分派给属于它的那一条清单项。'
  '业务叫法与文件名往往不同：只要**同一主题**就算，**不要求名字逐字相同**'
  '（例：清单写「客户IT库房应急卡领取记录表」，真实文件叫「字节IT库房应急卡领取登记记录表」'
  '——「字节」是客户方名称；「登记表」写成「记录表」也照算）。'
  '但不要张冠李戴：同一目录里别的登记表（如值班室 vs 中控室的钥匙表）属于别的清单项。\n'
  '规则：① 明确属于某条清单项 → 放进 assignments；有疑问、拿不准是不是它 → 放进 '
  'uncertain；② 每条清单项可以一个文件都不分配；③ 一个文件名最多分配给一条清单项、'
  '且只能出现在 assignments/uncertain 其中一处，分不清就都不分配；'
  '④ 「本月无…使用记录」这类说明文件归给对应条目。\n'
  '输出 JSON：'
  '{"assignments": {"<清单项编号>": ["<文件名原文>"]}, '
  '"uncertain": {"<清单项编号>": ["<文件名原文>"]}, '
  '"reasons": {"<清单项编号>": "<一句依据>"}}。\n'
  '文件名必须**逐字来自给定文件清单**，不要编造、不要改写文件名；不要重复输出同一个名字。'
)
_SYSTEM_HASH = hashlib.sha1(_SYSTEM.encode('utf-8')).hexdigest()[:12]

_FENCE_RE = re.compile(r'^```[a-zA-Z]*\s*|\s*```$')
_JSON_RE = re.compile(r'\{[\s\S]*\}')


def _norm(text) -> str:
  return str(text or '').replace('\u3000', ' ').strip()


_WS_RE = re.compile(r'\s+')


def _key(text) -> str:
  """比对键：**去掉全部空白**再比。模型会把中文名里的拉丁/数字前后插空格
  （实测「字节IT库房…」→「字节 IT 库房…」，「钥匙使用登记表-中控室」→「… - 中控室」），
  逐字比对会把真实文件全丢掉。"""
  return _WS_RE.sub('', _norm(text))


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


def _cache_key(payload: dict) -> str:
  """版本 + 提示词哈希 + 输入一起做种子：改了逻辑/提示词就不会命中旧结论。"""
  seed = json.dumps({'v': _CACHE_VERSION, 'sys': _SYSTEM_HASH, 'payload': payload},
                    sort_keys=True, ensure_ascii=False)
  return redis_manager.key(_CACHE_PREFIX
                           + hashlib.sha1(seed.encode('utf-8')).hexdigest())


def _cache_read(payload: dict) -> dict | None:
  try:
    raw = redis_manager.client.get(_cache_key(payload))
  except Exception:
    return None          # Redis 不可用 → fail-open，照常调模型
  if not raw:
    return None
  try:
    data = json.loads(raw)
  except (TypeError, ValueError):
    return None
  return data if isinstance(data, dict) else None


def _cache_write(payload: dict, result: dict) -> None:
  """只缓存**正常返回**（含「一个都没分派」）；调用失败不写，下一轮再试。"""
  try:
    redis_manager.client.set(_cache_key(payload),
                             json.dumps(result, ensure_ascii=False),
                             ex=_CACHE_TTL)
  except Exception as exc:
    logger.debug('[DocUpdate] 匹配复核缓存写入失败：%s', exc)


async def _chat(messages, **kwargs) -> str:
  """模型调用出口（模块级，测试可 monkeypatch，不连网）。"""
  from core.model_gateway import model_gateway

  return await model_gateway.chat(messages, **kwargs)


def _item_brief(item) -> dict:
  return {
    'number': item.number_label,
    'name': item.name,
    'note': item.note or '',
    'keywords': list(item.keywords or ()),
    'folders': list(item.folders or ()),
  }


def _payload(items: list, candidates: list[dict]) -> dict:
  """候选取**去重后的文件名**：同名文件散在 12 个月份目录里，带路径会把输入撑大 10 倍
  （实测 103 个文件 34k tokens），而分派只需名字，期号由路径在本地判。"""
  names: list[str] = []
  for rec in candidates:
    name = _norm(rec.get('topic'))
    if name and name not in names:
      names.append(name)
  return {
    'items': [_item_brief(item) for item in items],
    'file_names': names,
  }


def _entries(data: dict, channel: str) -> list[dict] | None:
  """解析一个通道（`assignments` / `uncertain`）；结构不认识返回 None。

  两种结构都认：`{编号: [名字]}` 对象（首选，输出短）与 `[{number, files}]` 数组。
  通道缺失（`raw is None`）返回 `[]`——两条都缺才算「结构不合预期」。
  """
  raw = data.get(channel)
  reasons = data.get('reasons') if isinstance(data.get('reasons'), dict) else {}
  if raw is None:
    return []
  if isinstance(raw, dict):
    return [{'number': number, 'files': files,
             'reason': reasons.get(number, '')} for number, files in raw.items()]
  if isinstance(raw, list):
    return [entry for entry in raw if isinstance(entry, dict)]
  return None


async def _ask(payload: dict, keys: set[str]) -> dict | None:
  """调用模型并做结构/取值校验；失败返回 None（不缓存，下一轮重试）。

  返回 `{'entries': [{'number','files','review','reason'}]}`；`files`/`review` 已是
  **去掉空白的比对键**，下游按 `_key(候选文件名)` 映射回记录。同一名字同时出现在
  两条通道（同一编号）时只算 `files`（明确归属优先于拿不准）。
  """
  messages = [{'role': 'system', 'content': _SYSTEM},
              {'role': 'user', 'content': json.dumps(payload, ensure_ascii=False)}]
  try:
    raw = await _chat(
      messages, temperature=0, max_tokens=_MAX_TOKENS, json_mode=True,
      timeout=float(settings.DOC_UPDATE_MATCH_LLM_TIMEOUT),
      caller='doc_update_match')
  except Exception as exc:
    logger.warning('[DocUpdate] 匹配复核调用失败：%s', str(exc)[:200])
    return None
  data = _loads(raw)
  if 'assignments' not in data and 'uncertain' not in data:
    logger.warning('[DocUpdate] 匹配复核返回结构不合预期：%s', str(raw)[:120])
    return None                  # 两条通道都缺（空对象/旧结构）→ 当失败、下一轮重试
  assigned = _entries(data, 'assignments')
  unsure = _entries(data, 'uncertain')
  if assigned is None or unsure is None:
    logger.warning('[DocUpdate] 匹配复核返回结构不合预期：%s', str(raw)[:120])
    return None
  reasons = data.get('reasons') if isinstance(data.get('reasons'), dict) else {}
  by_number: dict[str, dict] = {}
  for field, entries in (('files', assigned), ('review', unsure)):
    for entry in entries:
      number = _norm(entry.get('number'))
      names = entry.get('files')
      if not number or not isinstance(names, list):
        continue
      picked = by_number.setdefault(number, {'files': [], 'review': [], 'reason': ''})
      for name in names:
        name = _key(name)
        if name in keys and name not in picked['files'] and name not in picked['review']:
          picked[field].append(name)
      reason = _norm(entry.get('reason') or reasons.get(number))
      if not picked['reason'] and reason:
        picked['reason'] = reason[:300]
  entries = [{'number': number, **picked} for number, picked in by_number.items()]
  return {'entries': entries}


async def recheck_many(pairs: list[tuple]) -> dict[int, dict]:
  """把同一范围内的多条未找到项**一次调用**分派。

  `pairs` 是 `[(DocItem, 候选文件列表)]`；返回 `{seq: {files, review_files, reason}}`。
  同一文件名被两条清单项同时选中（`assignments`/`uncertain` 任一路）→ 判不清，谁都不算。
  """
  empty = {item.seq: {'files': [], 'review_files': [], 'reason': ''} for item, _ in pairs}
  if not settings.DOC_UPDATE_MATCH_LLM_ENABLED:
    return empty
  usable = [(item, cands) for item, cands in pairs if cands]
  if not usable:
    return empty
  names = {_key(f.get('topic')) for _, cands in usable for f in cands}
  if not names:
    return empty
  payload = _payload([item for item, _ in usable],
                     [f for _, cands in usable for f in cands])
  result = _cache_read(payload)
  if result is None:
    result = await _ask(payload, names)
    if result is None:           # 调用或解析失败：不缓存，留给下一轮
      return empty
    _cache_write(payload, result)

  by_number = {item.number_label: item for item, _ in usable}
  reasons: dict[str, str] = {}
  assigned: dict[str, set[str]] = {}
  unsure: dict[str, set[str]] = {}
  for entry in result.get('entries') or []:
    number = _norm(entry.get('number'))
    if number not in by_number:
      continue                   # 编出来的编号（不在本次询问的条目里）
    reasons.setdefault(number, _norm(entry.get('reason')))
    for field, target in (('files', assigned), ('review', unsure)):
      for name in entry.get(field) or []:
        name = _key(name)
        if name in names:
          target.setdefault(name, set()).add(number)

  picked_files: dict[int, set[str]] = {item.seq: set() for item, _ in usable}
  picked_review: dict[int, set[str]] = {item.seq: set() for item, _ in usable}
  for name in set(assigned) | set(unsure):
    numbers = assigned.get(name, set()) | unsure.get(name, set())
    if len(numbers) > 1:
      logger.info('[DocUpdate] 模型复核歧义（%s 被 %s 同时选中），丢弃', name,
                  '/'.join(sorted(numbers)))
      continue
    number = next(iter(numbers))
    seq = by_number[number].seq
    if number in assigned.get(name, set()):
      picked_files[seq].add(name)     # 明确归属优先于拿不准
    else:
      picked_review[seq].add(name)

  seq_of = {item.seq: item for item, _ in usable}
  out = {item.seq: {'files': [], 'review_files': [],
                    'reason': reasons.get(item.number_label, '')}
         for item, _ in usable}
  for item, cands in usable:
    # 同名文件在多个月份目录各有一条 → 一条名字回填该范围内的**全部**同名记录
    # （期号由各自路径判定，只取第一条会漏掉最新月份）
    files = picked_files[item.seq]
    if files:
      out[item.seq]['files'] = [f for f in cands if _key(f.get('topic')) in files]
    review = picked_review[item.seq]
    if review:
      out[item.seq]['review_files'] = [f for f in cands if _key(f.get('topic')) in review]
  for seq, res in out.items():
    item = seq_of[seq]
    if res['files']:
      logger.info('[DocUpdate] 模型复核命中 %s %s：%d 个文件（%s）',
                  item.number_label, item.name, len(res['files']),
                  res['reason'][:80])
    elif res['review_files']:
      logger.info('[DocUpdate] 模型复核存疑 %s %s：%d 个疑似文件（%s）',
                  item.number_label, item.name, len(res['review_files']),
                  res['reason'][:80])
  return out


async def recheck(item, candidates: list[dict]) -> dict:
  """单项复核（兼容入口）。返回 `{files, review_files, reason}`；异常不抛出。"""
  result = await recheck_many([(item, candidates)])
  return result.get(item.seq, {'files': [], 'review_files': [], 'reason': ''})

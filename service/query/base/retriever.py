"""能力语义检索 —— 海量能力收敛的关键（平台无关，跨平台统一索引）。

Agent 不直接面对全部能力，而是先用一句查询语义检索出最相关的 top-k 能力，
再让 LLM 从少数候选中选择。借鉴 langgraph-bigtool 的 retrieve_tools 模式与
`query -> list[id]` 极简签名，检索复用 core.model_gateway.embedding，不引入
LangGraph Store 第二套 embedding。

逻辑本就平台无关：索引源是 base.platform 的跨平台聚合（all_capabilities）。能力 id 全局
唯一（带平台前缀），单一索引天然隔离多平台，无需分平台建库。
"""

import hashlib
import json
import logging
import math

from service.query.base.platform import all_capabilities

logger = logging.getLogger(__name__)

# 能力向量缓存：capability_id -> embedding 向量
_CACHE: dict[str, list[float]] = {}
_CACHE_IDS: list[str] = []
# 桶索引：capability_id -> (platform, category, domain)，供分类/域维度先过滤再余弦（P-4）
_CACHE_META: dict[str, tuple[str, str, str]] = {}
# 通用条目向量缓存：entries 指纹 -> {key: 向量}（供内置工具检索复用同一模型与余弦）
_ENTRY_CACHE: dict[str, dict[str, list[float]]] = {}


def _cosine(a: list[float], b: list[float]) -> float:
  dot = sum(x * y for x, y in zip(a, b))
  na = math.sqrt(sum(x * x for x in a))
  nb = math.sqrt(sum(y * y for y in b))
  if na == 0 or nb == 0:
    return 0.0
  return dot / (na * nb)


def _index_text(cap) -> str:
  """构建用于检索的文本：名称 + 描述，名称加权在前。"""
  return f'{cap.name}。{cap.description}'


async def ensure_index() -> None:
  """确保能力向量索引已构建（惰性，首次检索时触发）。

  假设平台在启动阶段完成注册，首次检索时能力集已就绪。
  """
  if _CACHE:
    return
  from core.model_gateway import model_gateway

  caps = all_capabilities()
  if not caps:
    logger.warning('retriever: 无已注册能力，索引为空')
    return
  texts = [_index_text(c) for c in caps]
  vectors = await model_gateway.embedding(texts)
  for cap, vec in zip(caps, vectors):
    _CACHE[cap.id] = vec
    _CACHE_IDS.append(cap.id)
    _CACHE_META[cap.id] = (cap.id.split('.', 1)[0], cap.category, cap.domain)
  logger.info('retriever 索引构建完成 count=%d', len(_CACHE))


def _match_filter(cid: str, category: str | None, domain: str | None) -> bool:
  """按桶索引过滤：category/domain 与目标不一致的能力排除在外。"""
  _, cat, dom = _CACHE_META.get(cid, ('', '', ''))
  if category is not None and cat != category:
    return False
  if domain is not None and dom != domain:
    return False
  return True


async def search_capabilities_scored(
  query: str,
  k: int = 8,
  *,
  category: str | None = None,
  domain: str | None = None,
) -> list[tuple[str, float]]:
  """按查询语义检索最相关的能力 id，返回 (id, 余弦分) 降序。

  Args:
    query: 用户查询或意图描述。
    k: 返回条数上限。
    category: 仅检索该 L2 分类下的能力（如 'alarm'）；None 检索全量。
    domain: 仅检索该 L3 域下的能力（如 'device'）；None 不按域过滤。

  Returns:
    (capability_id, score) 列表；能力总数不足 k 时返回全部。
  """
  await ensure_index()
  ids = _CACHE_IDS
  if category is not None or domain is not None:
    # 先按分类/域过滤再余弦：提升跨域准确率，向量数（同分类）远小于全量，计算更省
    ids = [cid for cid in ids if _match_filter(cid, category, domain)]
  if not ids:
    return []
  from core.model_gateway import model_gateway

  q_vec = (await model_gateway.embedding([query]))[0]
  scored = [(cid, _cosine(q_vec, _CACHE[cid])) for cid in ids]
  scored.sort(key=lambda x: x[1], reverse=True)
  return scored[:k]


async def search_capabilities(
  query: str,
  k: int = 8,
  *,
  category: str | None = None,
  domain: str | None = None,
) -> list[str]:
  """按查询语义检索最相关的能力 id（薄封装，见 search_capabilities_scored）。"""
  scored = await search_capabilities_scored(query, k, category=category, domain=domain)
  return [cid for cid, _ in scored]


async def _entry_vectors(entries: dict[str, str]) -> dict[str, list[float]]:
  """构建（或复用缓存的）条目向量表：{key: 检索文本} → {key: 向量}。"""
  from core.model_gateway import model_gateway

  fingerprint = hashlib.sha1(
    json.dumps(sorted(entries.items()), ensure_ascii=False).encode('utf-8')
  ).hexdigest()
  cached = _ENTRY_CACHE.get(fingerprint)
  if cached is not None:
    return cached
  keys = list(entries)
  vectors = await model_gateway.embedding([entries[k] for k in keys])
  built = dict(zip(keys, vectors))
  _ENTRY_CACHE[fingerprint] = built
  return built


async def search_entries_scored(
  query: str,
  entries: dict[str, str],
  k: int = 8,
) -> list[tuple[str, float]]:
  """对任意 `{key: 检索文本}` 做语义检索，返回 (key, 余弦分) 降序。

  与能力检索共用同一个 embedding 模型与 `_cosine`，分数可直接比较合并。索引不用
  `ensure_index()`（那是能力专用）；向量表按 entries 指纹缓存，entries 为空或异常
  一律返回 []（fail-soft，等同「没搜到」）。
  """
  if not entries:
    return []
  try:
    from core.model_gateway import model_gateway

    vectors = await _entry_vectors(entries)
    q_vec = (await model_gateway.embedding([query]))[0]
    scored = [(key, _cosine(q_vec, vec)) for key, vec in vectors.items()]
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[:k]
  except Exception as e:
    logger.warning('search_entries_scored 失败（按未命中处理）: %s', e)
    return []


def reset_index() -> None:
  """清空索引缓存（平台变更或测试时用）。"""
  _CACHE.clear()
  _CACHE_IDS.clear()
  _CACHE_META.clear()
  _ENTRY_CACHE.clear()

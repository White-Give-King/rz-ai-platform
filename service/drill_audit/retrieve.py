"""知识库检索对应的 EOP（应急操作流程）。

检索入口是 `rag_service.hybrid_retrieve(..., intent_filter={'doc_type': 'EOP'})`：
单 Milvus collection，按 metadata 区分；意图过滤是 **fail-soft**（无命中会退化全库），
所以这里必须再按 `doc_type == 'EOP'` 二次过滤，非 EOP 一律丢弃。
"""

import asyncio
import logging

from config.settings import settings

logger = logging.getLogger(__name__)


class EopRetrievalError(Exception):
  """检索链路本身不可用（如 Milvus 连不上）。

  与「检索成功但没命中」必须分开：2026-11-17 实测 Milvus 停着时这里 return []，
  上游写成「知识库无相关 EOP 文档」，把基础设施故障说成文档缺失。
  """


async def find_eop(query: str) -> list[dict]:
  """返回 `[{file_name, content, score}]`；检索成功但为空返回空列表。

  检索不可用（异常）抛 `EopRetrievalError`，不伪装成「无命中」。
  """
  from service.rag.pipeline.rag_service import rag_service

  group_id = settings.DRILL_AUDIT_EOP_GROUP or 'default'
  try:
    result = await asyncio.to_thread(
      rag_service.hybrid_retrieve, query, group_id,
      intent_filter={'doc_type': 'EOP'},
    )
  except Exception as e:  # noqa: BLE001 - 转成语义明确的异常，由调用方决定怎么报
    logger.warning('[DrillAudit] EOP 检索失败 query=%s: %s', query[:60], str(e)[:200])
    raise EopRetrievalError(f'{type(e).__name__}: {str(e)[:200]}') from e

  docs = result.get('final_docs') or []
  out: list[dict] = []
  for doc in docs:
    meta = doc.metadata or {}
    if str(meta.get('doc_type') or '') != 'EOP':
      continue
    out.append({
      'file_name': str(meta.get('file_name') or meta.get('source') or ''),
      'content': str(doc.page_content or ''),
      'score': meta.get('score'),
    })
    if len(out) >= settings.DRILL_AUDIT_EOP_TOP_K:
      break
  return out

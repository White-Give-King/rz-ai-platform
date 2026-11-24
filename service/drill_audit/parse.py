"""演练 PDF 文本化：本地 MinerU Gradio（默认）或云端 v4 API（显式开 OCR）。

**绝不调 `_load_pdf_uncached`** —— 它的 CLI 兜底在本机实测会挂 600s 返 0 字，
远超 AGENT_EVENT_TIMEOUT=360，对话会被掐断。这里只调
`_load_pdf_mineru_gradio` / `_load_pdf_mineru_api` 并自带超时。

`DRILL_AUDIT_LOCAL_OCR`（默认 true）且配了 `MINERU_GRADIO_URL` 时走本地
vlm-engine（实测 8~10s、扫描件覆盖率最好），异常回退云端。
"""

import asyncio
import logging
import tempfile
from pathlib import Path

from config.settings import settings

logger = logging.getLogger(__name__)


def _extract_sync(data: bytes) -> str:
  from repository.ingestion.loader import (
    _load_pdf_mineru_api,
    _load_pdf_mineru_gradio,
  )

  tmp = tempfile.NamedTemporaryFile(suffix='.pdf', delete=False)
  try:
    tmp.write(data)
    tmp.close()
    docs = None
    if settings.DRILL_AUDIT_LOCAL_OCR and settings.MINERU_GRADIO_URL:
      try:
        docs = _load_pdf_mineru_gradio(tmp.name, is_ocr=True)
      except Exception as e:
        logger.warning('本地 Gradio OCR 失败，回退云端 MinerU: %s', e)
    if docs is None:
      docs = _load_pdf_mineru_api(tmp.name, is_ocr=True, language='ch')
    return '\n'.join(str(d.page_content or '') for d in docs).strip()
  finally:
    Path(tmp.name).unlink(missing_ok=True)


async def pdf_bytes_to_text(data: bytes) -> str:
  """PDF 字节 → 文本；单份受 `DRILL_AUDIT_OCR_TIMEOUT` 约束。失败向上抛。"""
  if not data:
    return ''
  return await asyncio.wait_for(
    asyncio.to_thread(_extract_sync, data),
    timeout=settings.DRILL_AUDIT_OCR_TIMEOUT,
  )

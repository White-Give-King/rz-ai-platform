"""未找到项的目录截图：进入共享文档里该项的真实文件夹截一张图，留作线下核对证据。

确定性匹配一行不动；**只有「未找到」的项**才需要这张图（触发点在
`fetch.take_dept_snapshot` 的项循环之后，补跑历史期不截），同一钉点的多项共用一份。

执行走独立的 browser 旁路容器：`POST /v1/capture` 接**运行期拼的配方**（不经
`recipes.py`、不需要 thread_id），登录态用 `deploy/scripts/browser_login.py` 事先落盘的
storageState。页面路径见 `docs/features/browser-capture.md`：直接深链 `#/shareFile`
左侧栏不渲染，必须先点「运维管理」→ 切部门 → 点「共享文档」→ 逐级点目录。

**整段 fail-soft**：截图只是证据，任何失败只写进 `screenshot.error`，
绝不影响判定与落库。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from config.settings import settings

logger = logging.getLogger(__name__)

_MAX_RENAME_TRIES = 100
_FILE_ROW = 'span.no-select.file-name'


def _css_text(base: str, text: str) -> dict:
  """按文字定位文件行（`:has-text` 是 Playwright 的 CSS 扩展）。"""
  safe = text.replace('\\', '\\\\').replace('"', '\\"')
  return {'by': 'css', 'css': f'{base}:has-text("{safe}")'}


def _steps(dept_name: str, segments: list[str]) -> list[dict]:
  """从系统根页进入目标文件夹的点击序列（层级与共享文档页面一致）。"""
  dept_option = dept_name if dept_name.endswith('分部') else f'{dept_name}分部'
  steps: list[dict] = [
    {'action': 'click', 'selector': {'by': 'text', 'name': '运维管理'}},
    # 左侧栏渲染出来才有部门选择器（深链直开时它不存在）
    {'action': 'wait_for', 'selector': {'by': 'css', 'css': '.company-and-dept-label'},
     'state': 'visible', 'timeout_ms': 20000},
    {'action': 'click', 'selector': {'by': 'css', 'css': '.company-and-dept-label'}},
    {'action': 'click', 'selector': {'by': 'text', 'name': dept_option}},
    {'action': 'click', 'selector': {'by': 'text', 'name': '共享文档'}},
  ]
  steps.extend({'action': 'click', 'selector': _css_text(_FILE_ROW, seg),
                'timeout_ms': 30000}
               for seg in segments if seg)
  steps.append({'action': 'wait_load', 'state': 'networkidle', 'timeout_ms': 15000})
  return steps


def _payload(dept_name: str, segments: list[str]) -> dict:
  return {
    'system': 'mgmt',
    'params': {},
    'full_page': True,
    'recipe': {
      'url': settings.BROWSER_MGMT_URL,
      'wait_until': 'domcontentloaded',
      'steps': _steps(dept_name, segments),
      'ready': {'selector': {'by': 'css', 'css': _FILE_ROW}, 'timeout_ms': 20000},
      'extract': [],
      'login': None,
    },
  }


def _save(image: bytes, company_id: str, dept_name: str, fid: str) -> str:
  """原子落盘（O_EXCL 预留文件名 + 重命名），同名绝不覆盖。返回纯 ASCII 文件名。"""
  directory = Path(settings.DOC_UPDATE_SCREENSHOT_DIR)
  directory.mkdir(parents=True, exist_ok=True)
  seed = hashlib.sha1(f'{company_id}|{dept_name}|{fid}'.encode('utf-8')).hexdigest()[:12]
  stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
  base = f'docupd-{seed}-{stamp}'
  for idx in range(_MAX_RENAME_TRIES):
    name = f'{base}.png' if not idx else f'{base}-{idx}.png'
    path = directory / name
    try:
      fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
      continue
    os.close(fd)
    tmp = directory / f'.{name}.{os.getpid()}.part'
    tmp.write_bytes(image)
    os.replace(tmp, path)
    return name
  raise RuntimeError('截图文件名冲突（同名文件过多）')


async def _capture(dept_name: str, segments: list[str]) -> dict:
  from service.query.browser.client import capture_raw

  return await capture_raw(_payload(dept_name, segments))


async def capture(*, company_id: str, dept_name: str, fid: str,
                  segments: list[str]) -> dict:
  """截一张图并落盘；返回 `{name, error, captured_at}`（**永不抛出**）。"""
  out = {'name': '', 'error': '', 'captured_at': ''}
  if not settings.DOC_UPDATE_SCREENSHOT_ENABLED:
    return out
  if not settings.BROWSER_SERVICE_URL:
    out['error'] = '未配置 BROWSER_SERVICE_URL，跳过截图'
    return out
  if not settings.BROWSER_MGMT_URL:
    out['error'] = '未配置 BROWSER_MGMT_URL，跳过截图'
    return out
  try:
    data = await asyncio.wait_for(
      _capture(dept_name, segments),
      timeout=float(settings.DOC_UPDATE_SCREENSHOT_TIMEOUT),
    )
  except asyncio.TimeoutError:
    out['error'] = f'截图超时（>{settings.DOC_UPDATE_SCREENSHOT_TIMEOUT:g}s）'
    return out
  except Exception as exc:  # noqa: BLE001  截图失败不影响判定
    out['error'] = f'截图失败：{type(exc).__name__}: {str(exc)[:200]}'
    return out
  try:
    image = base64.b64decode(data.get('image_b64') or '')
  except (TypeError, ValueError):
    image = b''
  if not image:
    out['error'] = '截图返回为空'
    return out
  try:
    out['name'] = _save(image, company_id, dept_name, fid)
  except Exception as exc:  # noqa: BLE001
    out['error'] = f'截图落盘失败：{type(exc).__name__}: {str(exc)[:200]}'
    return out
  out['captured_at'] = datetime.now(timezone.utc).isoformat()
  logger.info('[DocUpdate] 截图已保存 %s · %s → %s', dept_name,
              ' / '.join(segments[-1:]) or '-', out['name'])
  return out

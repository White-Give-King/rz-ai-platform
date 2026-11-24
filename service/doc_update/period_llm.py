"""「查新增」项的期号兜底（模型只补确定性解析不出的文件）。

期号先按目录链（任意深度）→ 文件名 两级确定性解析（`period.period_of_file`），
剩下的通常是模板（`_V1.docx`）与年度汇总，解析不出期号属正常。目录只到年份、
月份写在文件名里的形态（`4.1.5` 四条、A6/A10/A11 的混年目录）同样走这条兜底。**只有存在未解析文件时才调一次模型**，且模型输出必须过严格
校验（证据出自目录链或文件名原文、月份可验证、期号不超前、年份一致）才写入，
否则一律丢弃、保持确定性结果 —— 宁可漏补，不可编一个期号。

目录路径一并下发：年份常在目录里（`2026年输出物`）、月份写在文件名里
（`查询记录单（十月）`），裸中文月份靠目录年份锚定才可验证。

缓存：同一批「文件名 + 目录链」+ 应到期只调一次（Redis 30 天），重跑/回补不重复烧钱。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re

from config.redis_conn import redis_manager
from config.settings import settings

logger = logging.getLogger(__name__)

_CACHE_PREFIX = 'doc_update:period:'
_CACHE_TTL = 30 * 86400
_MAX_TOKENS = 1024
_MIN_PERIOD = '2000-01'

_SYSTEM = (
  '你是文档期号解析器。每条给文件名和它所在的目录路径。年月只能取自给出的文本'
  '（文件名或目录路径），禁止按文件用途、上下文或常识推测。\n'
  '年份常在目录里（如 `2026年输出物`），月份常在文件名里（如 `查询记录单（十月）`）；'
  '目录同时给出多个年份时取最后一个。\n'
  '输出 JSON：{"files": [{"file": "<文件名原文>", "period": "YYYY-MM" 或 null,'
  ' "evidence": ["<能证明年月的原文片段>", ...]}]}。\n'
  'evidence 必须是文件名或目录路径里的原文，可给多个片段：年份与月份可以分别来自目录和'
  '文件名（如 ["2026年输出物", "（十月）"]）。\n'
  '认得的写法如 2026年10月、2026年十月、2026.10、2026-10、202610、'
  '10月（另有年份时）、十月（目录或文件名里另有年份时）、Oct-2026。\n'
  '没有可验证年月的一律 period=null：模板（_V1/_AX 等）、年度汇总、与月份无关的文件。'
)

_FENCE_RE = re.compile(r'^```[a-zA-Z]*\s*|\s*```$')
_JSON_RE = re.compile(r'\{[\s\S]*\}')
_PERIOD_RE = re.compile(r'^(\d{4})-(\d{2})$')
# 年份锚点（`20261031` / `202512` / `2025年` 都要能取到 2025/2026，不能只认独立四位）
_YEAR_TOKEN_RES = (
  re.compile(r'(?<!\d)(20\d{2})\d{4}(?!\d)'),               # 20261031
  re.compile(r'(?<!\d)(20\d{2})(?:0[1-9]|1[0-2])(?!\d)'),   # 202512
  re.compile(r'(?<!\d)(20\d{2})(?!\d)'),                    # 2025年 / 2025-10
)


def _norm(text) -> str:
  return str(text or '').replace('\u3000', ' ').strip()


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


_CN_MONTH_VALUES = {
  '一': 1, '二': 2, '三': 3, '四': 4, '五': 5, '六': 6,
  '七': 7, '八': 8, '九': 9, '十': 10, '十一': 11, '十二': 12,
}
_EN_MONTHS = {
  'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'may': 5, 'jun': 6,
  'jul': 7, 'aug': 8, 'sep': 9, 'oct': 10, 'nov': 11, 'dec': 12,
}
# 证据里取月份：(正则, 类型, 捕获组)。含裸中文月份（`（十月）`）——
# 年份由目录提供时它就是有效证据
_MONTH_RES = (
  (re.compile(r'(20\d{2})[-_/\.年]\s*(\d{1,2})'), 'num', 2),
  # 20261031 / 【20261031】：8 位日期先取月份，否则会被下面的 6 位规则漏掉
  (re.compile(r'(?<!\d)(20\d{2})(0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])(?!\d)'), 'num', 2),
  (re.compile(r'(?<!\d)(20\d{2})(0[1-9]|1[0-2])(?!\d)'), 'num', 2),
  (re.compile(r'(?<!\d)(\d{1,2})\s*月(?!度)'), 'num', 1),
  (re.compile(r'(20\d{2})\s*年\s*(十[一二]?|[一二三四五六七八九])\s*月'), 'cn', 2),
  (re.compile(r'(?<![\u4e00-\u9fff\d])(十[一二]?|[一二三四五六七八九])\s*月(?!度)'), 'cn', 1),
  (re.compile(r'(?i)\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b'), 'en', 1),
)


def _years(text: str) -> set[int]:
  out: set[int] = set()
  for regex in _YEAR_TOKEN_RES:
    out.update(int(m.group(1)) for m in regex.finditer(text))
  return out


def _last_year(text: str) -> int | None:
  """文本里最后一个四位年份（与 `period._segments_year` 同口径）；没有返回 `None`。"""
  found: list[tuple[int, int]] = []
  for regex in _YEAR_TOKEN_RES:
    found.extend((m.start(1), int(m.group(1))) for m in regex.finditer(text))
  return max(found)[1] if found else None


def _months(text: str) -> set[int]:
  out: set[int] = set()
  for regex, kind, group in _MONTH_RES:
    for m in regex.finditer(text):
      token = m.group(group)
      if kind == 'cn':
        value = _CN_MONTH_VALUES.get(token, 0)
      elif kind == 'en':
        value = _EN_MONTHS.get(token.lower()[:3], 0)
      else:
        value = int(token) if token.isdigit() else 0
      if 1 <= value <= 12:
        out.add(value)
  return out


def _evidence_parts(evidence) -> list[str]:
  """证据统一成去空白的片段列表（模型可能给字符串，也可能给字符串数组）。"""
  raw = evidence if isinstance(evidence, (list, tuple)) else [evidence]
  return [part for part in (_norm(item) for item in raw) if part]


def _valid_period(period, file_name: str, evidence, expected: str,
                  path: str = '') -> str:
  """模型给的期号 → `'YYYY-MM'`；任何一条不满足就丢弃（返回 `''`）。

  `path` 是文件所在目录链（`' / '` 连接）：年份可以来自这里，但文件名自带年份时
  以文件名为准、目录不能覆盖。证据必须是目录链或文件名的原文，且能证明所声称的月份。
  """
  m = _PERIOD_RE.match(_norm(period))
  if not m:
    return ''
  value = f'{m.group(1)}-{m.group(2)}'
  year, month = int(m.group(1)), int(m.group(2))
  if not 1 <= month <= 12 or not 2000 <= year <= 2100:
    return ''
  # 不允许未来月份：报告期不可能晚于本次检查的应到期
  if expected and not _MIN_PERIOD <= value <= expected:
    return ''
  file_years = _years(file_name)
  if file_years:
    # 文件名自带年份时，期号年份必须与之一致（`2025年度（10月）` 不能被读成 2026-10）
    if year not in file_years:
      return ''
  elif _last_year(path) != year:
    # 文件名里没有年份 → 只能锚定目录里最后一个年份；目录也没有就丢弃
    return ''
  ctx = f'{_norm(path)} / {file_name}' if _norm(path) else file_name
  parts = _evidence_parts(evidence)
  if not parts or not all(part in ctx for part in parts):
    return ''
  if month not in _months(' / '.join(parts)):
    return ''
  return value


def _cache_key(payload: dict) -> str:
  seed = json.dumps(payload, sort_keys=True, ensure_ascii=False)
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


def _cache_write(payload: dict, mapping: dict) -> None:
  """只缓存**正常返回**（含「模型也没解析出来」）；调用失败不写，下一轮再试。"""
  try:
    redis_manager.client.set(_cache_key(payload),
                             json.dumps(mapping, ensure_ascii=False),
                             ex=_CACHE_TTL)
  except Exception as exc:
    logger.debug('[DocUpdate] 期号缓存写入失败：%s', exc)


async def _chat(messages, **kwargs) -> str:
  """模型调用出口（模块级，测试可 monkeypatch，不连网）。"""
  from core.model_gateway import model_gateway

  return await model_gateway.chat(messages, **kwargs)


async def _ask(payload: dict, paths: dict[str, str], expected: str) -> dict | None:
  messages = [{'role': 'system', 'content': _SYSTEM},
              {'role': 'user', 'content': json.dumps(payload, ensure_ascii=False)}]
  try:
    raw = await _chat(
      messages, temperature=0, max_tokens=_MAX_TOKENS, json_mode=True,
      timeout=float(settings.DOC_UPDATE_PERIOD_LLM_TIMEOUT),
      caller='doc_update_period')
  except Exception as exc:
    logger.warning('[DocUpdate] 期号兜底调用失败：%s', str(exc)[:200])
    return None
  entries = _loads(raw).get('files')
  if not isinstance(entries, list):
    logger.warning('[DocUpdate] 期号兜底返回结构不合预期：%s', str(raw)[:120])
    return None
  known = set(paths)
  out: dict[str, str] = {}
  for entry in entries:
    if not isinstance(entry, dict):
      continue
    file_name = _norm(entry.get('file'))
    if file_name not in known or file_name in out:
      continue
    value = _valid_period(entry.get('period'), file_name,
                          entry.get('evidence'), expected,
                          paths.get(file_name, ''))
    if value:
      out[file_name] = value
  return out


async def fill_periods(number: str, name: str, records: list[dict],
                       expected: str) -> int:
  """给 `period` 为空的文件补期号（模型兜底）。返回补上的文件数；异常不抛出。"""
  if not settings.DOC_UPDATE_PERIOD_LLM_ENABLED:
    return 0
  pending = [r for r in records if not r.get('period') and r.get('file_name')]
  if not pending:
    return 0
  files = [{'file': _norm(r['file_name']), 'path': _norm(r.get('path'))}
           for r in pending]
  paths: dict[str, str] = {}
  for item in files:
    paths.setdefault(item['file'], item['path'])
  payload = {'number': number, 'name': name, 'expected': expected, 'files': files}
  mapping = _cache_read(payload)
  if mapping is None:
    mapping = await _ask(payload, paths, expected)
    if mapping is None:          # 调用或解析失败：不缓存，留给下一轮
      return 0
    _cache_write(payload, mapping)
  filled = 0
  for record in pending:
    value = mapping.get(_norm(record['file_name'])) or ''
    if value:
      record['period'] = value
      filled += 1
  if filled:
    logger.info('[DocUpdate] 模型补期号 %s %s：%d/%d 个文件',
                number, name, filled, len(pending))
  return filled

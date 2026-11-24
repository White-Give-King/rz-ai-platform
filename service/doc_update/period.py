"""期号解析与目录形态判定（纯函数，不连库不联网）。

**默认只从目录名解析期号**：文件名里的日期是坑（如 `…2026-11更新.xlsx`），
会让按月提交的文档每月都被当成新一期。例外：`period_only` 项（能源月报）
各部门把期号写在文件名里、目录只到年份，允许 `name_period` 兜底；即便在例外分支
也**不使用 `create_time` 的年份**（分部常跨年补传，会把 2025 期算成 2026）。

年份可以写在容器目录的**前缀**（`2026年考勤`）、**括号**（`A9-…（2026）`）、
**内嵌**（`…【2025年10月总第116期】`）或**后缀**（`卫生清洁记录2026`）上，
月份由更深层的 `10月` 提供；后缀只认 `20xx`，避免把房间代号（`2101机房`）当年份。

月份也认**中文数字**（`十月` / `十一月` / `十二月`；部门实测把月份写在文件名里：
`视频录像监控查询单（一月）…（十月）.pdf`）。中文月份自身不带年份，年份同样取目录名，
**不拿 `create_time` 猜**；一个文件名里有多个中文月份时取最后一个（表示「更新到某月」）。

两种目录形态（由 `detect_layout` 自动判定，**只服务「查新增」项**）：
- `period` 有月份期号：路径里有月份目录（`2026/9月`、`2026-09`、`2026年9月`）——
  每月新起一份文件，只需看「本期应到那一期有没有文件」，不下载、不哈希。
- `flat` 无期号：没有月份目录 —— 比对 fid 是否在本期新出现，同样不下载。

**判定口径本身不在这里**：由清单项 `DocItem.judge`（PDF 第三列）决定，线上更新项走
sha256，其余项走本模块的期号 / 新 fid。例外：线上更新项若某部门实际按月新交
（`auto_flip`：多个月份期号 / 月份目录），抓取阶段改按期判。
"""

import re
from datetime import datetime

LAYOUT_PERIOD = 'period'
LAYOUT_FLAT = 'flat'

_YEAR_MONTH = re.compile(r'^(\d{4})\s*[-_/年.]\s*(\d{1,2})\s*月?(?:份)?$')
_YEAR_ONLY = re.compile(r'^(\d{4})\s*年?$')
_MONTH_ONLY = re.compile(r'^(\d{1,2})\s*月(?:份)?$')
# 目录名带业务标题时月份夹在中间，如「润泽国际信息港A-1能源报告【2025年10月总第116期】」。
# 只认「YYYY年M月」这种无歧义写法；`2026-11更新` 一类（文件名坑）不做模糊匹配。
_EMBEDDED_YEAR_MONTH = re.compile(r'(\d{4})\s*年\s*(\d{1,2})\s*月')
# 年份写在业务目录名里：`2026年考勤` / `2026年度文件` / `2026年视频监控查询`。
# 只取年份，月份仍由更深层的 `10月` 提供。
_YEAR_PREFIX = re.compile(r'^(\d{4})\s*年')
# 括号年份：`A9-卫生清洁记录表（2026）`。只看括号里就是四位数年份的写法。
_PAREN_YEAR = re.compile(r'[（(]\s*((?:19|20|21)\d{2})\s*[)）]')
# 年份写在业务目录名末尾：`卫生清洁记录2026`（当年记录目录，月份在更深层 `10月`）。
# 只认 20xx（房间代号 `2101机房` 之类不能当年份），且前一位不能是数字。
_YEAR_SUFFIX = re.compile(r'(?<!\d)(20\d{2})\s*年?\s*$')

# 中文数字月份（部门实测：`视频录像监控查询单（一月）…（十月）.pdf`）。
_CN_MONTHS = {
  '一': 1, '二': 2, '三': 3, '四': 4, '五': 5, '六': 6,
  '七': 7, '八': 8, '九': 9, '十': 10, '十一': 11, '十二': 12,
}
_CN_MONTH_CLASS = r'(十[一二]?|[一二三四五六七八九])'
# 年份与中文月份写在一起：`2026年十月报表`
_EMBEDDED_CN_YEAR_MONTH = re.compile(r'(\d{4})\s*年\s*' + _CN_MONTH_CLASS + r'\s*月')
# 整段就是中文月份：`十月`
_MONTH_ONLY_CN = re.compile(r'^' + _CN_MONTH_CLASS + r'\s*月份?$')
# 文件名里散落的中文月份（`（十月）`、`-十一月`）。前置不能是汉字/数字，
# 否则「统一月」会被切出「一月」；带年份的写法由 `_EMBEDDED_CN_YEAR_MONTH` 先取走。
_NAME_CN_MONTH = re.compile(r'(?<![\u4e00-\u9fff\d])' + _CN_MONTH_CLASS
                            + r'\s*月份?(?!度)')

# 文件名里的期号写法（实测 21 个部门）。按顺序取第一个有合法结果的写法。
_NAME_FULL_DATE = re.compile(r'(20\d{2})(\d{2})(\d{2})(?!\d)')       # 【20261031】
_NAME_YEAR_MONTH = re.compile(r'(\d{4})\s*年\s*(\d{1,2})\s*月')        # 2026年10月
_NAME_DASH_MONTH = re.compile(r'(20\d{2})\s*[-_/.]\s*(\d{1,2})(?!\d)')  # -2026-10月
_NAME_YYYYMM = re.compile(r'(?<!\d)(20\d{2})(0[1-9]|1[0-2])(?!\d)')   # 【202610】
_NAME_MONTH = re.compile(r'(?<!\d)(\d{1,2})\s*月份?(?!度)')             # 纯 `-10月`、4月份
_NAME_YEAR = re.compile(r'(20\d{2})\s*年')
_SEG_YEAR = re.compile(r'(?<!\d)(?:19|20|21)\d{2}(?!\d)')             # 目录里的年份


def _norm(text) -> str:
  return str(text or '').replace('\u3000', ' ').strip()


def _mk(year: int, month: int) -> str:
  if 1 <= month <= 12 and 2000 <= year <= 2100:
    return f'{year:04d}-{month:02d}'
  return ''


def _cn_month(text) -> int:
  """中文数字月份（`一`~`十二`）→ 1~12；认不出返回 0。"""
  return _CN_MONTHS.get(_norm(text), 0)


def parse_period(segments, fallback_year: int | None = None) -> str:
  """从目录名序列里解析月份级期号 `'YYYY-MM'`（取最深层的那一个）；解析不到返回 `''`。

  年份段向上找最近的四位年份；没有年份时用 `fallback_year`（一般取文件创建时间的年份）。
  """
  year = None
  found = ''
  for raw in segments or []:
    seg = _norm(raw)
    if not seg:
      continue
    m = _YEAR_MONTH.match(seg)
    if m:
      year = int(m.group(1))
      found = _mk(year, int(m.group(2))) or found
      continue
    m = _YEAR_ONLY.match(seg)
    if m:
      year = int(m.group(1))
      continue
    m = _MONTH_ONLY.match(seg)
    if m:
      use_year = year if year else fallback_year
      if use_year:
        found = _mk(int(use_year), int(m.group(1))) or found
      continue
    m = _MONTH_ONLY_CN.match(seg)
    if m:
      use_year = year if year else fallback_year
      if use_year:
        found = _mk(int(use_year), _cn_month(m.group(1))) or found
      continue
    # 年份容器（`2026年考勤` / `A9-…（2026）`）必须在 embedded 之后判：
    # `2026年10月报表` 要先被 `_EMBEDDED_YEAR_MONTH` 取走月份。
    m = _EMBEDDED_YEAR_MONTH.search(seg)
    if m:
      year = int(m.group(1))
      found = _mk(year, int(m.group(2))) or found
      continue
    m = _EMBEDDED_CN_YEAR_MONTH.search(seg)
    if m:
      year = int(m.group(1))
      found = _mk(year, _cn_month(m.group(2))) or found
      continue
    m = _YEAR_PREFIX.match(seg)
    if m:
      year = int(m.group(1))
      continue
    m = _PAREN_YEAR.search(seg)
    if m:
      year = int(m.group(1))
      continue
    # 年份后缀（`卫生清洁记录2026`）最后判：上面几条前缀/内嵌写法更具体，先匹配
    m = _YEAR_SUFFIX.search(seg)
    if m:
      year = int(m.group(1))
  return found


def period_of_file(file: dict, from_name: bool = False) -> str:
  """单个文件（walk 产出的 dict：`segments` + `topic` + `create_time`）的期号。

  目录名能解析出月份级期号时**以目录为准**；`from_name=True` 且目录解析不到时，
  退到文件名（`period_only` 项用），年份取目录里的年份，**不用 `create_time`**。
  """
  segments = file.get('segments') or []
  found = parse_period(segments, _create_year(file.get('create_time')))
  if found or not from_name:
    return found
  return name_period(file.get('topic') or '', _segments_year(segments))


def name_period(topic, fallback_year: int | None = None) -> str:
  """从文件名解析月份级期号；解析不到返回 `''`。

  优先级：8 位日期 → `YYYY年M月` → `YYYY-M` → `YYYYMM` → 纯 `M月` →
  中文数字月份（`十月` / `十一月` / `十二月`，阿拉伯写法都没命中时才用）。
  纯月份没有自带年份时，取该月**之前最近的 `YYYY年`**（没有则取文中任意一个），
  再没有才用 `fallback_year`（一般传目录里的年份）。
  """
  text = _norm(topic)
  if not text:
    return ''
  for regex in (_NAME_FULL_DATE, _NAME_YEAR_MONTH, _NAME_DASH_MONTH, _NAME_YYYYMM):
    value = _last_period(regex, text)
    if value:
      return value
  for m in reversed(list(_NAME_MONTH.finditer(text))):
    year = _year_near(text, m.start(), fallback_year)
    value = _mk(int(year), int(m.group(1))) if year else ''
    if value:
      return value
  # 中文数字月份排在阿拉伯写法之后，只在前面都没命中时才用，不改变既有优先级。
  for m in reversed(list(_EMBEDDED_CN_YEAR_MONTH.finditer(text))):
    value = _mk(int(m.group(1)), _cn_month(m.group(2)))
    if value:
      return value
  for m in reversed(list(_NAME_CN_MONTH.finditer(text))):
    year = _year_near(text, m.start(), fallback_year)
    value = _mk(int(year), _cn_month(m.group(1))) if year else ''
    if value:
      return value
  return ''


def _last_period(regex, text: str) -> str:
  """取最后一个合法命中（同一文件名里常有旧期号在括号里）。"""
  for m in reversed(list(regex.finditer(text))):
    value = _mk(int(m.group(1)), int(m.group(2)))
    if value:
      return value
  return ''


def _year_near(text: str, pos: int, fallback_year: int | None) -> int | None:
  years = [(m.start(), int(m.group(1))) for m in _NAME_YEAR.finditer(text)]
  before = [year for start, year in years if start < pos]
  if before:
    return before[-1]
  after = [year for start, year in years if start > pos]
  if after:
    return after[0]
  return fallback_year


def _segments_year(segments) -> int | None:
  """目录序列里最后一个四位年份（`2026`、`2026年`、`2026年输出物` 都认）。"""
  year = None
  for raw in segments or []:
    for m in _SEG_YEAR.finditer(_norm(raw)):
      year = int(m.group(0))
  return year


def recover_period(file_name, path: str = '') -> str:
  """读侧重算：快照只存了文件名与目录链时补出期号（确定性解析，解析不到返回 `''`）。

  `path` 是快照里的 `' / '.join(segments)` 目录链（不含文件名）；年份取目录链里最后一个
  四位年份，文件名自带年份时以文件名为准。期号解析改进后旧快照不必重跑即可自愈。
  """
  segments = [seg for seg in str(path or '').split(' / ') if seg.strip()]
  return name_period(file_name, _segments_year(segments))


def auto_flip(files) -> bool:
  """「查更新」项在该部门的这批文件应按期判（按月新交）而不是比 sha256。

  两条确定性信号：路径里有月份目录（不用 `create_time` 兜底年份），或按
  「目录年份 + 文件名月份」解析出 ≥2 个不同月份。**要求 ≥2 个月份是刻意设的误判闸**：
  文件名里带日期、原地覆盖的文件只出现一条时不翻，仍走 sha256，
  否则每月都会按那个旧日期判「未更新」。
  """
  rows = list(files or [])
  if any(parse_period(f.get('segments') or []) for f in rows):
    return True
  periods = {
    name_period(f.get('topic') or '', _segments_year(f.get('segments')))
    for f in rows
  }
  return len({p for p in periods if p}) >= 2


def month_clue_no_period(file_name, path: str = '') -> bool:
  """带月份线索、但目录链与文件名都没有年份锚 —— 期号确定性解析不出，交人工确认。

  只在「查更新项已按部门改判为查新增」时用于挑待确认文件，不参与判定。
  """
  segments = [seg for seg in str(path or '').split(' / ') if seg.strip()]
  if parse_period(segments) or name_period(file_name, _segments_year(segments)):
    return False
  texts = [_norm(seg) for seg in segments] + [_norm(file_name)]
  return any(_NAME_MONTH.search(text) or _NAME_CN_MONTH.search(text)
             for text in texts)


def detect_layout(files) -> str:
  """只要有一条路径解析出月份级期号，就按「有月份期号」判。"""
  for f in files or []:
    if period_of_file(f):
      return LAYOUT_PERIOD
  return LAYOUT_FLAT


def pick_latest_period(files, from_name: bool = False) -> str:
  periods = [period_of_file(f, from_name) for f in files or []]
  periods = [p for p in periods if p]
  return max(periods) if periods else ''


def relevant(rows, expected: str, *, keep_undated: bool = False) -> list:
  """按期号裁出与「本期是否已交」相关的记录（页面列表与判定证据共用同一口径）。

  本期及更晚的全留；一条都没有时只留最新一期（要能判「未更新」并显示最新期号）；
  全部解析不出期号时原样返回（模板/年度汇总，判定要能看到全部）。
  `keep_undated=True` 额外保留期号为空的行，供页面展示模板；判定口径不依赖这里
  —— `max(period)` 与最新一期都不会被裁掉。
  """
  rows = list(rows)
  with_period = [(r, r.get('period') or '') for r in rows if r.get('period')]
  if not with_period:
    return rows
  kept = [r for r, p in with_period if expected and p >= expected]
  if not kept:
    latest = max(p for _, p in with_period)
    kept = [r for r, p in with_period if p == latest]
  if keep_undated:
    ids = {id(r) for r in kept}
    return [r for r in rows if id(r) in ids or not r.get('period')]
  return kept


_PERIOD_RE = re.compile(r'^(\d{4})-(0[1-9]|1[0-2])$')


def current_period(now: datetime | None = None) -> str:
  """本地当前月 `YYYY-MM`（判定侧「本期」口径；补跑出来的旧期永不等于它）。"""
  now = now or datetime.now()
  return f'{now.year:04d}-{now.month:02d}'


def check_target_period(target: str, current: str, exists: bool) -> str:
  """补跑期校验：返回错误文案（空串 = 可补跑）。

  `exists` 由调用方传「已有快照 and not force」—— force 只绕过「已存在」这一条，
  格式与未来期间永远拦截。
  """
  text = (target or '').strip()
  if not _PERIOD_RE.match(text):
    return '期间格式应为 YYYY-MM'
  if text > current:
    return '不能补跑未来期间'
  if exists:
    return '该期已有快照（覆盖请加 --force）'
  return ''


def expected_period(now: datetime | None = None, grace_months: int = 0) -> str:
  """本期应到：默认就是 `now` 所在月（期间选 11 月就查 11 月报告）。

  `grace_months` 只用于兼容「按报告期命名」的老口径（设 N 回退 N 月）。**主链路里
  期间与应到期必须同源**（`fetch` 用同一个值生成 `period_key`），否则页面期间与
  判定月份错位 —— 曾因此把 11 月期间按上月（10 月）判，报告没交也显示「已更新」。
  """
  now = now or datetime.now()
  total = now.year * 12 + (now.month - 1) - max(0, int(grace_months))
  return f'{total // 12:04d}-{total % 12 + 1:02d}'


def shift_period(period_key: str, months: int) -> str:
  """`2026-11` 平移 N 月（负=往前，`-1` → `2026-10`）；解析不了原样返回。"""
  text = (period_key or '').strip()
  m = re.fullmatch(r'(\d{4})-(\d{2})', text)
  if not m or not months:
    return text
  total = int(m.group(1)) * 12 + int(m.group(2)) - 1 + months
  return f'{total // 12:04d}-{total % 12 + 1:02d}'


def _create_year(value) -> int | None:
  """`createTime` 可能是毫秒/秒时间戳或 ISO 串；只取年份，取不到返回 None。"""
  try:
    if isinstance(value, (int, float)):
      n = float(value)
      if n > 1e12:
        n /= 1000
      if n <= 0:
        return None
      return datetime.fromtimestamp(n).year
    text = _norm(value)
    if not text:
      return None
    m = re.match(r'^(\d{4})[-/年]', text)
    return int(m.group(1)) if m else None
  except (OSError, ValueError, OverflowError):
    return None

"""快照比对（纯函数，不连库）：两份快照 → 25 项状态。

**只有两种口径**（`evaluate` 返回的 `mode`）：
- `cross` 跨期：本期 vs **上一期（上月）** → 「已更新 / 未更新」（唯一判定口径）
- `baseline` 基线：没有上期可比，只建基线，**不说「未更新」**

参照物只能是上一期快照。**不得**拿同月早先的检查结果（或任何「上次执行」）当参照——
否则「文件已更新」会被下一次检查自己抹成「未更新」。参照由调用方（`store.previous`）保证。

**判定口径由清单项决定**（`DocItem.judge`，对应 PDF 第三列，一类文档一个口径）：
- `update`（第三列写「线上更新」）：**查更新** —— 下载算 sha256，与上一期比内容是否变化；
- `new`（其余 PDF/PPT/文档）：**查新增** —— 路径有月份期号就只看本期应到期是否已交，
  没有期号就比对 fid（出现上期没有的文件即算已交），不下载。

状态键（`updated/stale/missing/review/failed`）：
- `updated`：查更新项 sha256 与参照快照不同；或查新增项本期已交（新期号 / 新 fid）
- `stale`：查更新项 sha256 与参照快照相同；或查新增项本期没有新文件
- `missing` 未找到：未查询到相关文件（按编号/别名都匹配不到，模型也没挑出）
- `review` 待复核：确定性匹配为空，模型在范围内找到「疑似同主题」的文件但无法确定归属 ——
  **不当已命中**，不参与期号/哈希判定，只把候选列给人工确认
- `failed` 下载失败：取不到字节或类型校验不过 —— **不算「未更新」**；
  `period_only` 项找到文件却解析不出期号也记这里，`error` 写明原因
"""

from datetime import datetime, timezone

from config.settings import settings
from service.doc_update import period
from service.doc_update.catalog import JUDGE_NEW, JUDGE_UPDATE

STATUS_UPDATED = 'updated'
STATUS_STALE = 'stale'
STATUS_MISSING = 'missing'
STATUS_REVIEW = 'review'
STATUS_FAILED = 'failed'
STATUS_BASELINE = 'baseline'

MODE_CROSS = 'cross'
MODE_BASELINE = 'baseline'

# 未找到项的说明文案（写快照时用它；读老快照时把旧文案归一成同一句）
MISSING_TEXT = '未查询到相关文件'
_LEGACY_MISSING_TEXT = '整树中未找到该编号的文件'
# 模型在范围内找到疑似同主题的文件，但无法确定归属：判「待复核」，不当已命中
REVIEW_TEXT = '匹配到疑似同主题文件，归属待人工确认'
# `period_only` 项找到文件却解析不出期号：不是「未更新」，要说清原因
UNPARSED_TEXT = '已找到文件但未能从目录名/文件名解析出期号'

STATUS_LABELS = {
  STATUS_UPDATED: '已更新',
  STATUS_STALE: '未更新',
  STATUS_MISSING: '未找到',
  STATUS_REVIEW: '待复核',
  STATUS_FAILED: '下载失败',
  STATUS_BASELINE: '基线',
}

_STATUS_ORDER = (STATUS_UPDATED, STATUS_STALE, STATUS_MISSING, STATUS_REVIEW,
                 STATUS_FAILED, STATUS_BASELINE)


def review_names(rows) -> list[str]:
  """待复核候选 → 真实文件名列表（strip、去重保序）。

  契约（前端 `review_files?: string[]`）只认文件名；老快照存的是文件对象
  （`fid/topic/...`），读取时在这里归一，页面不会再拿到对象。
  """
  out: list[str] = []
  for row in rows or ():
    if isinstance(row, str):
      name = row
    else:
      name = row.get('topic') or row.get('file_name') or row.get('name') or ''
    name = str(name).strip()
    if name and name not in out:
      out.append(name)
  return out


def _prev_index(previous: dict | None) -> dict[str, str]:
  idx: dict[str, str] = {}
  for item in (previous or {}).get('items') or []:
    for f in item.get('files') or []:
      fid = str(f.get('fid') or '')
      if fid:
        idx[fid] = f.get('sha256') or ''
  return idx


def _prev_flat_index(previous: dict | None) -> dict[str, str]:
  """上期快照里所有文件的 sha256，按 fid 索引（部门分支用）。"""
  idx: dict[str, str] = {}
  for dept in (previous or {}).get('depts') or []:
    for item in dept.get('items') or []:
      for f in item.get('files') or []:
        fid = str(f.get('fid') or '')
        if fid:
          idx[fid] = f.get('sha256') or ''
  return idx


def _fids(snapshot: dict | None) -> set[str]:
  out: set[str] = set()
  for item in (snapshot or {}).get('items') or []:
    for f in item.get('files') or []:
      fid = str(f.get('fid') or '')
      if fid:
        out.add(fid)
  return out


def _days_ago(value, now: datetime | None) -> int | None:
  if not isinstance(value, datetime):
    return None
  ref = now or datetime.now(timezone.utc)
  if value.tzinfo is None:
    value = value.replace(tzinfo=timezone.utc)
  return max(0, (ref - value).days)


def _max_dt(values) -> datetime | None:
  dates = [v for v in values if isinstance(v, datetime)]
  return max(dates) if dates else None


def _file_verdict(f: dict, prev_sha: str, baseline: bool, labels: dict) -> dict:
  sha = f.get('sha256') or ''
  if not sha:
    status = STATUS_FAILED
  elif baseline:
    status = STATUS_BASELINE
  elif sha != prev_sha:
    status = STATUS_UPDATED
  else:
    status = STATUS_STALE
  return {**f, 'status': status, 'status_label': labels[status]}


def _item_status(files: list[dict], baseline: bool) -> str:
  if not files:
    return STATUS_MISSING
  if not any(f.get('sha256') for f in files):
    return STATUS_FAILED
  if baseline:
    return STATUS_BASELINE
  if any(f['status'] == STATUS_UPDATED for f in files):
    return STATUS_UPDATED
  return STATUS_STALE


def _error_text(text) -> str:
  """老快照里存的是旧文案，读出来统一成当前口径。"""
  text = text or ''
  return MISSING_TEXT if text == _LEGACY_MISSING_TEXT else text


def _period_status(files: list[dict], expected: str) -> tuple[str, str]:
  """查新增（路径有月份期号）：本期应到期（或更晚）有文件即已更新，否则未更新。返回 (状态, 最新期)。"""
  periods = [f.get('period') or '' for f in files]
  periods = [p for p in periods if p]
  latest = max(periods) if periods else ''
  if latest and expected and latest >= expected:
    return STATUS_UPDATED, latest
  return STATUS_STALE, latest


def _flat_status(files: list[dict], prev: dict[str, str], baseline: bool) -> str:
  if not any(f.get('sha256') for f in files):
    return STATUS_FAILED
  if baseline:
    return STATUS_BASELINE
  if any((f.get('sha256') or '') != prev.get(str(f.get('fid') or ''), '')
         for f in files):
    return STATUS_UPDATED
  return STATUS_STALE


def _new_fid_status(files: list[dict], prev_fids: set[str], baseline: bool) -> str:
  """查新增（路径无月份期号）：出现上期快照里没有的文件即算本期已交。"""
  if baseline:
    return STATUS_BASELINE
  if any(str(f.get('fid') or '') not in prev_fids for f in files):
    return STATUS_UPDATED
  return STATUS_STALE


def _dept_key(dept: dict) -> tuple[str, str]:
  """部门键：公司 + dept_id（dept_id 空时退回名称）。"""
  return (str(dept.get('company_id') or ''),
          str(dept.get('dept_id') or dept.get('dept_name') or ''))


def _evaluate_depts(latest: dict, previous: dict | None,
                    now: datetime | None) -> dict:
  """部门分支：逐「部门 × 清单项」按清单项的 `judge` 判状态（查更新 / 查新增）。"""
  prev_snapshot = previous if (previous or {}).get('depts') else None
  baseline = not prev_snapshot
  mode = MODE_BASELINE if baseline else MODE_CROSS
  labels = STATUS_LABELS
  prev = _prev_flat_index(prev_snapshot)
  prev_fids = _dept_fids(prev_snapshot)
  prev_depts = {_dept_key(d) for d in (prev_snapshot or {}).get('depts') or []}
  # 应到期＝该快照的期间（选 11 月就查 11 月）。读老快照也按期间重算，不用写库时
  # 存的值 —— 旧口径把「11 月期间」按上月（10 月）判，11 月报告没交也显示「已更新」
  # 查新增项另可用 `DOC_UPDATE_NEW_ACCEPT_PREV_MONTHS`（默认 0）把接受下限回退 N 月
  period_key = str(latest.get('period_key') or '')
  counts = {key: 0 for key in _STATUS_ORDER}
  items: list[dict] = []
  warnings: list[dict] = []
  changed: list[dict] = []
  depts: list[dict] = []

  for dept in latest.get('depts') or []:
    dept_id = str(dept.get('dept_id') or '')
    dept_name = dept.get('dept_name') or ''
    company_id = str(dept.get('company_id') or '')
    company_name = dept.get('company_name') or ''
    dept_error = dept.get('error') or ''
    # 上期没有这个部门（新增公司/部门）→ 需比上期的项按基线算，不误报「已更新」
    dept_baseline = baseline or _dept_key(dept) not in prev_depts
    dept_counts = {key: 0 for key in _STATUS_ORDER}

    for item in dept.get('items') or []:
      raw = item.get('files') or []
      layout = item.get('layout') or 'flat'
      # 老快照没有 `judge` 字段：按当时的形态推断（有月份期号=查新增，其余=查更新）
      judge = item.get('judge') or (JUDGE_NEW if layout == 'period' else JUDGE_UPDATE)
      expected = period_key or item.get('expected_period') or ''
      # 「查新增」项可临时接受前 N 月（DOC_UPDATE_NEW_ACCEPT_PREV_MONTHS，默认 0）；
      # 查更新项不参与（它只比 sha256）。放宽只对「本期」有意义 —— 补跑出来的
      # 2026-10 快照不能去接受 2026-09 的文件。放宽后页面「应到」显示实际判定下限。
      if (judge == JUDGE_NEW and expected
          and expected == period.current_period()):
        months = max(0, settings.DOC_UPDATE_NEW_ACCEPT_PREV_MONTHS or 0)
        if months:
          expected = period.shift_period(expected, -months)
      period_only = bool(item.get('period_only'))
      files = [dict(f) for f in raw]
      # 期号解析改进后旧快照不会自动重算：读侧对扁平查新增项的空期号按「目录年份 +
      # 文件名月份」再解析一次（确定性、不猜）。只补空的，已有期号原样不动
      recovered = False
      if judge != JUDGE_UPDATE and layout == 'flat':
        for f in files:
          if not f.get('period'):
            value = period.recover_period(f.get('file_name'), f.get('path'))
            if value:
              f['period'] = value
              recovered = True
      # 待复核候选：老快照存文件对象、新快照存文件名，统一归一后再判状态/下发
      review_files = review_names(item.get('review_files'))
      # 待人工确认（月份线索无年份锚）：只做展示，不参与状态判定
      pending_files = review_names(item.get('pending_files'))
      # 补跑是「用当前共享盘重放那一期」：晚于该期的文件不算那一期已交
      if latest.get('backfill') and expected:
        files = [f for f in files if not f.get('period')
                 or f.get('period') <= expected]
      # 扁平查新增项只要有一份文件解析出期号，期号为空的（外层模板/年度汇总/清单外副本）
      # 就不再算候选 —— 它们既证明不了「本期已交」，也不该显示成「未更新」。
      # 整项全无期号时原样保留，仍走 fid/基线兜底（一份文件长期挂着看新 fid 的口径）。
      if (judge != JUDGE_UPDATE and layout == 'flat'
          and any(f.get('period') for f in files)):
        files = [f for f in files if f.get('period')]
      # 找到文件但一个期号都解析不出：判失败并说明，不能含糊成「未更新」
      no_periods = (period_only and layout == 'period' and bool(files)
                    and not any(f.get('period') for f in files))
      # 扁平查新增项：文件名期号已解析出来（确定性或模型兜底）→ 按期判，
      # 不再依赖「上期 fid 集合」。没有任何期号时仍回退原 fid/基线口径
      flat_by_period = (judge != JUDGE_UPDATE and layout == 'flat'
                        and any(f.get('period') for f in files))

      if dept_error:
        status = STATUS_FAILED
        latest_period = item.get('latest_period') or ''
      elif not files:
        # 有归属待确认的候选 → 待复核（不当已命中）；否则就是未找到
        status = STATUS_REVIEW if review_files else STATUS_MISSING
        latest_period = item.get('latest_period') or ''
      elif judge == JUDGE_UPDATE:
        status = _flat_status(files, prev, dept_baseline)
        latest_period = item.get('latest_period') or ''
      elif layout == 'period':
        status, latest_period = _period_status(files, expected)
        if no_periods:
          status = STATUS_FAILED
      elif flat_by_period:
        status, latest_period = _period_status(files, expected)
      else:
        status = _new_fid_status(files, prev_fids, dept_baseline)
        latest_period = item.get('latest_period') or ''
      if not latest_period:
        latest_period = item.get('latest_period') or ''

      for f in files:
        if judge == JUDGE_UPDATE:
          f_status = _file_verdict(f, prev.get(str(f.get('fid') or ''), ''),
                                   dept_baseline, labels)['status']
        elif layout == 'period':
          period_value = f.get('period') or ''
          if period_only and not period_value:
            f_status = STATUS_FAILED
            f['error'] = f.get('error') or UNPARSED_TEXT
          else:
            f_status = STATUS_UPDATED if (period_value and period_value >= expected) \
              else STATUS_STALE
        elif flat_by_period:
          # 文件级同期判（无期号的文件记「未更新」，不猜测）
          period_value = f.get('period') or ''
          f_status = STATUS_UPDATED if (period_value and period_value >= expected) \
            else STATUS_STALE
        elif dept_baseline:
          f_status = STATUS_BASELINE
        elif str(f.get('fid') or '') not in prev_fids:
          f_status = STATUS_UPDATED
        else:
          f_status = STATUS_STALE
        f['status'] = f_status
        f['status_label'] = labels[f_status]

      # 页面列表只展示与本期判定相关的文件（本期/最新一期 + 模板等无期号文件）；
      # 快照仍存全量，判定与 changes 都基于未裁剪的 `files`。只对扁平查新增项生效
      # —— period 项在 fetch 已裁过，查更新项与 fid 判定项需要全部文件。
      display = files
      if judge != JUDGE_UPDATE and layout == 'flat':
        display = period.relevant(files, expected, keep_undated=True)

      counts[status] = counts.get(status, 0) + 1
      dept_counts[status] += 1

      changed_at = _max_dt([f.get('last_changed_at') for f in files if f.get('sha256')])
      for f in files:
        if f.get('warn'):
          warnings.append({
            'company_id': company_id, 'company_name': company_name,
            'dept_id': dept_id, 'dept_name': dept_name,
            'number': item.get('number') or '', 'name': item.get('name') or '',
            'file_name': f.get('file_name') or '', 'warn': f['warn'],
          })
        # 「本期变更」只列查更新项（有内容哈希变化时间）；查新增项没有哈希
        if f['status'] == STATUS_UPDATED and judge == JUDGE_UPDATE:
          changed.append({
            'company_id': company_id, 'company_name': company_name,
            'dept_id': dept_id, 'dept_name': dept_name,
            'number': item.get('number') or '', 'name': item.get('name') or '',
            'file_name': f.get('file_name') or '',
            'changed_at': f.get('last_changed_at'),
          })

      item_error = dept_error or _error_text(item.get('error'))
      if status == STATUS_REVIEW and not dept_error:
        item_error = REVIEW_TEXT
      if no_periods and not dept_error:
        item_error = UNPARSED_TEXT
      # 模型判定置信度（纯读侧推导，不落库；已有快照下次打开页面即生效）：
      # 同主题复核（llm）映射本身可信 = 高；子代理重定位（agent）命中文件能解析出
      # 月份级期号 = 高，只有无期号/模板 = 低（提示人工复核）。其余来源不产生值、不显示徽标。
      mapped_by = item.get('mapped_by') or 'heuristic'
      confidence = ''
      if mapped_by == 'llm':
        confidence = 'high'
      elif mapped_by == 'agent':
        confidence = 'high' if any(f.get('period') for f in files) else 'low'
      # 低置信度必须说清「为什么判不了、为什么要人工看」——光有定位依据不够
      # （如重庆 A1 值班巡检：子 agent 找到了真实文件，但文件名只有「（X月）」无年份）。
      confidence_reason = ''
      if confidence == 'low':
        month_only = any(
          period.month_clue_no_period(f.get('file_name') or '', f.get('path') or '')
          for f in files)
        if month_only:
          confidence_reason = (
            '模型已定位到真实记录，但文件名/目录只有月份、没有年份'
            '（平台不按上传时间猜年份），期号无法判定，确认不了本期是否已交，请人工核对')
        else:
          confidence_reason = (
            '模型已定位到文件，但没有可识别的期号，无法确认本期是否已交，请人工核对')
      items.append({
        'company_id': company_id,
        'company_name': company_name,
        'dept_id': dept_id,
        'dept_name': dept_name,
        'seq': item.get('seq'),
        'number': item.get('number') or '',
        'name': item.get('name') or '',
        'cadence': item.get('cadence') or '',
        'note': item.get('note') or '',
        'judge': judge,
        'layout': layout,
        'expected_period': expected,
        'latest_period': latest_period,
        # 期号来源（扁平查新增项）：'name' 文件名确定性解析（含读侧补算）/ 'llm' 模型兜底 / ''
        'period_source': item.get('period_source') or ('name' if recovered else ''),
        # 实际命中的目录名（页面展示用，老快照没有这个字段就是空数组）
        'actual_dirs': item.get('actual_dirs') or [],
        # 来源：配置的显式路径 / 自动匹配 / 模型复核（老快照没有该字段就按自动匹配）
        'mapped_by': item.get('mapped_by') or 'heuristic',
        # 模型判定置信度：'high' 模型判定 / 'low' 建议人工复核 / '' 非模型判定
        'confidence': confidence,
        # 低置信度时说明为何判不了（定位依据在 match_reason，此处是"为何需复核"）
        'confidence_reason': confidence_reason,
        # 模型复核的一句依据、实际查找路径、目录里的真实文件名、目录截图（老快照为空）
        'match_reason': item.get('match_reason') or '',
        'search_path': item.get('search_path') or '',
        'dir_files': item.get('dir_files') or [],
        # 模型给出的疑似文件（仅未命中时非空；判「待复核」，不改判定口径）
        'review_files': review_files,
        # 月份线索无年份锚的文件（仅按部门改判为查新增的项目）：待人工确认，不改状态
        'pending_files': pending_files,
        'screenshot': item.get('screenshot') or None,
        'config_path': item.get('config_path') or [],
        'warn': item.get('warn') or '',
        'status': status,
        'status_label': labels[status],
        'error': item_error,
        'file_count': len(display),
        # 匹配总数（含未列出的历史文件）；老快照没有该字段就回退到全量文件数
        'matched_count': int(item.get('matched_count') or len(files)),
        'changed_at': changed_at,
        'changed_days_ago': _days_ago(changed_at, now),
        'files': display,
      })

    depts.append({
      'company_id': company_id,
      'company_name': company_name,
      'dept_id': dept_id,
      'dept_name': dept_name,
      'root_fid': dept.get('root_fid') or '',
      'error': dept_error,
      'baseline': dept_baseline,
      'counts': dept_counts,
      'total': sum(dept_counts.values()),
    })

  latest_fids = _dept_fids(latest)
  return {
    'baseline': baseline,
    'mode': mode,
    'labels': labels,
    'scope': 'dept',
    'period_key': latest.get('period_key') or '',
    'period_label': latest.get('period_label') or '',
    'taken_at': latest.get('taken_at'),
    'counts': counts,
    'total': len(items),
    'dept_count': len(depts),
    'depts': depts,
    'items': items,
    'changes': {
      'changed': changed,
      'added_fids': sorted(latest_fids - prev_fids),
      'removed_fids': sorted(prev_fids - latest_fids) if prev_snapshot else [],
    },
    'warnings': warnings,
  }


def _dept_fids(snapshot: dict | None) -> set[str]:
  out: set[str] = set()
  for dept in (snapshot or {}).get('depts') or []:
    for item in dept.get('items') or []:
      for f in item.get('files') or []:
        fid = str(f.get('fid') or '')
        if fid:
          out.add(fid)
  return out


def evaluate(latest: dict | None, previous: dict | None,
             now: datetime | None = None) -> dict:
  """比对两份快照。

  `previous` 必须是**上一期（上月）**快照；`None` 表示没有上期，只建基线。
  **不要**传同月早先的检查结果 —— 那会把「已更新」在下次检查时抹成「未更新」。
  快照带 `depts` 时走部门分支，否则走旧单树分支。
  """
  if (latest or {}).get('depts'):
    return _evaluate_depts(latest, previous, now)
  baseline = not previous
  mode = MODE_BASELINE if baseline else MODE_CROSS
  labels = STATUS_LABELS
  prev = _prev_index(previous)
  counts = {key: 0 for key in _STATUS_ORDER}
  items: list[dict] = []
  warnings: list[dict] = []
  changed: list[dict] = []

  for item in (latest or {}).get('items') or []:
    raw = item.get('files') or []
    files = [
      _file_verdict(f, prev.get(str(f.get('fid') or ''), ''), baseline, labels)
      for f in raw
    ]
    status = _item_status(files, baseline)
    counts[status] = counts.get(status, 0) + 1

    changed_at = _max_dt([f.get('last_changed_at') for f in files if f.get('sha256')])
    for f in files:
      if f.get('warn'):
        warnings.append({
          'number': item.get('number') or '',
          'name': item.get('name') or '',
          'file_name': f.get('file_name') or '',
          'warn': f['warn'],
        })
      if f['status'] == STATUS_UPDATED:
        changed.append({
          'number': item.get('number') or '',
          'name': item.get('name') or '',
          'file_name': f.get('file_name') or '',
          'changed_at': f.get('last_changed_at'),
        })

    items.append({
      'seq': item.get('seq'),
      'number': item.get('number') or '',
      'name': item.get('name') or '',
      'cadence': item.get('cadence') or '',
      'note': item.get('note') or '',
      'status': status,
      'status_label': labels[status],
      'error': _error_text(item.get('error')),
      'file_count': len(files),
      'changed_at': changed_at,
      'changed_days_ago': _days_ago(changed_at, now),
      'files': files,
    })

  latest_fids = _fids(latest)
  prev_fids = _fids(previous)
  return {
    'baseline': baseline,
    'mode': mode,
    'labels': labels,
    'period_key': (latest or {}).get('period_key') or '',
    'period_label': (latest or {}).get('period_label') or '',
    'taken_at': (latest or {}).get('taken_at'),
    'counts': counts,
    'total': len(items),
    'items': items,
    'changes': {
      'changed': changed,
      'added_fids': sorted(latest_fids - prev_fids),
      'removed_fids': sorted(prev_fids - latest_fids) if previous else [],
    },
    'warnings': warnings,
  }


def summary_text(verdict: dict) -> str:
  """一句话结论（工具/页面抬头共用）：基线期不说「未更新」。"""
  counts = verdict.get('counts') or {}
  total = verdict.get('total') or 0
  if verdict.get('scope') == 'dept':
    parts = (f"已更新 {counts.get(STATUS_UPDATED, 0)} · "
             f"未更新 {counts.get(STATUS_STALE, 0)} · "
             f"未找到 {counts.get(STATUS_MISSING, 0)} · "
             f"下载失败 {counts.get(STATUS_FAILED, 0)}")
    if counts.get(STATUS_REVIEW):
      parts += f" · 待复核 {counts.get(STATUS_REVIEW, 0)}"
    if counts.get(STATUS_BASELINE):
      parts += f" · 基线 {counts.get(STATUS_BASELINE, 0)}"
    companies = {d.get('company_id') or d.get('company_name')
                 for d in verdict.get('depts') or []}
    prefix = f'{len(companies)} 家公司 · ' if len(companies) > 1 else ''
    head = (f"{verdict.get('period_label') or ''} {prefix}"
            f"{verdict.get('dept_count') or 0} 个部门 × 共 {total} 格：")
    return f'{head}{parts}'
  if verdict.get('baseline'):
    return f'已建立基线（{total} 项），下期起比对是否更新。'
  parts = (f"已更新 {counts.get(STATUS_UPDATED, 0)} · "
           f"未更新 {counts.get(STATUS_STALE, 0)} · "
           f"未找到 {counts.get(STATUS_MISSING, 0)} · "
           f"下载失败 {counts.get(STATUS_FAILED, 0)}")
  return f'{verdict.get("period_label") or ""} 共 {total} 项：{parts}'

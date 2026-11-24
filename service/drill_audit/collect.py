"""演练周采集入口：对话工具与未来「每周演练合规检查」共用。

职责边界：
- 定位演练周 → 逐班组取文本（命中内容库则零下载零 OCR）→（可选）检索 EOP + 判定
  → 落 `drill_audit_weeks` 记录；
- 存储细节全在 `store.py`，判定口径全在 `judge.py` —— 这里只做编排。

未来周任务（Phase 2）只需：遍历配置部门 → 选出本周已结束且有记录的演练周 →
逐周 `collect_week(dept_id, week_id=..., audit=True)`；不需要重写取数/OCR/判定。
"""

from typing import Awaitable, Callable

Progress = Callable[[str], Awaitable[None]] | None


async def collect_week(
  dept_id: str,
  *,
  plan_name: str = '',
  week_id: str = '',
  year: int | str = 0,
  week: int = 0,
  groups: str = '',
  user_id: str | None = None,
  refresh: bool = False,
  audit: bool = True,
  progress: Progress = None,
  audit_source: str = 'dialog',
  run_id: str = '',
  period_key: str = '',
  period_label: str = '',
) -> dict:
  """采集并（可选）审查一个演练周，返回 `pipeline.run_audit` 同构结果。

  Args:
    dept_id: 部门 id（`week_id` 直给时可留空）。
    refresh: True 时绕过内容库，重下重解析。
    audit: False 时只采集文本、不检索 EOP 也不判定（批量采集后统一判定的场景）。
    audit_source / run_id / period_key / period_label: 透传给 `run_audit`（周任务用）。
  """
  from . import pipeline

  return await pipeline.run_audit(
    plan_name=plan_name,
    week_id=week_id,
    dept_id=dept_id,
    year=year,
    week=week,
    groups=groups,
    user_id=user_id,
    progress=progress,
    refresh=refresh,
    audit=audit,
    audit_source=audit_source,
    run_id=run_id,
    period_key=period_key,
    period_label=period_label,
  )

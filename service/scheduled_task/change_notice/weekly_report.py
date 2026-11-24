"""变更通知**合规周报**工作流（分析报告 · `changenotice__nationwide__weekly`）。

与「每日变更统计」同源取数（`workflow.collect_events`：读群消息 → 解析 → 模型兜底 →
配对去重 → 补职级），但产出是**一份 PDF 合规周报**：统计概览 + 两张图 + 合规率汇总 +
不合规明细清单（附录）。合规判定是**可扩展规则表**（`compliance.RULES`，加规则只追加一项）。

窗口＝**上一个完整自然周**（`report_templates.window_for('weekly')`，周一~周日，含首尾）。
为何带回看：`_merge` 按 (园区,工单号) 把「开始 + 完成」两条通知配成一行，跨周时
「上周开始、本周完成」的变更其 start_msg_time 落在回看区；不带回看会把规则①/②
误判成「没发开始」。回看只为补齐开始通知，**事件归属仍按本周时刻**过滤。

0 事件**不算失败**：落一条 SUCCESS 记录、`artifact_urls=[]`，页面显示「无产物」。
任何异常都 `finish_run(FAILED)`（否则页面永远卡「生成中」），不向调用方抛。
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from config.settings import settings
from core.sandbox.datasets import load_dataset_dir, save_dataset
from models.scheduled_task import RunStatus
from service.scheduled_task import repository as repo
from service.scheduled_task.change_notice import compliance
from service.scheduled_task.change_notice.workflow import (
  _ARTIFACT_PREFIX, collect_events,
)
from service.scheduled_task.report import common as report_common
from service.scheduled_task.report import render as report_render
from service.scheduled_task.report_templates import period_of, window_for

logger = logging.getLogger(__name__)

_TZ = ZoneInfo('Asia/Shanghai')
_SCOPE = '全国'
_TITLE = '变更通知合规周报'


# ---- 窗口 ------------------------------------------------------------------

def week_window(now: datetime | None = None) -> dict:
  """上一个完整自然周：[上周一 00:00, 本周一 00:00)，Asia/Shanghai。"""
  start_date, end_date = window_for('weekly', now)
  start = datetime.combine(start_date, dtime.min, tzinfo=_TZ)
  end = datetime.combine(start_date + timedelta(days=7), dtime.min, tzinfo=_TZ)
  return {
    'start_date': start_date, 'end_date': end_date,
    'start': start, 'end': end,
    'start_s': int(start.timestamp()), 'end_s': int(end.timestamp()),
    'label': f'{start_date.isoformat()} ~ {end_date.isoformat()}',
  }


def _attribution_ts(event: dict) -> int:
  """事件归属时刻：完成通知 > 开始通知 > 消息时间（都取最早那次）。"""
  return int(event.get('end_msg_time') or event.get('start_msg_time')
             or event.get('msg_time') or 0)


def _lookback_days() -> int:
  return max(0, int(getattr(settings, 'CHANGE_COMPLIANCE_LOOKBACK_DAYS', 7) or 0))


# ---- 图表 ------------------------------------------------------------------

async def _draw_charts(ds_dir: Path, result: dict) -> None:
  """沙箱里画两张图（拼接 report/common.py，复用它统一的柱宽/图例/存图助手）。"""
  common_src = Path(report_common.__file__).read_text(encoding='utf-8')
  payload = json.dumps({
    'by_park': result['by_park'],
    'rules': [{'title': r['title'], 'violations': r['violations']}
              for r in result['rules']],
  }, ensure_ascii=False)
  code = (
    common_src + '\nimport json as _json\n'
    + '_plt = rz_setup()\n'
    + f'_d = _json.loads({payload!r})\n'
    + '_colors = _plt.rcParams["axes.prop_cycle"].by_key()["color"]\n'
    # 图1：各园区 合规 / 不合规 分组柱
    + '_bp = _d["by_park"]\n'
    + '_names = [p["park"] for p in _bp]\n'
    + '_xs = list(range(len(_names)))\n'
    + '_w = _bar_width(len(_xs), 2)\n'
    + '_lo, _ro = _bar_offsets(len(_xs), 2)\n'
    + '_fig, _ax = _plt.subplots(figsize=(8.2, 5.2))\n'
    + '_ax.bar([x + _lo for x in _xs], [p["compliant"] for p in _bp], width=_w,'
    +            ' label="合规", color=_colors[1])\n'
    + '_ax.bar([x + _ro for x in _xs], [p["violation"] for p in _bp], width=_w,'
    +            ' label="不合规", color=_colors[2])\n'
    + '_ax.set_xticks(_xs)\n'
    + '_ax.set_xticklabels(_names)\n'
    + '_ax.set_xlabel("园区")\n'
    + '_ax.set_ylabel("变更数")\n'
    + '_legend_below(_ax)\n'
    + "_emit(_fig, 'chart_park', _plt, rz_save)\n"
    # 图2：各规则违规数横向条
    + '_rules = _d["rules"]\n'
    + '_ys = list(range(len(_rules)))\n'
    + '_fig2, _ax2 = _plt.subplots(figsize=(8.2, 4.8))\n'
    + '_ax2.barh(_ys, [r["violations"] for r in _rules], color=_colors[0])\n'
    + '_ax2.set_yticks(_ys)\n'
    + '_ax2.set_yticklabels([r["title"] for r in _rules])\n'
    + '_ax2.set_xlabel("违规数")\n'
    + '_ax2.grid(True, axis="x")\n'
    + '_ax2.grid(False, axis="y")\n'
    + '_ax2.invert_yaxis()\n'
    + "_emit(_fig2, 'chart_rules', _plt, rz_save)\n"
  )
  await report_render.run_in_sandbox(code, ds_dir, label='合规出图')


# ---- HTML ------------------------------------------------------------------

def _rate_text(ok: int, total: int) -> str:
  return f'{ok / total * 100:.1f}%' if total else '—'


def _build_html(result: dict, window: dict, period_label: str,
                generated_at: str) -> str:
  card, table, esc = report_render._card, report_render._table, report_render._e
  total = result['total']
  rule_violation = sum(r['violations'] for r in result['rules'])
  cards = ''.join([
    card('变更总数', str(total), '本周已配对变更数'),
    card('合规率', _rate_text(result['compliant_events'], total),
         f"合规 {result['compliant_events']} 条"),
    card('合规变更', str(result['compliant_events']), '无任何不合规规则'),
    card('违规规则命中', str(rule_violation), '全部规则合计'),
  ])
  rules_rows = [[r['title'], r['violations'], r['undetermined'], r['compliant'],
                 _rate_text(r['compliant'], r['compliant'] + r['violations'])]
                for r in result['rules']]
  park_rows = [[p['park'], p['compliant'], p['violation'],
                _rate_text(p['compliant'], p['compliant'] + p['violation'])]
               for p in result['by_park']]
  detail_rows = [[d.get('园区', ''), d.get('数据中心', ''), d.get('工单号', ''),
                  d.get('维护事项', ''), d.get('规则', ''), d.get('明细', ''),
                  d.get('计划开始', ''), d.get('实际结束', ''), d.get('消息时间', '')]
                 for d in result['detail_rows']]
  appendix = (table(['园区', '数据中心', '工单号', '维护事项', '规则', '明细',
                     '计划开始', '实际结束', '消息时间'], detail_rows)
              if detail_rows else '<p class="note">本周无不合规项。</p>')
  return (
    '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">'
    f'<title>{esc(_TITLE)}</title></head><body>'
    '<div class="cover">'
    f'<h1>{esc(_TITLE)}</h1>'
    f'<p class="subtitle">{esc(_SCOPE)} · {esc(period_label)}</p>'
    f'<p class="meta"><span class="gen">{esc(generated_at)}</span>　｜　'
    f'报告周期：{esc(window["label"])}　｜　共 {total} 条记录</p>'
    '</div>'
    '<div class="section"><h2>1. 统计概览</h2>'
    f'<div class="kpi-grid">{cards}</div>'
    '<p class="note">合规率（事件级）＝无任何不合规规则的事件数 / 总事件数；'
    '无法判定的规则不计入不合规。规则可扩展：每新增一条规则自动进入汇总与附录。</p>'
    '</div>'
    '<div class="section"><h2>2. 园区与规则分布</h2>'
    '<div class="figblock"><div class="figure">图 1　各园区合规情况</div>'
    '<img src="charts/chart_park.png"/></div>'
    '<div class="figblock"><div class="figure">图 2　各规则违规数</div>'
    '<img src="charts/chart_rules.png"/></div>'
    '</div>'
    '<div class="section"><h2>3. 合规率汇总</h2>'
    f'{table(["规则", "违规", "无法判定", "合规", "合规率"], rules_rows)}'
    f'{table(["园区", "合规", "不合规", "合规率"], park_rows)}'
    '</div>'
    '<div class="section appendix"><h2>附录：不合规明细清单</h2>'
    f'{appendix}</div>'
    '</body></html>'
  )


# ---- 通知 / 主流程 ---------------------------------------------------------

async def _notify(task, run, status, final_text: str, error: str) -> None:
  recipients = [task.created_by]
  batch_key = ''
  if status == RunStatus.FAILED and task.created_by == repo.SYSTEM_USER_ID:
    recipients = await repo.alert_user_ids() or [task.created_by]
    batch_key = repo.alert_batch_key()

  if batch_key:
    title = f'{_TITLE}执行失败'
    body = f'「{task.name}」{(final_text or error).strip()[:120]}'
  else:
    title = (f"定时任务「{task.name}」执行"
             f"{'成功' if status == RunStatus.SUCCESS else '失败'}")
    body = (final_text or error).strip()[:80]

  for user_id in recipients:
    await repo.create_notification(
      user_id=user_id, task_id=task.task_id, task_name=task.name, run_id=run.run_id,
      status=status.value, title=title, body=body, batch_key=batch_key,
    )


async def run_change_compliance_task(task) -> None:
  """跑一期变更合规周报。**不抛异常**（成败落运行记录与通知）。"""
  run_id = uuid.uuid4().hex
  thread_id = f'rz-agent-sched-{task.task_id}-{run_id}'
  window = week_window()
  period_key, period_label = period_of(task.template_id)
  run = await repo.create_run(task.task_id, run_id=run_id, thread_id=thread_id,
                              preset_key=task.template_id, period_key=period_key,
                              period_label=period_label)
  start_ts = time.time()
  error, final_text, artifacts = '', '', []
  counts: dict[str, int] | None = None

  try:
    lookback_start_s = window['start_s'] - _lookback_days() * 86400
    events_all = await collect_events(lookback_start_s, window['end_s'])
    week_events = [e for e in events_all
                   if window['start_s'] <= _attribution_ts(e) < window['end_s']]

    if not week_events:
      final_text = '本周无变更记录，未生成合规报告。'
    else:
      cfg = compliance.Cfg(
        late_grace_minutes=int(settings.CHANGE_COMPLIANCE_LATE_START_GRACE_MINUTES),
        completion_diff_minutes=int(settings.CHANGE_COMPLIANCE_COMPLETION_DIFF_MINUTES),
      )
      result = compliance.evaluate_events(week_events, cfg)

      params = {'period': period_key,
                'start': window['start'].isoformat(),
                'end': window['end'].isoformat()}
      info = save_dataset(thread_id, 'change_notice', params,
                          result['detail_rows'], dataset_name='change_compliance')
      dataset_id = info['dataset_id']
      ds_dir = load_dataset_dir(thread_id, dataset_id)

      generated_at = datetime.now(_TZ).strftime('%Y-%m-%d %H:%M')
      (ds_dir / 'report.html').write_text(
        _build_html(result, window, period_label, generated_at), encoding='utf-8')
      await _draw_charts(ds_dir, result)

      pdf_name = f'change_compliance_{period_key}.pdf'
      await report_render.render_pdf(ds_dir, pdf_name)

      artifacts = [f'{_ARTIFACT_PREFIX}{thread_id}/{dataset_id}/{pdf_name}']
      final_text = (f'《{_TITLE}》{period_label}（{window["label"]}）。'
                    f'共 {result["total"]} 条变更，合规率 '
                    f'{_rate_text(result["compliant_events"], result["total"])}，'
                    f'不合规明细 {len(result["detail_rows"])} 项。')
      counts = {r['key']: r['violations'] for r in result['rules']}
  except Exception as e:
    logger.exception('变更合规周报工作流失败 task_id=%s', task.task_id)
    error = f'{type(e).__name__}: {str(e)[:400]}'

  duration_ms = int((time.time() - start_ts) * 1000)
  status = RunStatus.FAILED if error else RunStatus.SUCCESS
  await repo.finish_run(run.run_id, status=status, final_reply=final_text,
                        artifact_urls=artifacts, error=error, duration_ms=duration_ms,
                        thread_id=thread_id, check_counts=counts)
  await _notify(task, run, status, final_text, error)
  logger.info('变更合规周报工作流完成 task_id=%s period=%s status=%s duration_ms=%s',
              task.task_id, period_key, status.value, duration_ms)

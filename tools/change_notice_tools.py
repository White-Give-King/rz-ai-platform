"""每日变更统计工具 —— 园区日常运维群变更通知 → 园区 × 数据中心明细。

解析/配对/分桶全部在 `service/scheduled_task/change_notice/`（与每日定时预设同一套），
对话侧只传日期窗口与交付形态、**不复算**。只统计**人发送**的消息（机器人卡片不计）。
交付形态同 `incident_report`：≤20 行由服务端渲染文字表 + 交互表格卡片，>20 行只给交互表格，
只有明确要 Excel 才落 xlsx（表格一律服务端生成，模型只复制、不自选列）。
"""

import asyncio
import json
import logging
from datetime import datetime, timedelta
from urllib.parse import quote
from zoneinfo import ZoneInfo

from langchain.tools import tool
from langchain_core.runnables.config import RunnableConfig

from core.sandbox.datasets import resolve_artifact_url, save_dataset, thread_dir
from tools.incident_tools import _get_thread_id, _markdown_table, _split_parks

logger = logging.getLogger(__name__)

_TZ = ZoneInfo('Asia/Shanghai')
_WORKSPACE_ID = 'workspace'
_INLINE_MAX_ROWS = 20          # ≤20 条文字表 + 交互表格；>20 条只给交互表格


def _err(message: str) -> str:
  return json.dumps({'error': message}, ensure_ascii=False)


def _parse_day(value: str) -> datetime:
  try:
    return datetime.strptime(value.strip(), '%Y-%m-%d').replace(tzinfo=_TZ)
  except ValueError:
    raise ValueError(f'日期需为 YYYY-MM-DD（收到 {value!r}）')


def _window(date_str: str, start: str, end: str) -> tuple[int, int, str, bool]:
  """变更统计的**自然日**窗口：date=该日整天；start/end=日期区间（含首尾）；都不传=今天。

  第 4 项 `rolling`：窗口含「此刻」被裁到当前时刻（今天/含今天）时为 True，
  调用方据此在 `window_note` 里写明实际截止时刻——两个时刻的合法计数看着才不矛盾。
  """
  if date_str and (start or end):
    raise ValueError('date 与 start/end 不能同时传：指定某天用 date，日期区间用 start/end')
  now = datetime.now(_TZ)
  if date_str:
    start_dt = _parse_day(date_str)
    end_dt = start_dt + timedelta(days=1)
    label = start_dt.strftime('%Y-%m-%d')
  elif start or end:
    first = _parse_day(start or end)
    last = _parse_day(end or start)
    if last < first:
      raise ValueError('end 必须不早于 start')
    start_dt = first
    end_dt = last + timedelta(days=1)
    label = (first.strftime('%Y-%m-%d') if first == last
             else f'{first:%Y-%m-%d} ~ {last:%Y-%m-%d}')
  else:
    start_dt = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end_dt = now
    label = f'{start_dt:%Y-%m-%d}（今天）'
  # 今天只算到当前时刻：未来时间戳同步不到、查询也没有意义
  rolling = False
  if start_dt <= now < end_dt:
    end_dt = now
    rolling = True
  return int(start_dt.timestamp()), int(end_dt.timestamp()), label, rolling


def _configured_parks() -> list[str]:
  from service.scheduled_task.change_notice import config as notice_config

  return notice_config.configured_parks()


def _resolve_parks(value: str) -> tuple[list[str], list[str]]:
  """请求里的园区名 → 配置 key；返回 (命中, 未命中)。空请求 = 全部已配置园区。"""
  configured = _configured_parks()
  requested = _split_parks(value)
  if not requested:
    return configured, []
  hit: list[str] = []
  missing: list[str] = []
  for name in requested:
    found = next((c for c in configured if c in (name, f'{name}园区')), '')
    if not found:
      cands = [c for c in configured if name.startswith(c) or c.startswith(name)]
      found = cands[0] if len(cands) == 1 else ''
    if not found:
      missing.append(name)
    elif found not in hit:
      hit.append(found)
  return hit, missing


@tool
async def change_notice_report(
  date: str = '',
  start: str = '',
  end: str = '',
  parks: str = '',
  sync: bool = True,
  export_excel: bool = False,
  config: RunnableConfig = None,
) -> str:
  """统计园区日常运维群里的**变更通知**（园区 × 数据中心：变更类型 / 维护事项 / 执行状态）。

  数据来自飞书群消息（人发送的通知，**机器人卡片不计**），按**自然日**统计，
  由平台确定性解析与配对，**不要**自己用 analysis_load 复算，也不要把计划时间换算成别的口径。
  用户说「昨天 / 今天 / 某天」时传 `date`；说「11 月 17 到 19 日」时传 `start`+`end`；
  什么都不说时留空（平台默认今天）。

  **今天 / 含今天的窗口按「到此刻」的滚动窗口统计**：同一问题在不同时刻问，条数会随新增消息变化。
  回答开头**必须复述 `window_note`**（写明实际截止时刻），否则用户看到两个数字会以为统计错了。
  汇总与明细**一律用本工具返回的 `summary_markdown`/`table_markdown`**，**不要**改用
  `group_message_search`（只查应急表）或 `fetch_group_messages`（原始消息）自己重算。

  返回里的 `mode` 决定怎么交付：
  - `inline`（≤20 条）：先给 `summary_markdown` 汇总，再**原样输出 `table_markdown`**（服务端已按固定列渲染）
    与 `file_tag`；只在表格前后各加一两句结论，不要自己重排/删列/另造表；
  - `records`（>20 条）：先原样输出 `summary_markdown` 汇总，再输出 `file_tag` 打开交互表格，
    补一两句结论；**不要**因为没文字明细就去拉原始群消息；
  - `export`（用户明确要 Excel）：原样输出 `file_tag`；
  - `empty`：按 `note` 说明当天没有变更记录。
  说「变更统计 / 统计一下 / 表格」都不是要文件，只有明确说 Excel/导出/下载才传 `export_excel=true`。

  Args:
    date: `YYYY-MM-DD`，统计该自然日。不传 = 今天。
    start: 起始日期 `YYYY-MM-DD`（含当天），与 end 搭配表示区间；不能与 date 同时传。
    end: 结束日期 `YYYY-MM-DD`（含当天）。只传 start 或只传 end 时按单日处理。
    parks: 园区名，逗号分隔（如「廊坊」或「廊坊,平湖AB」）。留空 = 所有已配置园区群。
    sync: 是否先从飞书群补拉该窗口的消息（默认 true，需要机器人已在群里）。
    export_excel: 是否产出 xlsx（默认 false）。仅用户明确要 Excel/文件/导出时置 true。
  """
  try:
    start_s, end_s, window_label, rolling = _window(date, start, end)
  except ValueError as e:
    return _err(str(e))

  park_list, missing = _resolve_parks(parks)
  available = '、'.join(_configured_parks()) or '（未配置）'
  if missing:
    return _err(f'园区未配置日常运维群：{"、".join(missing)}；当前可用：{available}')
  if not park_list:
    return _err('未配置日常运维群（CHANGE_NOTICE_FEISHU_PARK_CHATS 为空），无法统计；'
                '请先让机器人进群并在部署配置里登记。')

  from service.scheduled_task.change_notice import build, workflow, xlsx

  sync_error = ''
  if sync:
    try:
      await workflow.sync_window(start_s, end_s, park_list)
    except Exception as e:
      # 拉群失败不整单失败：按库里已有消息出表，把原因带回给模型说明
      sync_error = f'{type(e).__name__}: {str(e)[:200]}'
      logger.warning('[ChangeNotice] 对话出表同步失败（按库内数据继续）: %s', e)

  try:
    events = await workflow.collect_events(start_s, end_s, park_list)
  except Exception as e:
    logger.exception('[ChangeNotice] 对话出表取数失败')
    return _err(f'生成失败：{type(e).__name__}: {str(e)[:200]}')

  rows = build.to_rows(events)
  groups = build.overview(events)
  if rolling:
    window_note = (f"本次统计 {datetime.fromtimestamp(start_s, _TZ):%Y-%m-%d %H:%M} ~ "
                   f"{datetime.fromtimestamp(end_s, _TZ):%H:%M}（含今天，按到此刻的滚动窗口，"
                   f'实时新增会影响条数）')
  else:
    window_note = f'本次统计 {window_label}（自然日全天）'
  thread_id = _get_thread_id(config)
  result: dict = {
    'window': window_label,
    'window_note': window_note,
    'window_basis': '按消息发送时间归属的自然日',
    'parks': park_list,
    'rows': len(rows),
    'overview': [{'park': g['park'], 'total': g['total'],
                  'by_type': {k: v for k, v in g['by_type'].items() if v},
                  'by_status': {k: v for k, v in g['by_status'].items() if v}}
                 for g in groups],
  }

  if export_excel:
    name = f'change_notice_{datetime.fromtimestamp(start_s, _TZ):%Y%m%d}.xlsx'
    out_path = thread_dir(thread_id) / _WORKSPACE_ID / name
    try:
      await asyncio.to_thread(xlsx.write_workbook, out_path, rows, groups,
                              title='变更统计明细', period=window_label)
    except Exception as e:
      logger.exception('[ChangeNotice] 对话出表写 xlsx 失败')
      return _err(f'生成失败（写文件）：{type(e).__name__}: {str(e)[:200]}')
    url = resolve_artifact_url(thread_id, name)
    if not url:
      return _err('生成失败：产物落盘后在会话目录里找不到（文件名或目录异常）')
    result.update(mode='export', url=url, file_name=name,
                  file_tag=f'<file url="{url}" name="{name}"/>')
  elif not rows:
    result.update(mode='empty', note=f'{window_label} 各园区日常运维群没有变更通知记录。')
  else:
    info = await asyncio.to_thread(
      save_dataset, thread_id, 'change_notice',
      {'date': date, 'start': start, 'end': end, 'parks': park_list},
      rows, 'change-notice',
    )
    records_url = (f'/api/v1/chat/agent/artifacts/{quote(thread_id, safe="")}/'
                   f'{quote(info["dataset_id"], safe="")}/records')
    file_tag = f'<file url="{records_url}" name="变更明细"/>'
    summary = build.summary_markdown(groups)
    if len(rows) > _INLINE_MAX_ROWS:
      result.update(mode='records', summary_markdown=summary,
                    records_url=records_url, file_tag=file_tag)
    else:
      result.update(mode='inline', summary_markdown=summary,
                    table_markdown=_markdown_table(list(build.COLUMNS), rows),
                    records_url=records_url, file_tag=file_tag)

  if sync:
    result['synced'] = True
  if sync_error:
    result['sync_error'] = sync_error
  return json.dumps(result, ensure_ascii=False)

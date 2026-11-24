"""本机核对每日变更统计（默认只读 DB；`--sync` 才回补群消息）。

用法:
  python -m service.scheduled_task.change_notice.cli --date 2026-11-19
  python -m service.scheduled_task.change_notice.cli --date 2026-11-19 --sync
  python -m service.scheduled_task.change_notice.cli --start 2026-11-18 --end 2026-11-19 \
      --parks 廊坊,平湖AB --out /tmp/change.xlsx
"""

import argparse
import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from service.scheduled_task.change_notice import build, workflow, xlsx

_TZ = ZoneInfo('Asia/Shanghai')


def _parse_day(text: str) -> datetime:
  return datetime.strptime(text, '%Y-%m-%d').replace(tzinfo=_TZ)


def _window(args) -> dict:
  if args.start or args.end:
    start = _parse_day(args.start or args.end)
    end = _parse_day(args.end or args.start) if args.end else start
    end = end + timedelta(days=1)
  elif args.date:
    start = _parse_day(args.date)
    end = start + timedelta(days=1)
  else:
    return workflow.today_window()
  return {
    'start': start, 'end': end,
    'start_s': int(start.timestamp()), 'end_s': int(end.timestamp()),
    'key': start.strftime('%Y-%m-%d'), 'label': start.strftime('%Y-%m-%d'),
  }


def _print(rows: list[dict], groups: list[dict]) -> None:
  print(f'共 {len(rows)} 条')
  for group in groups:
    print(f"  {group['park']}: {group['total']} "
          f"({', '.join(f'{k} {v}' for k, v in group['by_type'].items() if v)})")
  for row in rows:
    print(f"- [{row['园区']}/{row['数据中心']}] {row['维护事项']} · {row['变更类型']} · "
          f"{row['执行状态']} · {row['工单号'] or '-'} · 负责人 {row['现场负责人'] or '-'}"
          f"（{row['现场负责人等级'] or '-'}） · 计划 {row['计划开始'] or '-'}")


async def _collect(args, window: dict, parks: list[str]) -> list[dict]:
  if args.sync:
    result = await workflow.sync_window(window['start_s'], window['end_s'], parks)
    print(f"回补：写入 {result['saved']} 条，撤回 {result['recalled']} 条，"
          f"失败园区 {result['failed']}")
  return await workflow.collect_events(window['start_s'], window['end_s'], parks or None)


def main() -> None:
  parser = argparse.ArgumentParser(description='每日变更统计（本机核对）')
  parser.add_argument('--date', default='', help='自然日 YYYY-MM-DD（缺省今天）')
  parser.add_argument('--start', default='', help='区间起始 YYYY-MM-DD')
  parser.add_argument('--end', default='', help='区间结束 YYYY-MM-DD（含当天）')
  parser.add_argument('--parks', default='', help='园区多选，逗号/顿号分隔')
  parser.add_argument('--sync', action='store_true', help='先回补群消息（默认只读 DB）')
  parser.add_argument('--out', default='', help='xlsx 输出路径')
  args = parser.parse_args()

  parks = [p.strip() for p in (args.parks or '').replace('、', ',').split(',') if p.strip()]
  window = _window(args)

  events = asyncio.run(_collect(args, window, parks))
  rows = build.to_rows(events)
  groups = build.overview(events)
  _print(rows, groups)

  if args.out:
    xlsx.write_workbook(args.out, rows, groups, title='变更统计明细', period=window['label'])
    print(f'已写出 {args.out}')


if __name__ == '__main__':
  main()

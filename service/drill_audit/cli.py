"""本机核对脚本：定位演练周 + 预览班组 PDF / EOP 命中，`--yes` 才真跑完整判定。

用法：
  python -m service.drill_audit.cli --dept 23CI1EiZnff --year 2026 --plan-name 制冷
  python -m service.drill_audit.cli --dept 23CI1EiZnff --year 2026 --plan-name 制冷 --yes
  python -m service.drill_audit.cli --store-stats
"""

import argparse
import asyncio
import json
import sys

from . import pipeline, retrieve, store
from .codes import (
  FINISH_STATUS, group_file, normalize_groups, speciality_hint, speciality_name,
)


async def _main_async(args) -> int:
  if args.store_stats:
    print(json.dumps(await store.stats(), ensure_ascii=False, indent=1))
    return 0

  if args.weekly_dry_run or args.weekly:
    from . import weekly

    if args.weekly_dry_run:
      result = await weekly.run_weekly_audit(dry_run=True)
      print(f'本期候选演练周: {result["weeks"]} 个')
      for cand in result.get('candidates') or []:
        print(f'  - {cand["dept_name"]} · {cand["plan_name"]} 第 {cand["week"]} 周'
              f'（{cand["stime"]} ~ {cand["etime"]}）')
      for err in result.get('errors') or []:
        print(f'  ! {err}')
      print('\n[dry-run] 未下载、未 OCR、未判定。')
      return 0

    if not args.yes:
      print('每周审查会下载 + OCR + 判定，确认后加 --yes。')
      return 1

    async def _weekly_progress(message: str) -> None:
      print(f'  · {message}')

    result = await weekly.run_weekly_audit(progress=_weekly_progress,
                                           refresh=args.refresh)
    print(f'\n候选 {result["weeks"]} 周，审查 {result["audited"]} 周，'
          f'跳过 {result["skipped"]} 周，问题 {result["issues_total"]} 项')
    for err in result.get('errors') or []:
      print(f'  ! {err}')
    print(result.get('summary') or '')
    return 0

  year = int(args.year)
  plan, week_row, detail = await pipeline._resolve_plan_week(
    args.plan_name, args.week_id, args.dept, year, int(args.week), None,
  )
  name = str(detail.get('drillPlanName') or plan.get('name') or '')
  print(f'计划: {name}')
  print(f'专业: {speciality_name(str(plan.get("drillSpecialityId") or "")) or "-"}'
        f' | 部门: {plan.get("deptName") or args.dept}')
  finish = FINISH_STATUS.get(str(detail.get('finishStatus') or ''), '-')
  print(f'周次: 第 {detail.get("week")} 周 ({detail.get("stime")} ~ {detail.get("etime")})'
        f' | 完成状态: {finish} | 记录: {detail.get("fileStatus") or "-"}')

  letters = normalize_groups(args.groups)
  plan_files = []
  for letter in letters:
    fid, fname = group_file(detail, letter)
    print(f'  {letter}班组: {"有记录 " + fname if fid else "无记录"}')
    if fid:
      plan_files.append((letter, fid))

  query = speciality_hint(name)
  print(f'\nEOP 检索词: {query}')
  try:
    eop_docs = await retrieve.find_eop(query)
  except retrieve.EopRetrievalError as e:
    print(f'EOP 检索不可用: {e}')
  else:
    if not eop_docs:
      print('EOP 命中: 无（知识库可访问但没有相关文档）')
    for doc in eop_docs:
      print(f'  - {doc.get("file_name")}  score={doc.get("score")}')

  if not args.yes:
    print('\n[dry-run] 未下载、未 OCR、未调用判定模型；确认无误后加 --yes 跑完整审查。')
    return 0

  if not plan_files:
    print('\n所选周没有任何班组记录文件，无法审查。')
    return 1

  async def _progress(message: str) -> None:
    print(f'  · {message}')

  result = await pipeline.run_audit(
    plan_name=args.plan_name, week_id=args.week_id, dept_id=args.dept,
    year=year, week=int(args.week), groups=args.groups, user_id=None,
    progress=_progress, refresh=args.refresh, store_max_mb=args.store_max_mb,
  )
  print('\n=== 审查结果 ===')
  print(json.dumps(result, ensure_ascii=False, indent=1))
  return 0


def main(argv: list[str] | None = None) -> int:
  parser = argparse.ArgumentParser(description='演练记录合规审查（核对脚本）')
  parser.add_argument('--dept', default='', help='部门 id（如 23CI1EiZnff）')
  parser.add_argument('--year', default='2026', help='年份（默认 2026）')
  parser.add_argument('--plan-name', default='', help='计划名称子串')
  parser.add_argument('--week', type=int, default=0,
                      help='周号；缺省取最近一个已完成/有记录的已安排周')
  parser.add_argument('--week-id', default='', help='直接指定演练周 id（跳过计划定位）')
  parser.add_argument('--groups', default='', help='限定班组，如 A,B；缺省四个都查')
  parser.add_argument('--yes', action='store_true', help='真正下载+OCR+判定')
  parser.add_argument('--refresh', action='store_true',
                      help='绕过内容库，重下重解析（怀疑上游记录已更新时用）')
  parser.add_argument('--store-max-mb', type=int, default=None,
                      help='临时覆盖内容库容量上限（MB），用于验证淘汰')
  parser.add_argument('--store-stats', action='store_true',
                      help='只打印内容库统计（文件数 / 占用 / 可命中数）')
  parser.add_argument('--weekly-dry-run', action='store_true',
                      help='只列出本周会审哪些演练周，不下载不 OCR 不判定')
  parser.add_argument('--weekly', action='store_true',
                      help='真跑一轮每周审查（需配 --yes）')
  args = parser.parse_args(argv)
  if (not args.store_stats and not args.weekly_dry_run and not args.weekly
      and not args.dept and not args.week_id):
    parser.error('--dept 或 --week-id 至少给一个（或用 --store-stats / --weekly-dry-run）')
  return asyncio.run(_main_async(args))


if __name__ == '__main__':
  sys.exit(main())

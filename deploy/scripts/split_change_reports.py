#!/usr/bin/env python3
"""把误写进应急通报表的日常运维群消息搬到 `dcim_change_reports`（默认 dry-run）。

背景：变更报文物理分表前，日常运维群的消息与应急通报共用 `dcim_incident_reports`，
带「通报/通告」的变更报文被判成 `kind='first'`、`cluster=NULL`，永远配不上告警却
卡在匹配队列队首。修复后入口按群身份路由，本脚本只清理**历史遗留**行。

口径：
  - 目标行的 chat_id 集合取自 `CHANGE_NOTICE_FEISHU_PARK_CHATS`，并排除同时配置在
    应急群里的 id（重叠时按应急处理，那些行不能搬走）。
  - 行内容原样搬运（含当时的 kind/title/text/raw），不改写、不重新解析。
  - 只按 chat_id 集合动作，绝不碰其它 chat_id 的行、也不动匹配结果。

用法（在 backend 容器里跑；先停 dcim-ingest 更稳妥）：

    docker cp deploy/scripts/split_change_reports.py rz-backend:/tmp/
    docker exec -w /app rz-backend python /tmp/split_change_reports.py        # dry-run
    docker exec -w /app rz-backend python /tmp/split_change_reports.py --yes  # 真搬

退出码：0 正常（dry-run 也返回 0）；1 配置不可用（PG DSN 缺失 / 集合为空仍强搬）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 既支持「仓库内直接跑」，也支持 docker cp 到 /tmp 后用 `-w /app` 跑。
for _root in (Path.cwd(), Path(__file__).resolve().parent.parent.parent):
  if (_root / 'service').is_dir() and str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

import psycopg  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402

from config.settings import settings  # noqa: E402
from service.scheduled_task.incident_stats import feishu, schema  # noqa: E402

_SOURCE = 'dcim_incident_reports'
_TARGET = 'dcim_change_reports'
_DELETE_BATCH = 5000

_COUNT_SQL = (
  f'SELECT chat_id, kind, count(*) AS n FROM {_SOURCE}'
  '  WHERE chat_id = ANY(%s) GROUP BY chat_id, kind ORDER BY chat_id, kind'
)
_TOTAL_SQL = f'SELECT count(*) AS n FROM {_SOURCE} WHERE chat_id = ANY(%s)'
_INSERT_SQL = (
  f'INSERT INTO {_TARGET}'
  ' (chat_id, message_id, kind, title, create_time, text, raw,'
  '  sender, sender_id, sender_id_type, ingested_at)'
  ' SELECT chat_id, message_id, kind, title, create_time, text, raw,'
  '        sender, sender_id, sender_id_type, ingested_at'
  f'  FROM {_SOURCE} WHERE chat_id = ANY(%s)'
  ' ON CONFLICT (chat_id, message_id) DO NOTHING'
)
_DELETE_SQL = (
  f'DELETE FROM {_SOURCE} WHERE ctid IN ('
  f'  SELECT ctid FROM {_SOURCE} WHERE chat_id = ANY(%s) LIMIT %s)'
)


def _target_chats() -> tuple[list[str], dict[str, str]]:
  """→ (要搬的 chat_id 列表, {chat_id: 园区})；重叠的应急群已在 `is_change_chat` 排除。"""
  mapping = feishu.change_park_chats()
  chats = sorted(c for c in mapping.values() if feishu.is_change_chat(c))
  chat_to_park = {c: p for p, c in mapping.items() if c in chats}
  return chats, chat_to_park


def main() -> int:
  ap = argparse.ArgumentParser(description='搬迁误入库的日常运维群变更消息（默认 dry-run）')
  ap.add_argument('--yes', action='store_true', help='真正执行搬迁（默认只打印计划）')
  args = ap.parse_args()

  if not settings.DCIM_PG_DSN:
    print('! DCIM_PG_DSN 未配置')
    return 1
  chats, chat_to_park = _target_chats()
  print(f"变更群搬迁（{'APPLY' if args.yes else 'DRY-RUN'}）：{len(chats)} 个群")
  for chat in chats:
    print(f'  {chat}  ({chat_to_park.get(chat, "")})')
  if not chats:
    print('\n没有可搬迁的群（CHANGE_NOTICE_FEISHU_PARK_CHATS 为空或全部与应急群重叠），'
          '无事可做。')
    return 0

  with psycopg.connect(settings.DCIM_PG_DSN, row_factory=dict_row) as conn:
    with conn.cursor() as cur:
      cur.execute(_TOTAL_SQL, (chats,))
      total = int(cur.fetchone()['n'])
      cur.execute(_COUNT_SQL, (chats,))
      dist = cur.fetchall()

    print(f'\n{_SOURCE} 中待搬运：{total} 行')
    for row in dist:
      print(f"  {row['chat_id']}  kind={row['kind']:<6}{int(row['n']):>8} 行")
    if total == 0:
      print('\n没有历史遗留行，无需搬迁。')
      return 0

    if not args.yes:
      conn.rollback()
      print('\nDRY-RUN：未改动任何数据。确认无误后加 --yes 执行。')
      return 0

    # 先确保目标表存在（实时入库/工作流从未跑过时可能还没建）
    conn.rollback()
    schema.ensure_change_schema(settings.DCIM_PG_DSN)
    with conn.cursor() as cur:
      cur.execute(_INSERT_SQL, (chats,))
      inserted = cur.rowcount
      conn.commit()
      print(f'\n已写入 {_TARGET}：{max(0, inserted)} 行（冲突行跳过）')

      deleted = 0
      while True:
        cur.execute(_DELETE_SQL, (chats, _DELETE_BATCH))
        n = cur.rowcount
        if not n:
          break
        deleted += n
        conn.commit()
        print(f'  删除 {_SOURCE} 行：本批 {n}，累计 {deleted}')
    print(f'\n完成：写入 {_TARGET} {max(0, inserted)} 行，'
          f'从 {_SOURCE} 删除 {deleted} 行。')
  return 0


if __name__ == '__main__':
  sys.exit(main())

"""日常运维群的变更消息表（`dcim_change_reports`）读写。

与应急通报表 `dcim_incident_reports` **物理分开**：入口（`realtime.py`）按群身份路由，
变更群的消息只落本表，因此不参与告警匹配、不进事件统计。表里只留在库需要的字段，
`kind` 保留 change / other 供排查与检索。读时解析、不固化快照（同事件统计表）。
"""

import json
import logging

from config.settings import settings

logger = logging.getLogger(__name__)

TABLE = 'dcim_change_reports'

_MESSAGES_SQL = (
  'SELECT id, chat_id, message_id, kind, title, create_time, text,'
  '       sender, sender_id, sender_id_type'
  f'  FROM {TABLE}'
  ' WHERE create_time >= %s AND create_time < %s'
)

_STATS_SQL = (
  'SELECT count(*) AS n, COALESCE(max(id), 0) AS max_id'
  f'  FROM {TABLE}'
  ' WHERE create_time >= %s AND create_time < %s'
)

_UPSERT_SQL = (
  f'INSERT INTO {TABLE}'
  ' (chat_id, message_id, kind, title, create_time, text, raw,'
  '  sender, sender_id, sender_id_type)'
  ' VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)'
  ' ON CONFLICT (chat_id, message_id) DO UPDATE SET'
  '   kind = EXCLUDED.kind, title = EXCLUDED.title, text = EXCLUDED.text,'
  '   raw = EXCLUDED.raw,'
  # sender 空值不覆盖：实时推送只拿得到 open_id（通讯录缺权限/离职时姓名为空）
  "   sender = COALESCE(NULLIF(EXCLUDED.sender, ''), dcim_change_reports.sender),"
  '   sender_id = EXCLUDED.sender_id, sender_id_type = EXCLUDED.sender_id_type'
  ' RETURNING (xmax = 0) AS is_new'
)

_DELETE_SQL = f'DELETE FROM {TABLE} WHERE chat_id = %s AND message_id = %s'


def _repo():
  from service.dcim_ingest.repository import DcimRepository

  if not settings.DCIM_PG_DSN:
    raise RuntimeError('DCIM_PG_DSN 未配置，无法读写变更通报表')
  return DcimRepository(settings.DCIM_PG_DSN)


def load_messages(start_s: int, end_s: int,
                  chat_ids: list[str] | None = None) -> list[dict]:
  """窗口内全部变更群消息（按消息时间升序）。

  不带 kind 过滤：变更统计自己解析正文，普通消息（值班名单/天气）由解析丢弃。
  """
  sql = _MESSAGES_SQL
  args: list = [start_s, end_s]
  if chat_ids:
    sql += ' AND chat_id = ANY(%s)'
    args.append(list(chat_ids))
  sql += ' ORDER BY create_time'
  repo = _repo()
  try:
    with repo.conn.cursor() as cur:
      cur.execute(sql, args)
      return list(cur.fetchall())
  finally:
    repo.close()


def message_stats(start_s: int, end_s: int,
                  chat_ids: list[str] | None = None) -> tuple[int, int]:
  """窗口内消息数与最大自增 id（作结果缓存的水位）。

  新增（含回补）与撤回删行都会改变其中一项 —— 缓存键因此自动失效，
  不需要额外的「刷新」参数，也不会把过期结果当新结果返回。
  """
  sql = _STATS_SQL
  args: list = [start_s, end_s]
  if chat_ids:
    sql += ' AND chat_id = ANY(%s)'
    args.append(list(chat_ids))
  repo = _repo()
  try:
    with repo.conn.cursor() as cur:
      cur.execute(sql, args)
      row = cur.fetchone()
      return (int(row['n'] or 0), int(row['max_id'] or 0)) if row else (0, 0)
  finally:
    repo.close()


def save_reports(rows: list[dict]) -> int:
  """批量 upsert（幂等：同 `(chat_id, message_id)` 不重复写入）。返回新插入行数。"""
  if not rows:
    return 0

  repo = _repo()
  inserted = 0
  try:
    with repo.conn.cursor() as cur:
      for r in rows:
        cur.execute(_UPSERT_SQL, (
          r['chat_id'], r['message_id'], r['kind'], r.get('title'),
          r['create_time'], r.get('text'),
          json.dumps(r.get('raw'), ensure_ascii=False) if r.get('raw') is not None else None,
          r.get('sender'), r.get('sender_id'), r.get('sender_id_type'),
        ))
        row = cur.fetchone()
        inserted += 1 if row and row['is_new'] else 0
    repo.conn.commit()
  finally:
    repo.close()
  return inserted


def delete_report(chat_id: str, message_id: str) -> int:
  """按幂等键物理删除一行（撤回消息当作没发过）。返回删除行数。"""
  repo = _repo()
  try:
    with repo.conn.cursor() as cur:
      cur.execute(_DELETE_SQL, (chat_id, message_id))
      deleted = cur.rowcount
    repo.conn.commit()
  finally:
    repo.close()
  return max(0, int(deleted or 0))

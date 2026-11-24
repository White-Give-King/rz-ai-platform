#!/usr/bin/env python3
"""把某个消费组在指定园区的位移重置到「最新 / 最早 / 指定时刻」（**默认 dry-run**）。

用途：新园区首次接入时，topic 里往往已经堆了十几万条历史消息，而消费组从未提交过位移
（`committed = -1001`）+ `auto.offset.reset=earliest` ⇒ 每次重启都从头重放。带大量新
设备类型的宽表写入又慢，缓冲顶满内存就 OOM，形成「重放 → 写不完 → OOM → 重启 → 再重放」
的死循环（2026-11-13 惠州）。本脚本把位移直接落到指定位置，历史数据另走离线补数。

    # 先停 worker（避免它同时提交），再 dry-run 看要跳过多少条
    docker stop rz-dcim-ingest
    docker run --rm --env-file deploy/config/.env.production -v "$PWD:/app" -w /app \
      rz-ai-backend:latest python deploy/scripts/dcim_reset_offsets.py \
      --clusters 惠州A2M1,惠州A2M2,惠州A3M1,惠州A3M2

    # 确认无误后加 --yes 真正提交
    ... python deploy/scripts/dcim_reset_offsets.py --clusters ... --yes

    # 回滚到最早（保留期内可重放）
    ... --to earliest --yes

⚠️ `--to latest` 会**跳过**当前积压的全部消息（dry-run 会打印条数）。默认 dry-run。
退出码：全部成功 → 0；任一园区失败 → 1。
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# 直接 `python deploy/scripts/...` 时 sys.path[0] 是脚本目录，import config 会失败
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from confluent_kafka import Consumer, TopicPartition  # noqa: E402

from config.settings import settings  # noqa: E402
from service.dcim_ingest.config import ClusterConfigError, clusters, consumer_config  # noqa: E402

_TZ = ZoneInfo('Asia/Shanghai')


def _parse_to(value: str) -> tuple[str, int]:
  """`--to` → ('latest'|'earliest'|'time', ts_ms)。"""
  text = (value or '').strip().lower()
  if text in ('latest', 'earliest'):
    return text, 0
  try:
    num = int(text)
  except ValueError:
    pass
  else:
    # 秒级（< 1e11）自动换算成毫秒，免得把 1730000000 当成 1970 年
    return 'time', num if num >= 10 ** 11 else num * 1000
  for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S', '%Y-%m-%d'):
    try:
      return 'time', int(datetime.strptime(text, fmt).replace(tzinfo=_TZ).timestamp() * 1000)
    except ValueError:
      continue
  raise SystemExit(f'--to 无法解析：{value}（可用 latest / earliest / epoch / YYYY-MM-DD HH:MM:SS）')


def _targets(consumer: Consumer, topic: str, parts: list[int], mode: str, ts_ms: int) -> dict:
  """算出每个分区的目标位移，同时返回 low/high 供打印。"""
  marks: dict[int, tuple[int, int]] = {}
  for p in parts:
    low, high = consumer.get_watermark_offsets(TopicPartition(topic, p), timeout=15)
    marks[p] = (low, high)

  out: dict[int, int] = {}
  if mode == 'latest':
    out = {p: marks[p][1] for p in parts}
  elif mode == 'earliest':
    out = {p: marks[p][0] for p in parts}
  else:
    stamped = consumer.offsets_for_times(
      [TopicPartition(topic, p, ts_ms) for p in parts], timeout=15,
    )
    for tp in stamped:
      # -1 = 该时刻之后没有消息 → 落到末尾（与「从此刻开始」语义一致）
      out[tp.partition] = tp.offset if tp.offset >= 0 else marks[tp.partition][1]
  return {'target': out, 'marks': marks}


def _join(consumer: Consumer, topic: str, parts: list[int], timeout: float = 30.0) -> None:
  """以组员身份加入消费组，直到全部分区都分配给我方。

  必须真入组再提交：该 broker 拒绝非成员提交（`assign()` 后 `memberid()=None` →
  `UNKNOWN_MEMBER_ID`）。所以执行前一定要停 worker，否则会与它抢分区（两边都拿不全）。
  """
  consumer.subscribe([topic])
  deadline = time.time() + timeout
  while time.time() < deadline:
    consumer.poll(0.5)
    if {tp.partition for tp in consumer.assignment() if tp.topic == topic} >= set(parts):
      return
  raise RuntimeError(
    f'{timeout:.0f}s 内未取得全部分区（先停 worker 再执行；分区被别的成员持有）')


def reset_cluster(spec, topic: str, group: str, mode: str, ts_ms: int, execute: bool) -> bool:
  conf = consumer_config(spec, group_id=group)
  conf['enable.auto.commit'] = False
  consumer = Consumer(conf)
  try:
    meta = consumer.list_topics(topic, timeout=15)
    if topic not in meta.topics:
      print(f'  [{spec.cluster}] topic 不存在：{topic}')
      return False
    parts = sorted(meta.topics[topic].partitions)
    if execute:
      _join(consumer, topic, parts)
    else:
      consumer.assign([TopicPartition(topic, p) for p in parts])
    committed = {tp.partition: tp.offset for tp in consumer.committed(
      [TopicPartition(topic, p) for p in parts], timeout=15,
    )}
    calc = _targets(consumer, topic, parts, mode, ts_ms)

    print(f'  [{spec.cluster}] topic={topic} group={group} 分区 {len(parts)} 个')
    total_skip = 0
    for p in parts:
      low, high = calc['marks'][p]
      cur = committed.get(p, -1)
      target = calc['target'][p]
      base = cur if cur >= 0 else low
      skip = max(0, target - base)
      total_skip += skip
      print(f'    p{p}: committed={cur if cur >= 0 else "-"} '
            f'low={low} high={high} → target={target}（跳过 {skip} 条）')

    if not execute:
      print(f'    dry-run：将跳过合计 {total_skip} 条（加 --yes 才会真正提交）')
      return True

    offsets = [TopicPartition(topic, p, calc['target'][p]) for p in parts]
    consumer.commit(offsets=offsets, asynchronous=False)
    print(f'    已提交 {len(offsets)} 个分区的位移（跳过合计 {total_skip} 条）')
    return True
  finally:
    consumer.close()


def main() -> int:
  parser = argparse.ArgumentParser(description='重置 DCIM 消费组位移（默认 dry-run）')
  parser.add_argument('--clusters', required=True,
                      help='园区名，逗号分隔（如 惠州A2M1,惠州A2M2）')
  parser.add_argument('--topic', default=settings.KAFKA_TOPIC_REAL_DATA,
                      help=f'默认 {settings.KAFKA_TOPIC_REAL_DATA}')
  parser.add_argument('--group', default=settings.KAFKA_REAL_DATA_GROUP_ID,
                      help=f'默认 {settings.KAFKA_REAL_DATA_GROUP_ID}')
  parser.add_argument('--to', default='latest',
                      help='latest（默认）/ earliest / epoch(秒或毫秒) / YYYY-MM-DD HH:MM:SS')
  parser.add_argument('--yes', action='store_true', help='真正提交；不加只打印')
  args = parser.parse_args()

  mode, ts_ms = _parse_to(args.to)
  try:
    known = {c.cluster: c for c in clusters()}
  except ClusterConfigError as e:
    print(f'集群清单不可用：{e}')
    return 1

  wanted = [x.strip() for x in args.clusters.split(',') if x.strip()]
  unknown = [x for x in wanted if x not in known]
  if unknown:
    print(f'未知园区：{"、".join(unknown)}；可用：{"、".join(known)}')
    return 1

  print(f'目标位移：{mode}' + (f'（ts={ts_ms}）' if mode == 'time' else ''))
  if not args.yes:
    print('模式：dry-run（不会修改任何位移）')
  print('提示：执行前请先停掉 rz-dcim-ingest，避免它同时写入位移。')

  failed = [name for name in wanted
            if not reset_cluster(known[name], args.topic, args.group, mode, ts_ms, args.yes)]
  if failed:
    print('失败：' + '、'.join(failed))
    return 1
  print('完成。' + ('可重启 rz-dcim-ingest。' if args.yes else '确认无误后加 --yes 再执行。'))
  return 0


if __name__ == '__main__':
  sys.exit(main())

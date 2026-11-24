import { Loader2 } from 'lucide-react';
import { cn } from '@/lib/utils';
import type { DocUpdateProgress } from '@/api/scheduledTasks';

/** 检查进度条：合规页卡片（compact）与整屏看板共用，避免两处文案/色值漂移。
 *  done/total 是后端 Redis 租约里的步数（每部门 = 遍历 25 步 + 逐项 25 步），
 *  页面 3s 轮询刷新。**遍历阶段（phase='walk'）同样推进百分比** ——
 *  一个部门要遍历 8~10 分钟，若此时不显示百分比，用户看到的就是「没有进度」。
 *  只有刚抢到租约、还拿不到 total 时才渲染不确定进度（滑动光带）。
 *  `progress.cancelling`（已点终止）时主文案换成「正在终止…」，百分比照旧。 */
export function DocUpdateProgressBar({ progress, compact = false }: {
  progress: DocUpdateProgress | null;
  compact?: boolean;
}) {
  const total = progress?.total ?? 0;
  const done = Math.min(progress?.done ?? 0, total || (progress?.done ?? 0));
  const pct = total > 0 ? Math.round((done / total) * 100) : 0;
  const walking = progress?.phase === 'walk';
  const cancelling = Boolean(progress?.cancelling);
  const unknown = total <= 0;
  const startedAt = !compact && progress?.started_at
    ? new Date(progress.started_at * 1000).toLocaleTimeString('zh-CN', {
      hour: '2-digit', minute: '2-digit',
    })
    : '';

  return (
    <div className={cn(!compact && 'rounded-xl border border-primary/25 bg-primary/5 px-4 py-3')}>
      <div className="flex items-center gap-2 text-xs">
        <Loader2 className={cn('shrink-0 animate-spin text-primary',
          compact ? 'size-3.5' : 'size-4')} />
        <span className="shrink-0 text-muted-foreground">
          {cancelling ? '正在终止…' : walking ? '正在遍历目录…' : '正在检查…'}
        </span>
        {progress?.current && (
          <span className="truncate text-muted-foreground/80" title={progress.current}>
            {progress.current}
          </span>
        )}
        <span className="ml-auto shrink-0 tabular-nums text-muted-foreground">
          {unknown ? '准备中' : `${pct}%（${done}/${total}）`}
        </span>
      </div>
      <div className={cn('relative h-1.5 overflow-hidden rounded-full bg-primary/15',
        compact ? 'mt-1.5' : 'mt-2', unknown && 'rz-indeterminate')}>
        {!unknown && (
          <div className="h-full rounded-full bg-primary transition-[width] duration-500"
            style={{ width: `${pct}%` }} />
        )}
      </div>
      {startedAt && (
        <p className="mt-1.5 text-[11px] text-muted-foreground/70">{startedAt} 开始</p>
      )}
    </div>
  );
}

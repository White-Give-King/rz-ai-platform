import type { ReactNode } from 'react';
import { ArrowUpRight, type LucideIcon } from 'lucide-react';
import { Card } from '@/components/ui/card';
import { cn } from '@/lib/utils';

/** 一个带语义色的小数字指标：圆点 + 标签 + 加粗数字（四张卡共用）。
 *  浅底与文字色由各面板按自己的色表传入（`cls`），未知状态回退中性灰。 */
export function ComplianceMetric({ label, value, cls }: {
  label: string; value: ReactNode; cls: string;
}) {
  return (
    <span className={cn('inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 text-[11px]', cls)}>
      <span className="size-1.5 shrink-0 rounded-full bg-current opacity-70" />
      {label}
      <b className="tabular-nums">{value}</b>
    </span>
  );
}

/** 合规页四张汇总卡的统一形态：图标 + 标题 + 周期 + 一句语境 + 一排带色小数字指标
 *  + 底部查看详情。运行中摘要槽换成进度/状态句、指标行隐藏（数字是上一轮结果）。
 *  卡片上不放任何按钮，执行/刷新/终止都在点开的整屏看板里。 */
export function ComplianceTile({
  icon: Icon, tone, title, cadence, summary, metrics, meta, muted = false, onClick,
}: {
  icon: LucideIcon;
  tone: string;
  title: string;
  cadence: string;
  summary?: ReactNode;
  metrics?: ReactNode;
  meta?: ReactNode;
  muted?: boolean;
  onClick: () => void;
}) {
  return (
    <Card
      role="button"
      tabIndex={0}
      onClick={onClick}
      onKeyDown={(e) => {
        if (e.target !== e.currentTarget) return;
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          onClick();
        }
      }}
      className={cn(
        'group flex cursor-pointer flex-col p-5 transition-all',
        'hover:border-primary/40 hover:shadow-md',
        'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
        muted ? 'border-dashed bg-card/60' : 'border-border',
      )}
    >
      <div className="flex items-center gap-2.5">
        <span className={cn('flex size-9 shrink-0 items-center justify-center rounded-lg', tone)}>
          <Icon className="size-4" />
        </span>
        <span className="truncate text-base font-semibold">{title}</span>
        <span className="ml-auto shrink-0 text-xs text-muted-foreground">{cadence}</span>
      </div>

      <div className="mt-3 space-y-2">
        {summary ? (
          <div className="min-h-5 text-sm leading-5 text-muted-foreground">{summary}</div>
        ) : null}
        {metrics ? (
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1">{metrics}</div>
        ) : null}
      </div>

      <div className="mt-auto flex items-center justify-between gap-3 border-t border-border pt-3">
        <span className="min-w-0 truncate text-xs tabular-nums text-muted-foreground">{meta}</span>
        <span className="flex shrink-0 items-center gap-0.5 text-xs text-muted-foreground/70
          transition-colors group-hover:text-primary">
          查看详情 <ArrowUpRight className="size-3.5" />
        </span>
      </div>
    </Card>
  );
}

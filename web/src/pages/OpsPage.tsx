import { Link } from 'react-router-dom';
import { ArrowUpRight, CalendarClock, ClipboardList, type LucideIcon } from 'lucide-react';
import { PlatformShell } from '@/components/platform-shell';
import { Card, CardContent } from '@/components/ui/card';

type OpsEntry = {
  href: string;
  title: string;
  desc: string;
  metric: string;
  icon: LucideIcon;
  tone: string;
};

const entries: OpsEntry[] = [
  {
    href: '/ops/change-notice',
    title: '变更统计',
    desc: '园区日常运维群里人发的每日变更通知，按自然日汇总各数据中心的变更类型 / 维护事项 / 执行状态',
    metric: '6 园区 · 按自然日',
    icon: CalendarClock,
    tone: 'bg-chart-3/15 text-chart-3',
  },
  {
    href: '/ops/incident-stats',
    title: '事件统计',
    desc: '以飞书园区群速报 / 首报为入口，关联告警与处置工单，按班次汇总全部园区的事件清单',
    metric: '全部园区合并 · 按班次',
    icon: ClipboardList,
    tone: 'bg-primary/10 text-primary',
  },
];

export default function OpsPage() {
  return (
    <PlatformShell
      title="日常运维"
      description="园区运维数据的日常统计入口，数据来自飞书园区群消息"
    >
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
        {entries.map((entry) => {
          const Icon = entry.icon;
          return (
            <Link key={entry.href} to={entry.href} className="group">
              <Card className="h-full transition-all hover:border-primary/40 hover:shadow-md">
                <CardContent className="p-5">
                  <div className={`flex size-10 items-center justify-center rounded-lg ${entry.tone}`}>
                    <Icon className="size-5" />
                  </div>
                  <h3 className="mt-4 flex items-center gap-1.5 font-semibold">
                    {entry.title}
                    <ArrowUpRight className="size-4 text-muted-foreground transition-transform group-hover:translate-x-0.5 group-hover:-translate-y-0.5" />
                  </h3>
                  <p className="mt-1 text-sm leading-relaxed text-muted-foreground">{entry.desc}</p>
                  <div className="mt-4 flex items-center justify-between border-t border-border pt-3">
                    <span className="text-xs text-muted-foreground">{entry.metric}</span>
                  </div>
                </CardContent>
              </Card>
            </Link>
          );
        })}
      </div>
    </PlatformShell>
  );
}

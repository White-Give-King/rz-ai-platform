import { useEffect, useMemo, useState } from 'react';
import { CalendarClock, Download, Filter, Loader2, RefreshCw } from 'lucide-react';
import { PlatformShell } from '@/components/platform-shell';
import { RecordsTable } from '@/components/RecordsTable';
import { FilterSelect, type Opt } from '@/components/filter-select';
import { Card } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';
import {
  changeNoticeExportUrl, changeNoticeRecordsUrl, getChangeNoticeOverview,
  type ChangeNoticeOverview,
} from '@/api/scheduledTasks';
import { downloadArtifact } from '@/lib/artifact-download';

/** 变更类型 / 执行状态 / 等级匹配的展示顺序（与后端分桶一致）。 */
const TYPE_ORDER = ['临时新增紧急变更', '高风险变更', '中风险变更', '低风险变更', '未标注'];
const STATUS_ORDER = ['已完成', '进行中', '已取消'];
const MATCH_ORDER = ['符合', '不符合', '无法判定'];

const TYPE_TONE: Record<string, string> = {
  临时新增紧急变更: 'bg-destructive/10 text-destructive',
  高风险变更: 'bg-warning/15 text-warning',
  中风险变更: 'bg-chart-2/15 text-chart-2',
  低风险变更: 'bg-primary/10 text-primary',
  未标注: 'bg-muted text-muted-foreground',
};

const STATUS_TONE: Record<string, string> = {
  已完成: 'bg-success/15 text-success',
  进行中: 'bg-primary/10 text-primary',
  已取消: 'bg-muted text-muted-foreground',
};

const MATCH_TONE: Record<string, string> = {
  符合: 'bg-success/15 text-success',
  不符合: 'bg-destructive/10 text-destructive',
  无法判定: 'bg-muted text-muted-foreground',
};

const MATCH_DOT: Record<string, string> = {
  符合: 'bg-success',
  不符合: 'bg-destructive',
  无法判定: 'bg-muted-foreground/50',
};

/** 固定园区筛选：与 CHANGE_NOTICE_FEISHU_PARK_CHATS 已配园区保持一致，新增园区需同步这里 */
const PARKS = ['廊坊', '平湖AB', '佛山', '惠州', '重庆', '巴淡'];
const PARK_OPTIONS: Opt[] = [
  { value: '', label: '全部园区' },
  ...PARKS.map((name) => ({ value: name, label: name })),
];

/** 本地日期 → `YYYY-MM-DD`（**不能用 `toISOString()`**：UTC 会让东八区平移到前一天）。 */
function dayStr(d: Date): string {
  const p = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

function today(): string {
  return dayStr(new Date());
}

/** 一张分布卡：标题 + 彩色胶囊计数（无数据出「暂无数据」）。 */
function DistributionCard({ title, order, tones, counts, loading }: {
  title: string;
  order: string[];
  tones: Record<string, string>;
  counts: Record<string, number>;
  loading: boolean;
}) {
  return (
    <Card className="p-4">
      <p className="mb-3 text-xs font-medium text-muted-foreground">{title}</p>
      {loading ? (
        <Loader2 className="size-5 animate-spin text-muted-foreground" />
      ) : order.some((k) => counts[k]) ? (
        <div className="flex flex-wrap gap-1.5">
          {order.filter((k) => counts[k]).map((k) => (
            <span key={k} className={cn('rounded-full px-2.5 py-1 text-xs', tones[k])}>
              {k} <b className="tabular-nums">{counts[k]}</b>
            </span>
          ))}
        </div>
      ) : (
        <span className="text-xs text-muted-foreground">暂无数据</span>
      )}
    </Card>
  );
}

export default function ChangeNoticePage() {
  const [date, setDate] = useState(today);
  const [park, setPark] = useState('');
  const [overview, setOverview] = useState<ChangeNoticeOverview | null>(null);
  const [loading, setLoading] = useState(true);
  const [exporting, setExporting] = useState(false);
  const [error, setError] = useState('');
  const [refreshKey, setRefreshKey] = useState(0);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    setError('');
    getChangeNoticeOverview(date)
      .then((d) => { if (alive) setOverview(d); })
      .catch(() => { if (alive) { setOverview(null); setError('加载失败，请刷新重试'); } })
      .finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
  }, [date, refreshKey]);

  // 选中园区时汇总只算该园区；否则全部合并
  const totals = useMemo(() => {
    const byType: Record<string, number> = {};
    const byStatus: Record<string, number> = {};
    const byMatch: Record<string, number> = {};
    let total = 0;
    for (const p of overview?.parks ?? []) {
      if (park && p.park !== park) continue;
      total += p.total;
      for (const [k, v] of Object.entries(p.by_type)) byType[k] = (byType[k] ?? 0) + v;
      for (const [k, v] of Object.entries(p.by_status)) {
        byStatus[k] = (byStatus[k] ?? 0) + v;
      }
      for (const [k, v] of Object.entries(p.by_match ?? {})) {
        byMatch[k] = (byMatch[k] ?? 0) + v;
      }
    }
    return { total, byType, byStatus, byMatch };
  }, [overview, park]);

  const refresh = () => setRefreshKey((k) => k + 1);

  const exportExcel = async () => {
    setExporting(true);
    setError('');
    const suffix = park ? `_${park}` : '';
    try {
      await downloadArtifact(
        changeNoticeExportUrl({ date, park }),
        `变更统计_${date}${suffix}.xlsx`,
        60000,
      );
    } catch {
      setError('导出失败，请重试');
    } finally {
      setExporting(false);
    }
  };

  return (
    <PlatformShell
      title="变更统计"
      description="园区日常运维群里的变更通知（只统计人发的消息，机器人卡片不计），按自然日汇总各园区 × 数据中心的变更类型、维护事项、执行状态，并核对现场负责人等级是否满足变更等级要求"
    >
      <div className="space-y-4">
        {/* Hero 摘要 */}
        <div className="rounded-2xl border border-border bg-card p-4 shadow-sm sm:p-5">
          <div className="flex flex-wrap items-start justify-between gap-4">
            <div className="min-w-0">
              <p className="text-xs font-medium text-muted-foreground">
                {park || '全部园区'} · {date}
              </p>
              <div className="mt-1 flex items-baseline gap-2">
                <span className="text-4xl font-semibold tracking-tight tabular-nums">
                  {loading ? '—' : totals.total}
                </span>
                <span className="text-sm text-muted-foreground">条变更</span>
              </div>
            </div>
            <div className="flex flex-wrap items-center gap-x-5 gap-y-2">
              {MATCH_ORDER.map((m) => (
                <div key={m} className="flex items-center gap-1.5">
                  <span className={cn('size-2 rounded-full', MATCH_DOT[m])} />
                  <span className="text-xs text-muted-foreground">{m}</span>
                  <span className="text-sm font-semibold tabular-nums">
                    {loading ? '—' : (totals.byMatch[m] ?? 0)}
                  </span>
                </div>
              ))}
            </div>
          </div>
        </div>

        {/* 筛选栏 */}
        <div className="flex flex-wrap items-center gap-2 rounded-xl border border-border bg-card p-2 shadow-sm">
          <label className="flex items-center gap-1.5 rounded-lg border border-border px-2.5 py-1.5 text-sm">
            <CalendarClock className="size-4 text-muted-foreground" />
            <input
              type="date"
              value={date}
              max={dayStr(new Date())}
              onChange={(e) => setDate(e.target.value || today())}
              className="bg-transparent text-sm text-foreground tabular-nums outline-none"
            />
          </label>
          <FilterSelect
            icon={Filter} ariaLabel="按园区筛选"
            value={park} options={PARK_OPTIONS}
            onChange={(v) => setPark(String(v))}
          />
          <Button
            size="sm" variant="outline"
            disabled={exporting || loading}
            onClick={exportExcel}
            className="ml-auto"
          >
            {exporting
              ? <Loader2 className="size-4 animate-spin" />
              : <Download className="size-4" />}
            导出 Excel
          </Button>
          <Button
            size="sm" variant="ghost" disabled={loading} onClick={refresh}
          >
            <RefreshCw className="size-4" /> 刷新
          </Button>
        </div>

        {error && <p className="text-sm text-destructive">{error}</p>}
        {overview?.note && (
          <p className="rounded-xl border border-dashed border-border bg-card/60 px-3 py-2 text-xs text-muted-foreground">
            {overview.note}
          </p>
        )}

        <div className="grid gap-3 sm:grid-cols-3">
          <DistributionCard
            title="变更类型" order={TYPE_ORDER} tones={TYPE_TONE}
            counts={totals.byType} loading={loading}
          />
          <DistributionCard
            title="执行状态" order={STATUS_ORDER} tones={STATUS_TONE}
            counts={totals.byStatus} loading={loading}
          />
          <DistributionCard
            title="等级匹配" order={MATCH_ORDER} tones={MATCH_TONE}
            counts={totals.byMatch} loading={loading}
          />
        </div>

        <Card className="overflow-hidden">
          <div className="h-[60vh] min-h-[360px]">
            <RecordsTable
              key={`${date}:${park}:${refreshKey}`}
              url={changeNoticeRecordsUrl({ date, park })}
              name="变更明细"
              adaptive
              timeout={60000}
              cellTones={{ 变更类型: TYPE_TONE, 执行状态: STATUS_TONE, 等级匹配: MATCH_TONE }}
              emptyText="当天暂无变更记录"
            />
          </div>
        </Card>
      </div>
    </PlatformShell>
  );
}

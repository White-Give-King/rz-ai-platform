import { useEffect, useState } from 'react';
import {
  CheckCircle2, ClipboardList, Clock,
  Download, FileSpreadsheet, Loader2, LockKeyhole, Moon, RefreshCw, Repeat2,
  ShieldCheck, Sun, Timer, XCircle, type LucideIcon,
} from 'lucide-react';
import type { DateRange } from 'react-day-picker';
import { PlatformShell } from '@/components/platform-shell';
import { BoardHeader, type Crumb } from '@/components/board-header';
import { ComplianceMetric, ComplianceTile } from '@/components/compliance-tile';
import { DocUpdatePanel } from '@/components/doc-update-panel';
import { DrillAuditPanel } from '@/components/drill-audit-panel';
import { InfraredQualityPanel } from '@/components/infrared-quality-panel';
import { RecordsTable } from '@/components/RecordsTable';
import { FullScreenOverlay } from '@/components/full-screen-overlay';
import { FilterSelect, type Opt } from '@/components/filter-select';
import { DateRangeFilter } from '@/components/date-range-filter';
import { Badge } from '@/components/ui/badge';
import { Card, CardContent } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';
import {
  complianceSheetUrl, fetchComplianceArtifactBlob, getComplianceRunSheets, getComplianceSpec,
  listComplianceRuns,
  type ComplianceCheck, type ComplianceRun, type ComplianceRunSheets,
  type ComplianceSpec, type ComplianceSummaryItem,
} from '@/api/scheduledTasks';

const RUN_LABEL: Record<string, string> = {
  pending: '排队中', running: '生成中', success: '已完成', failed: '失败',
};

const CADENCE_OPTS: Opt[] = [
  { value: '', label: '全部班次' }, { value: 'night', label: '夜班' },
  { value: 'day', label: '白班' },
];

const PAGE_SIZE = 20;

/** 本地日期 → `YYYY-MM-DD`。**不能用 `toISOString()`**：那是 UTC，东八区会平移到前一天。 */
function dayStr(d: Date): string {
  const p = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

/** 检查项视觉：名字/口径仍来自后端注册表，这里只按 key 给图标与色调，未知 key 走兜底。 */
interface CheckVisual { Icon: LucideIcon; chip: string; bar: string }

const CHECK_VISUALS: Record<string, CheckVisual> = {
  response_timeout: {
    Icon: Timer, chip: 'bg-warning/12 text-warning', bar: 'bg-warning',
  },
  unclosed_order: {
    Icon: ClipboardList, chip: 'bg-primary/10 text-primary', bar: 'bg-primary',
  },
  premature_close: {
    Icon: LockKeyhole, chip: 'bg-destructive/10 text-destructive', bar: 'bg-destructive',
  },
  repeat_alarm: {
    Icon: Repeat2, chip: 'bg-chart-2/15 text-chart-2', bar: 'bg-chart-2',
  },
};
const FALLBACK_VISUAL: CheckVisual = {
  Icon: ShieldCheck, chip: 'bg-muted text-muted-foreground', bar: 'bg-muted-foreground',
};
const visualOf = (key: string) => CHECK_VISUALS[key] ?? FALLBACK_VISUAL;

function fmtShort(iso: string | null | undefined): string {
  if (!iso) return '—';
  const d = new Date(iso);
  const p = (n: number) => String(n).padStart(2, '0');
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

/** 期号 `2026-10-27-n` / `-d` → 班次类型、日期、窗口（从 period_label 的括号里取）。 */
type ShiftLike = { period_key: string; period_label: string; triggered_at?: string };

function shiftMeta(run: ShiftLike) {
  const suffix = run.period_key.slice(-1);
  const kind = suffix === 'n' ? 'night' : suffix === 'd' ? 'day' : '';
  const m = run.period_label.match(/（(.+?)\s*~\s*(.+?)）/);
  return {
    kind,
    label: kind === 'night' ? '夜班' : kind === 'day' ? '白班' : '',
    day: run.period_key.slice(0, 10)
      || (run.triggered_at ? fmtShort(run.triggered_at).slice(0, 5) : ''),
    window: m ? `${m[1]} → ${m[2]}` : '',
  };
}

function totalOf(run: ComplianceRun): number | null {
  if (!run.check_counts) return null;
  return Object.values(run.check_counts).reduce((n, v) => n + (Number(v) || 0), 0);
}

function hasArtifact(run: ComplianceRun): boolean {
  return run.files.some((f) => f.endsWith('.xlsx'));
}

/** 触发下载：端点要 Bearer 头，`<a download>` 带不了，先 fetch 成 Blob。 */
async function saveWorkbook(run: ComplianceRun): Promise<void> {
  const name = run.files.find((f) => f.endsWith('.xlsx')) || run.files[0];
  if (!name) return;
  const blob = await fetchComplianceArtifactBlob(run.run_id, name);
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `${run.period_label || '合规检查'}.xlsx`;
  a.click();
  URL.revokeObjectURL(url);
}

function StatusBadge({ run }: { run: ComplianceRun }) {
  if (run.status === 'failed') {
    return <Badge variant="destructive"><XCircle className="size-3" />失败</Badge>;
  }
  if (run.status === 'pending' || run.status === 'running') {
    return <Badge variant="outline">{RUN_LABEL[run.status]}</Badge>;
  }
  if (!hasArtifact(run)) return <Badge variant="warning">无产物</Badge>;
  return <Badge variant="success"><CheckCircle2 className="size-3" />已完成</Badge>;
}

/**
 * 告警不合规检查汇总卡：只显示最近一期，点开整屏看板按期浏览。
 * 没有产出时保留这张卡（虚线 + 说明），否则「不合规检查」入口会从页面上消失。
 * 与其它三张卡同形态：一句摘要 + 元信息，无按钮、无计数 chip，操作都在看板里。
 */
function CompliancePanel({
  latest, loading, checks, onOpen,
}: {
  latest: ComplianceRun | null;
  loading: boolean;
  checks: ComplianceCheck[];
  onOpen: () => void;
}) {
  const meta = latest ? shiftMeta(latest) : null;
  const usable = latest ? totalOf(latest) !== null : false;
  const generating = latest?.status === 'pending' || latest?.status === 'running';
  const head = meta?.label ? `${meta.label} · ${meta.day}` : '';

  // 一句语境（班次 · 日期）；运行中只说正在生成，指标行隐藏（那是上一轮的数）。
  const summary = !latest
    ? loading ? '正在读取最近一期产出…' : '本功能尚未开启定时产出，点开看板可查四项口径与历史期次'
    : generating ? [head, '正在生成…'].filter(Boolean).join(' · ')
      : usable ? (head || '本班次已完成')
        : [head, '无命中数据'].filter(Boolean).join(' · ');

  const metrics = latest && usable && !generating && checks.length
    ? checks.map((c) => (
      <ComplianceMetric
        key={c.key}
        label={c.name}
        value={latest.check_counts?.[c.key] ?? 0}
        cls={visualOf(c.key).chip}
      />
    ))
    : undefined;

  const metaText = !latest
    ? '每班 09:00 / 18:00 自动生成'
    : latest.status === 'failed' ? '最近一期生成失败'
      : meta?.window ? `窗口 ${meta.window}` : '';

  return (
    <ComplianceTile
      icon={ShieldCheck}
      tone="bg-primary/10 text-primary"
      title="告警不合规检查"
      cadence="按班次"
      muted={!latest}
      summary={summary}
      metrics={metrics}
      meta={metaText}
      onClick={onOpen}
    />
  );
}

/** 本期构成：四项占比堆叠条 + 图例。全 0 时换成绿色结论。 */
function Composition({ summary }: { summary: ComplianceSummaryItem[] }) {
  const total = summary.reduce((n, s) => n + s.rows, 0);
  if (!total) {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-1 py-6">
        <CheckCircle2 className="size-7 text-success/70" />
        <p className="text-sm text-muted-foreground">本班次未发现不合规项</p>
      </div>
    );
  }
  return (
    <div className="space-y-3">
      <div className="flex h-2.5 overflow-hidden rounded-full bg-muted">
        {summary.filter((s) => s.rows > 0).map((s) => (
          <span
            key={s.key}
            className={visualOf(s.key).bar}
            style={{ width: `${(s.rows / total) * 100}%` }}
            title={`${s.name} ${s.rows} 条`}
          />
        ))}
      </div>
      <div className="grid grid-cols-2 gap-x-4 gap-y-1.5">
        {summary.map((s) => (
          <div key={s.key} className="flex items-center gap-1.5 text-[11px]">
            <span className={cn('size-2 shrink-0 rounded-full', visualOf(s.key).bar)} />
            <span className="truncate text-muted-foreground">{s.name}</span>
            <span className="ml-auto tabular-nums">{s.rows}</span>
            <span className="w-9 shrink-0 text-right tabular-nums text-muted-foreground/70">
              {Math.round((s.rows / total) * 100)}%
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}

/** 近 N 期总命中数（数据来自已加载的列表，不足 2 期不画）。 */
function TrendBars({ history, runId }: { history: ComplianceRun[]; runId: string }) {
  const pts = [...history]
    .reverse()
    .filter((r) => r.check_counts)
    .slice(-10)
    .map((r) => ({ run: r, value: totalOf(r) ?? 0 }));
  if (pts.length < 2) {
    return <p className="py-6 text-center text-xs text-muted-foreground">期数不足，暂无法对比</p>;
  }
  const max = Math.max(1, ...pts.map((p) => p.value));
  return (
    <div className="flex h-28 items-end gap-1.5 pt-2">
      {pts.map(({ run, value }) => {
        const cur = run.run_id === runId;
        return (
          <div key={run.run_id}
            className="group flex min-w-0 flex-1 flex-col items-center justify-end gap-1"
            title={`${run.period_label || run.period_key}｜${value} 条`}>
            <span className={cn(
              'text-[10px] tabular-nums',
              cur ? 'font-semibold text-foreground' : 'text-muted-foreground/60',
            )}>{value}</span>
            <span
              className={cn(
                'w-full rounded-t transition-colors',
                cur ? 'bg-primary' : 'bg-primary/25 group-hover:bg-primary/45',
              )}
              style={{ height: `${Math.max(3, (value / max) * 64)}px` }}
            />
            <span className={cn(
              'truncate text-[9px] tabular-nums',
              cur ? 'text-foreground' : 'text-muted-foreground/60',
            )}>{run.period_key.slice(5, 10)}</span>
          </div>
        );
      })}
    </div>
  );
}

/** 较上一同类班次的增减（列表是最新在前，取早于本期的第一条）。 */
function prevCount(history: ComplianceRun[], run: ComplianceRun, key: string): number | undefined {
  const suffix = run.period_key.slice(-1);
  const prev = history.find((r) => r.period_key < run.period_key
    && r.period_key.endsWith(`-${suffix}`) && r.check_counts);
  return prev?.check_counts?.[key];
}

/** 单期明细：四张指标卡 + 构成 + 趋势 + 检查项切换 + 表格。 */
function RunDetail({
  run, data, history, loading, error, activeKey, onActive,
}: {
  run: ComplianceRun;
  data: ComplianceRunSheets | null;
  history: ComplianceRun[];
  loading: boolean;
  error: string;
  activeKey: string;
  onActive: (key: string) => void;
}) {
  const active = data?.summary.find((s) => s.key === activeKey);

  if (loading) {
    return (
      <div className="flex flex-1 items-center justify-center">
        <Loader2 className="size-6 animate-spin text-muted-foreground" />
      </div>
    );
  }
  if (error) {
    return (
      <div className="flex flex-1 flex-col items-center justify-center gap-2">
        <XCircle className="size-8 text-destructive/70" />
        <p className="text-sm text-destructive">{error}</p>
      </div>
    );
  }
  if (!data) return null;

  return (
    <div className="min-h-0 flex-1 overflow-y-auto">
      <div className="mx-auto max-w-6xl space-y-3 p-3 sm:p-4">
        {/* 指标卡带：命中数 + 口径 + 较上一班增减，点击切明细 */}
        <div className="grid grid-cols-2 gap-2 xl:grid-cols-4">
          {data.summary.map((s) => {
            const { Icon, chip } = visualOf(s.key);
            const prev = prevCount(history, run, s.key);
            const delta = prev === undefined ? null : s.rows - prev;
            const selected = s.key === activeKey;
            return (
              <button
                key={s.key}
                type="button"
                onClick={() => onActive(s.key)}
                className={cn(
                  'rounded-xl border bg-card p-3 text-left transition-colors',
                  selected
                    ? 'border-primary/50 ring-1 ring-primary/20'
                    : 'border-border hover:border-primary/30',
                )}
              >
                <div className="flex items-center gap-2">
                  <span className={cn('flex size-7 shrink-0 items-center justify-center rounded-lg', chip)}>
                    <Icon className="size-3.5" />
                  </span>
                  <span className="truncate text-xs text-muted-foreground">{s.name}</span>
                </div>
                <div className="mt-2 flex items-end gap-2">
                  <span className={cn(
                    'text-2xl font-semibold leading-none tracking-tight tabular-nums',
                    s.rows === 0 && 'text-success',
                  )}>{s.rows}</span>
                  <span className="pb-0.5 text-[11px] text-muted-foreground">条</span>
                  {delta !== null && (
                    <span
                      title="较上一同类班次"
                      className={cn(
                        'ml-auto pb-0.5 text-[11px] tabular-nums',
                        delta > 0 ? 'text-destructive'
                          : delta < 0 ? 'text-success' : 'text-muted-foreground/70',
                      )}
                    >
                      {delta === 0 ? '持平' : `${delta > 0 ? '↑' : '↓'}${Math.abs(delta)}`}
                    </span>
                  )}
                </div>
                <p className="mt-1.5 line-clamp-2 text-[11px] leading-relaxed text-muted-foreground/80">
                  {s.description}
                </p>
              </button>
            );
          })}
        </div>

        <div className="grid gap-3 lg:grid-cols-2">
          <Card>
            <CardContent className="p-4">
              <p className="mb-3 text-xs font-medium text-muted-foreground">本期构成</p>
              <Composition summary={data.summary} />
            </CardContent>
          </Card>
          <Card>
            <CardContent className="p-4">
              <p className="mb-1 text-xs font-medium text-muted-foreground">近 10 期总命中数</p>
              <TrendBars history={history} runId={run.run_id} />
            </CardContent>
          </Card>
        </div>

        <div className="flex flex-wrap items-center gap-1 pt-1">
          {data.summary.map((s) => {
            const { Icon } = visualOf(s.key);
            return (
              <button
                key={s.key}
                type="button"
                onClick={() => onActive(s.key)}
                className={cn(
                  'flex shrink-0 items-center gap-1.5 rounded-full border px-3 py-1.5 text-sm transition-colors',
                  s.key === activeKey
                    ? 'border-primary/40 bg-primary/5 text-foreground'
                    : 'border-border text-muted-foreground hover:bg-muted hover:text-foreground',
                )}
              >
                <Icon className="size-3.5" />
                {s.name}
                <span className="tabular-nums opacity-70">{s.rows}</span>
              </button>
            );
          })}
        </div>

        <Card className="overflow-hidden">
          <div className="h-[52vh] min-h-[320px]">
            {active && active.rows === 0 ? (
              <div className="flex h-full flex-col items-center justify-center gap-1.5">
                <CheckCircle2 className="size-7 text-success/70" />
                <p className="text-sm text-muted-foreground">本班次未发现该项不合规</p>
                <p className="max-w-lg px-6 text-center text-xs text-muted-foreground/80">
                  {active.description}
                </p>
              </div>
            ) : (
              <RecordsTable
                key={activeKey}
                url={complianceSheetUrl(run.run_id, activeKey)}
                name={active?.name ?? activeKey}
              />
            )}
          </div>
        </Card>
      </div>
    </div>
  );
}

/** 无产出时展示四个检查项的口径（注册表驱动，各有自己的图标与色条）。 */
function CheckSpecs({ checks }: { checks: ComplianceCheck[] }) {
  return (
    <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-4">
      {checks.map((c) => {
        const { Icon, chip } = visualOf(c.key);
        return (
          <div key={c.key} className="rounded-xl border border-border bg-card p-3.5">
            <div className="flex items-center gap-2">
              <span className={cn('flex size-8 shrink-0 items-center justify-center rounded-lg', chip)}>
                <Icon className="size-4" />
              </span>
              <span className="truncate text-sm font-semibold">{c.name}</span>
              <Badge variant="outline" className="ml-auto shrink-0">{c.cadence}</Badge>
            </div>
            <p className="mt-2 text-xs leading-relaxed text-muted-foreground">{c.description}</p>
          </div>
        );
      })}
    </div>
  );
}

/** 一期一行（按期浏览）。四项计数与窗口在窄屏收起，只留期次 / 状态 / 总命中 / 下载。 */
function PeriodRow({
  run, checks, onOpen,
}: {
  run: ComplianceRun;
  checks: ComplianceCheck[];
  onOpen: (run: ComplianceRun) => void;
}) {
  const meta = shiftMeta(run);
  const counts = run.check_counts;
  const total = totalOf(run);
  const usable = total !== null;
  const Icon = meta.kind === 'night' ? Moon : Sun;

  return (
    <tr
      tabIndex={0}
      onClick={() => onOpen(run)}
      onKeyDown={(e) => {
        if (e.target !== e.currentTarget) return;
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onOpen(run); }
      }}
      title={run.status === 'failed' ? (run.error || '生成失败') : `${run.period_label}｜${run.teaser}`}
      className="cursor-pointer border-t border-border transition-colors hover:bg-muted/40 focus:bg-muted/40 focus:outline-none"
    >
      <td className="px-3 py-2.5">
        <div className="flex items-center gap-2">
          <span className={cn(
            'flex size-7 shrink-0 items-center justify-center rounded-lg',
            meta.kind === 'night' ? 'bg-primary/10 text-primary' : 'bg-warning/15 text-warning',
          )}>
            <Icon className="size-3.5" />
          </span>
          <span className="min-w-0">
            <span className="block font-medium">
              {meta.label} · <span className="tabular-nums">{meta.day}</span>
            </span>
            <span className="block truncate text-[11px] tabular-nums text-muted-foreground/80 xl:hidden">
              {meta.window || run.period_key}
            </span>
          </span>
        </div>
      </td>
      <td className="hidden whitespace-nowrap px-3 py-2.5 text-[11px] tabular-nums text-muted-foreground lg:table-cell">
        {meta.window || '—'}
      </td>
      <td className="px-3 py-2.5"><StatusBadge run={run} /></td>
      {checks.map((c) => {
        const value = counts?.[c.key];
        const { Icon: CheckIcon, chip } = visualOf(c.key);
        return (
          <td key={c.key} className="hidden whitespace-nowrap px-2 py-2.5 text-right xl:table-cell">
            <span
              className={cn(
                'inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-xs tabular-nums',
                value ? chip : 'text-muted-foreground/50',
              )}
              title={c.name}
            >
              <CheckIcon className="size-3" />
              {value ?? '—'}
            </span>
          </td>
        );
      })}
      <td className={cn(
        'px-3 py-2.5 text-right text-base font-semibold tabular-nums',
        usable && total === 0 ? 'text-success' : !usable ? 'text-muted-foreground/60' : '',
      )}>{usable ? total : '—'}</td>
      <td className="hidden whitespace-nowrap px-3 py-2.5 text-right text-[11px] tabular-nums text-muted-foreground lg:table-cell">
        {fmtShort(run.triggered_at)}
        {run.duration_ms > 0 && (
          <span className="opacity-70"> · {(run.duration_ms / 1000).toFixed(0)}s</span>
        )}
      </td>
      <td className="px-3 py-2.5 text-right">
        <Button size="xs" variant="ghost" disabled={!hasArtifact(run)}
          onClick={(e) => { e.stopPropagation(); void saveWorkbook(run); }}
          onKeyDown={(e) => e.stopPropagation()}
          title="下载 Excel">
          <Download className="size-3.5" />
        </Button>
      </td>
    </tr>
  );
}

/**
 * 告警不合规检查看板：**按期浏览**。默认是期次列表（日期区间 + 班次筛选 + 分页），
 * 点某期进入该期明细（指标卡 / 构成 / 趋势 / 四项明细表），面包屑可回列表。
 * 列表与明细共用一个 overlay 与顶栏 —— `BoardHeader` portal 的是同一个插槽，
 * 两个 overlay 叠起来会渲染出两套面包屑。
 */
function ComplianceBoard({ checks, onClose }: { checks: ComplianceCheck[]; onClose: () => void }) {
  const [runs, setRuns] = useState<ComplianceRun[]>([]);
  const [total, setTotal] = useState(0);
  const [statusCounts, setStatusCounts] = useState<Record<string, number>>({});
  const [page, setPage] = useState(1);
  const [range, setRange] = useState<DateRange | undefined>();
  const [cadence, setCadence] = useState('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [view, setView] = useState<ComplianceRun | null>(null);
  const [detail, setDetail] = useState<{
    data: ComplianceRunSheets | null; loading: boolean; error: string; activeKey: string;
  } | null>(null);

  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  const load = async (pageNo: number) => {
    setLoading(true);
    setError('');
    try {
      const data = await listComplianceRuns({
        offset: (pageNo - 1) * PAGE_SIZE,
        limit: PAGE_SIZE,
        cadence,
        start: range?.from ? dayStr(range.from) : '',
        end: range?.to ? dayStr(range.to) : '',
      });
      setRuns(data.runs);
      setTotal(data.total);
      setStatusCounts(data.statusCounts);
      setPage(pageNo);
    } catch {
      setError('加载失败，请刷新重试');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { void load(1); }, [range, cadence]);

  const openRun = async (run: ComplianceRun) => {
    setView(run);
    setDetail({ data: null, loading: true, error: '', activeKey: '' });
    try {
      const data = await getComplianceRunSheets(run.run_id);
      setDetail((cur) => (cur ? {
        ...cur, data, loading: false,
        activeKey: cur.activeKey || data.sheets[0]?.key || '',
      } : cur));
    } catch (e) {
      setDetail((cur) => (cur ? { ...cur, loading: false, error: (e as Error).message } : cur));
    }
  };

  const backToList = () => { setView(null); setDetail(null); };

  const goPage = (p: number) => {
    const target = Math.min(Math.max(1, p), totalPages);
    if (target === page || loading) return;
    void load(target);
  };

  const active = detail?.data?.summary.find((s) => s.key === detail.activeKey);
  const meta = view ? shiftMeta(view) : null;

  const crumbs: Crumb[] = [
    { label: '合规检查', onClick: onClose },
    { label: '告警不合规检查', onClick: view ? backToList : undefined },
    ...(view
      ? [{ label: view.period_label || view.period_key },
        ...(active ? [{ label: active.name }] : [])]
      : []),
  ];

  const hasCounts = Object.keys(statusCounts).length > 0;
  const succeeded = hasCounts ? (statusCounts.success ?? 0)
    : runs.filter((r) => r.status === 'success').length;
  const failed = hasCounts ? (statusCounts.failed ?? 0)
    : runs.filter((r) => r.status === 'failed').length;

  return (
    <FullScreenOverlay>
      <BoardHeader
        onBack={view ? backToList : onClose}
        crumbs={crumbs}
        meta={meta?.window ? <span>{meta.window}</span> : undefined}
        actions={view ? (
          <Button size="sm" variant="outline" className="shrink-0" title="下载 Excel"
            disabled={!hasArtifact(view)} onClick={() => void saveWorkbook(view)}>
            <Download className="size-4" />
            <span className="hidden sm:inline">下载 Excel</span>
          </Button>
        ) : (
          <Button size="sm" variant="ghost" className="shrink-0" disabled={loading}
            onClick={() => void load(page)}>
            <RefreshCw className="size-4" />
            <span className="hidden sm:inline">刷新</span>
          </Button>
        )}
      />

      {view ? (
        <RunDetail
          run={view}
          data={detail?.data ?? null}
          history={runs}
          loading={detail?.loading ?? true}
          error={detail?.error ?? ''}
          activeKey={detail?.activeKey ?? ''}
          onActive={(key) => setDetail((cur) => (cur ? { ...cur, activeKey: key } : cur))}
        />
      ) : (
        <div className="flex min-h-0 flex-1 flex-col gap-3 p-3 sm:p-4">
          <div className="flex flex-wrap items-center gap-2 rounded-xl border border-border bg-card p-2 shadow-sm">
            <DateRangeFilter value={range} onChange={setRange} />
            <FilterSelect
              icon={Clock} ariaLabel="按班次筛选"
              value={cadence} options={CADENCE_OPTS}
              onChange={(v) => setCadence(String(v))}
            />
            <span className="ml-auto px-1 text-xs tabular-nums text-muted-foreground">
              共 {total} 期
            </span>
          </div>

          {error && <p className="text-sm text-destructive">{error}</p>}

          {loading ? (
            <div className="flex flex-1 items-center justify-center">
              <Loader2 className="size-6 animate-spin text-muted-foreground" />
            </div>
          ) : runs.length === 0 ? (
            <div className="min-h-0 flex-1 space-y-4 overflow-y-auto">
              <Card>
                <CardContent className="flex flex-col items-center gap-2 py-14">
                  <FileSpreadsheet className="size-8 text-muted-foreground/50" />
                  <p className="text-sm text-muted-foreground">
                    {range || cadence ? '该筛选条件下没有产出' : '还没有产出'}
                  </p>
                  <p className="text-xs text-muted-foreground/80">
                    {range || cadence
                      ? '换个日期区间或班次看看'
                      : '本功能尚未开启定时产出，开启后每班（09:00 / 18:00）自动生成'}
                  </p>
                </CardContent>
              </Card>
              {checks.length > 0 && <CheckSpecs checks={checks} />}
            </div>
          ) : (
            <>
              <div className="min-h-0 flex-1 overflow-y-auto rounded-xl border border-border bg-card">
                <table className="w-full text-sm">
                  <thead className="sticky top-0 z-10 bg-muted/30 text-xs text-muted-foreground">
                    <tr>
                      <th className="px-3 py-2 text-left font-medium">期次</th>
                      <th className="hidden px-3 py-2 text-left font-medium lg:table-cell">窗口</th>
                      <th className="px-3 py-2 text-left font-medium">状态</th>
                      {checks.map((c) => (
                        <th key={c.key} className="hidden px-2 py-2 text-right font-medium xl:table-cell">
                          {c.name}
                        </th>
                      ))}
                      <th className="px-3 py-2 text-right font-medium">总命中</th>
                      <th className="hidden px-3 py-2 text-right font-medium lg:table-cell">生成时间</th>
                      <th className="px-3 py-2 text-right font-medium">操作</th>
                    </tr>
                  </thead>
                  <tbody>
                    {runs.map((r) => (
                      <PeriodRow key={r.run_id} run={r} checks={checks} onOpen={(x) => void openRun(x)} />
                    ))}
                  </tbody>
                </table>
              </div>

              <div className="flex shrink-0 flex-wrap items-center justify-between gap-2 border-t border-border pt-3">
                <div className="flex items-center gap-3 text-xs text-muted-foreground">
                  <span>共 <b className="text-foreground tabular-nums">{total}</b> 期产出</span>
                  <span className="flex items-center gap-2">
                    <span className="flex items-center gap-1">
                      <CheckCircle2 className="size-3.5 text-success" />{succeeded}
                    </span>
                    {failed > 0 && (
                      <span className="flex items-center gap-1">
                        <XCircle className="size-3.5 text-destructive" />{failed}
                      </span>
                    )}
                  </span>
                </div>
                {totalPages > 1 && (
                  <div className="flex items-center gap-1">
                    <Button size="xs" variant="outline" disabled={page <= 1 || loading}
                      onClick={() => goPage(page - 1)}>上一页</Button>
                    <span className="px-2 text-xs tabular-nums text-muted-foreground">
                      {page} / {totalPages}
                    </span>
                    <Button size="xs" variant="outline" disabled={page >= totalPages || loading}
                      onClick={() => goPage(page + 1)}>下一页</Button>
                  </div>
                )}
              </div>
            </>
          )}
        </div>
      )}
    </FullScreenOverlay>
  );
}

export default function CompliancePage() {
  const [spec, setSpec] = useState<ComplianceSpec | null>(null);
  const [latestRun, setLatestRun] = useState<ComplianceRun | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [boardOpen, setBoardOpen] = useState(false);

  const checks = spec?.checks ?? [];

  // 页面只取最近一期给汇总卡用；完整期次列表在 ComplianceBoard 里按筛选分页加载
  const loadLatest = async () => {
    setLoading(true);
    setError('');
    try {
      const data = await listComplianceRuns({ limit: 1 });
      setLatestRun(data.runs[0] ?? null);
    } catch {
      setError('加载失败，请刷新重试');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    // 检查项注册表与产物清单互不依赖：注册表失败不能让页面变成空白
    getComplianceSpec().then(setSpec).catch(() => setSpec(null));
    void loadLatest();
  }, []);

  return (
    <PlatformShell
      title="合规检查"
      description="按班次核对告警处置不合规，按月度跟踪平台资料更新，按需检测红外巡检图片质量，按周审查应急演练记录"
      actions={(
        <Button size="sm" variant="ghost" onClick={() => { void loadLatest(); }}>
          <RefreshCw className="size-4" /> 刷新
        </Button>
      )}
    >
      <div className="space-y-4">
        {error && <p className="text-sm text-destructive">{error}</p>}

        <DocUpdatePanel />
        <CompliancePanel
          latest={latestRun}
          loading={loading}
          checks={checks}
          onOpen={() => setBoardOpen(true)}
        />
        <InfraredQualityPanel />
        <DrillAuditPanel />
      </div>

      {/* 看板 portal 到 body；关闭时顺手刷新最近一期，避免汇总卡落后 */}
      {boardOpen && (
        <ComplianceBoard
          checks={checks}
          onClose={() => { setBoardOpen(false); void loadLatest(); }}
        />
      )}
    </PlatformShell>
  );
}

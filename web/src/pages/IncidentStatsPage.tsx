import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  ArrowUpRight, BarChart3, Download, FileSpreadsheet, Loader2, Moon, RefreshCw,
  Sun, XCircle,
} from 'lucide-react';
import { PlatformShell } from '@/components/platform-shell';
import { BoardHeader, type Crumb } from '@/components/board-header';
import { RecordsTable } from '@/components/RecordsTable';
import { FullScreenOverlay } from '@/components/full-screen-overlay';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';
import {
  fetchIncidentArtifactBlob, getIncidentPeriodSheets, incidentPeriodSheetUrl,
  listIncidentPeriods,
  type IncidentParkRun, type IncidentPeriod, type IncidentPeriodSheets,
} from '@/api/scheduledTasks';

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

/** 事件统计表 xlsx：端点要 Bearer 头，先 fetch 成 Blob 再触发下载。 */
async function saveIncidentFiles(items: IncidentParkRun[]): Promise<void> {
  for (const item of items) {
    // 现在一期一份合并表（park === 'all'），历史期才是「一园区一份」才带园区名
    const suffix = item.park === 'all' ? '' : `_${item.park_label}`;
    for (const name of item.files) {
      const blob = await fetchIncidentArtifactBlob(item.run_id, name);
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `数据中心事件统计表${suffix}${name.slice(name.lastIndexOf('.'))}`;
      a.click();
      URL.revokeObjectURL(url);
      // 浏览器对连续下载有节流：多园区「下载全部」时留出间隔，否则后面的会被吞掉
      await new Promise((resolve) => setTimeout(resolve, 300));
    }
  }
}

/**
 * 顶部大卡：数据中心事件统计表（飞书园区群通报）最近一期。
 * 没有产出时也保留这张卡，用说明文案代替数字（否则页面顶部会空一块）。
 */
function IncidentHero({
  period, onOpen,
}: {
  period: IncidentPeriod | null;
  onOpen: (period: IncidentPeriod) => void;
}) {
  if (!period) {
    return (
      <div className="rounded-2xl border border-dashed border-border bg-card/60 p-4 sm:p-5">
        <div className="flex items-center gap-2">
          <span className="flex size-8 items-center justify-center rounded-lg bg-chart-3/15 text-chart-3">
            <BarChart3 className="size-4" />
          </span>
          <span className="text-base font-semibold">数据中心事件统计表</span>
          <Badge variant="outline">尚无产出</Badge>
        </div>
        <p className="mt-2 text-xs leading-relaxed text-muted-foreground">
          以飞书园区群通报（速报 + 首报）为入口，关联告警与处置工单补全原因、进展与处理时长，
          全部园区合并成一张班次表（白班 / 夜班）。定时产出开启后每班结束（09:00 夜班 /
          18:00 白班）自动生成。
        </p>
      </div>
    );
  }

  // 班次类型与期卡同源（period_key 后缀），图标/色调也照期卡来，两处说的是同一班次
  const meta = shiftMeta(period);
  const Icon = meta.kind === 'night' ? Moon : meta.kind === 'day' ? Sun : BarChart3;
  const chip = meta.kind === 'night'
    ? 'bg-primary/10 text-primary'
    : meta.kind === 'day' ? 'bg-warning/15 text-warning' : 'bg-chart-3/15 text-chart-3';
  const title = `数据中心事件统计表${meta.label ? ` · ${meta.label}` : ''}`;
  const windowText = meta.window || period.window;

  const downloadable = period.parks.filter((p) => p.status === 'success' && p.files.length > 0);
  // 没有成功产物时后端合并接口会 404，卡片也就不该可点
  const canOpen = downloadable.length > 0;

  const open = () => onOpen(period);

  return (
    <div
      role={canOpen ? 'button' : undefined}
      tabIndex={canOpen ? 0 : undefined}
      onClick={canOpen ? open : undefined}
      onKeyDown={canOpen ? (e) => {
        if (e.target !== e.currentTarget) return;
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); }
      } : undefined}
      className={cn(
        'rounded-2xl border border-border bg-card p-4 shadow-sm sm:p-5',
        canOpen && 'group cursor-pointer transition-all hover:border-primary/40 hover:shadow-md',
      )}
    >
      <div className="flex flex-col gap-5 xl:flex-row xl:items-center">
        <div className="min-w-0 flex-1 space-y-2">
          <div className="flex flex-wrap items-center gap-2">
            <span className={cn('flex size-8 items-center justify-center rounded-lg', chip)}>
              <Icon className="size-4" />
            </span>
            <span className="text-base font-semibold">{title}</span>
            <span className="text-sm tabular-nums text-muted-foreground">{meta.day}</span>
            {period.failed > 0 && <Badge variant="destructive">{period.failed} 个任务失败</Badge>}
          </div>
          {windowText && (
            <p className="text-xs tabular-nums text-muted-foreground/80">{windowText}</p>
          )}
          <p className="text-xs leading-relaxed text-muted-foreground">
            以园区群速报 / 首报为入口，关联告警与处置工单补全原因、进展与处理时长
          </p>
          <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
            <span className="text-4xl font-semibold tracking-tight tabular-nums">
              {period.total_count}
            </span>
            <span className="text-sm text-muted-foreground">条已通报事件</span>
          </div>
        </div>

        <div className="flex shrink-0 gap-2 xl:flex-col">
          {canOpen && (
            <span className="hidden items-center justify-end gap-0.5 text-[11px] text-muted-foreground opacity-0 transition-opacity group-hover:opacity-100 xl:flex">
              查看明细 <ArrowUpRight className="size-3.5" />
            </span>
          )}
        </div>
      </div>
    </div>
  );
}

/** 近期期次：一行一期，点开看当期的合并明细（与顶部大卡同一个看板）。 */
function PeriodRows({
  periods, onOpen,
}: {
  periods: IncidentPeriod[];
  onOpen: (period: IncidentPeriod) => void;
}) {
  return (
    <div className="overflow-hidden rounded-xl border border-border bg-card">
      <table className="w-full text-sm">
        <thead className="bg-muted/30 text-xs text-muted-foreground">
          <tr>
            <th className="px-3 py-2 text-left font-medium">期次</th>
            <th className="hidden px-3 py-2 text-left font-medium lg:table-cell">窗口</th>
            <th className="px-3 py-2 text-right font-medium">事件数</th>
            <th className="px-3 py-2 text-right font-medium">状态</th>
          </tr>
        </thead>
        <tbody>
          {periods.map((p) => {
            const meta = shiftMeta(p);
            const count = p.parks.filter((x) => x.status === 'success').length;
            const openable = count > 0;
            return (
              <tr
                key={p.period_key}
                tabIndex={openable ? 0 : undefined}
                onClick={openable ? () => onOpen(p) : undefined}
                onKeyDown={openable ? (e) => {
                  if (e.target !== e.currentTarget) return;
                  if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onOpen(p); }
                } : undefined}
                className={cn(
                  'border-t border-border',
                  openable ? 'cursor-pointer transition-colors hover:bg-muted/40' : 'opacity-70',
                )}
              >
                <td className="px-3 py-2.5 whitespace-nowrap">
                  <span className="font-medium">{meta.label || '期次'}</span>
                  <span className="ml-1.5 tabular-nums text-muted-foreground">{meta.day}</span>
                </td>
                <td className="hidden px-3 py-2.5 text-xs tabular-nums text-muted-foreground lg:table-cell">
                  {meta.window || p.window || '—'}
                </td>
                <td className="px-3 py-2.5 text-right text-base font-semibold tabular-nums">
                  {p.total_count}
                </td>
                <td className="px-3 py-2.5 text-right">
                  {p.failed > 0 ? (
                    <Badge variant="destructive"><XCircle className="size-3" />{p.failed} 失败</Badge>
                  ) : openable ? (
                    <Badge variant="outline">可查看</Badge>
                  ) : (
                    <Badge variant="outline">无产物</Badge>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

/**
 * 事件统计表整期看板：当期所有园区的行合并成一张表（**不按园区切换** —— 这张表就是
 * 这一班/这一周全园区的事件清单），列与生成的 xlsx 完全一致。
 */
function IncidentBoard({ period, onClose }: { period: IncidentPeriod; onClose: () => void }) {
  const [data, setData] = useState<IncidentPeriodSheets | null>(null);
  const [active, setActive] = useState('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    setError('');
    getIncidentPeriodSheets(period.period_key)
      .then((d) => {
        if (!alive) return;
        setData(d);
        setActive((cur) => cur || d.sheets[0]?.name || '');
      })
      .catch((e) => { if (alive) setError((e as Error).message); })
      .finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
  }, [period.period_key]);

  const downloadable = period.parks.filter((p) => p.status === 'success' && p.files.length > 0);
  const meta = shiftMeta(period);
  const windowText = meta.window || period.window;

  const downloadAll = async () => {
    if (!downloadable.length || busy) return;
    setBusy(true);
    try {
      await saveIncidentFiles(downloadable);
    } finally {
      setBusy(false);
    }
  };

  const crumbs: Crumb[] = [
    { label: '事件统计', onClick: onClose },
    { label: '数据中心事件统计表' },
    { label: period.period_label || period.period_key },
  ];

  return (
    <FullScreenOverlay>
      <BoardHeader
        onBack={onClose}
        crumbs={crumbs}
        meta={windowText ? <span>{windowText}</span> : undefined}
        actions={(
          <>
            {period.failed > 0 && (
              <Badge variant="destructive" className="hidden sm:inline-flex">
                {period.failed} 个任务失败
              </Badge>
            )}
            <Button size="sm" variant="outline" className="shrink-0" title="下载 Excel"
              disabled={!downloadable.length || busy || data?.available === false}
              onClick={() => void downloadAll()}>
              {busy ? <Loader2 className="size-4 animate-spin" /> : <Download className="size-4" />}
              <span className="hidden sm:inline">下载 Excel</span>
            </Button>
          </>
        )}
      />

      {loading ? (
        <div className="flex flex-1 items-center justify-center">
          <Loader2 className="size-6 animate-spin text-muted-foreground" />
        </div>
      ) : error ? (
        <div className="flex flex-1 flex-col items-center justify-center gap-2">
          <XCircle className="size-8 text-destructive/70" />
          <p className="text-sm text-destructive">{error}</p>
        </div>
      ) : !data ? null : data.sheets.length === 0 ? (
        <div className="flex flex-1 flex-col items-center justify-center gap-2 px-6 text-center">
          <FileSpreadsheet className="size-8 text-muted-foreground/50" />
          <p className="max-w-md text-sm text-muted-foreground">
            {data.reason || '该期暂无可预览的明细'}
          </p>
        </div>
      ) : (
        <div className="min-h-0 flex-1">
          {active && (
            <RecordsTable
              key={`${period.period_key}:${active}`}
              url={incidentPeriodSheetUrl(period.period_key, active)}
              name={active}
              adaptive
              headerLeft={(
                <div role="group" aria-label="切换工作表" className="flex flex-wrap items-center gap-1">
                  {data.sheets.map((s) => (
                    <button
                      key={s.key}
                      type="button"
                      aria-pressed={s.name === active}
                      onClick={() => setActive(s.name)}
                      className={cn(
                        'flex shrink-0 items-center gap-1.5 rounded-full border px-3 py-1.5 text-sm transition-colors',
                        s.name === active
                          ? 'border-primary/40 bg-primary/5 text-foreground'
                          : 'border-border text-muted-foreground hover:bg-muted hover:text-foreground',
                      )}
                    >
                      {s.name}
                      <span className="tabular-nums opacity-70">{s.rows}</span>
                    </button>
                  ))}
                </div>
              )}
            />
          )}
        </div>
      )}
    </FullScreenOverlay>
  );
}

export default function IncidentStatsPage() {
  const navigate = useNavigate();
  const [periods, setPeriods] = useState<IncidentPeriod[]>([]);
  const [openPeriod, setOpenPeriod] = useState<IncidentPeriod | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  const load = async () => {
    setLoading(true);
    setError('');
    try {
      setPeriods(await listIncidentPeriods(12));
    } catch {
      setPeriods([]);
      setError('加载失败，请刷新重试');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { void load(); }, []);

  const latest = periods[0] ?? null;

  return (
    <PlatformShell
      title="事件统计"
      description="以飞书园区群通报（速报 + 首报）为入口，关联告警与处置工单，按班次汇总全部园区的事件清单"
      actions={(
        <Button size="sm" variant="ghost" disabled={loading} onClick={() => void load()}>
          <RefreshCw className="size-4" /> 刷新
        </Button>
      )}
    >
      {!openPeriod && (
        <BoardHeader
          onBack={() => navigate('/ops')}
          crumbs={[
            { label: '日常运维', onClick: () => navigate('/ops') },
            { label: '事件统计' },
          ]}
        />
      )}
      <div className="space-y-4">
        {error && <p className="text-sm text-destructive">{error}</p>}
        {loading && !periods.length ? (
          <div className="flex items-center justify-center py-20">
            <Loader2 className="size-6 animate-spin text-muted-foreground" />
          </div>
        ) : (
          <>
            <IncidentHero period={latest} onOpen={setOpenPeriod} />
            {periods.length > 1 && (
              <div className="space-y-2">
                <p className="px-1 text-xs font-medium text-muted-foreground">近期期次</p>
                <PeriodRows periods={periods} onOpen={setOpenPeriod} />
              </div>
            )}
          </>
        )}
      </div>

      {openPeriod && (
        <IncidentBoard period={openPeriod} onClose={() => setOpenPeriod(null)} />
      )}
    </PlatformShell>
  );
}

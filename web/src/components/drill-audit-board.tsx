import { useEffect, useMemo, useRef, useState } from 'react';
import {
  AlertTriangle, CalendarDays, Check, CheckCircle2, ClipboardCheck, ListChecks,
  Loader2, Play, RefreshCw, Tag, Workflow,
} from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';
import { BoardHeader, type Crumb } from '@/components/board-header';
import { FilterSelect } from '@/components/filter-select';
import { FullScreenOverlay } from '@/components/full-screen-overlay';
import MermaidRenderer from '@/components/MermaidRenderer';
import {
  FINISH_STATUS,
  getDrillAuditWeek,
  type DrillAuditDimension,
  type DrillAuditGroup,
  type DrillAuditIssue,
  type DrillAuditLane,
  type DrillAuditMissingStep,
  type DrillAuditOverview,
  type DrillAuditStepFinding,
  type DrillAuditPlanRow,
  type DrillAuditPlanWeek,
  type DrillAuditRunItem,
  type DrillAuditWeekDetail,
  type DrillAuditWeekMeta,
  type DrillAuditWeekSummary,
} from '@/api/scheduledTasks';

export const SEVERITY_ORDER = ['高', '中', '低'] as const;

/** 严重程度三档的色（面板与看板共用）。 */
export function severityChip(level: string): string {
  if (level === '高') return 'bg-destructive/10 text-destructive';
  if (level === '中') return 'bg-warning/12 text-warning';
  return 'bg-muted text-muted-foreground';
}

/** 后端 Mongo 读回的是 naive UTC（无时区后缀），JS 会当本地时间 → 差 8 小时。
 *  无 `Z`/`±HH:MM` 偏移时一律按 UTC 解释；带偏移的原样解析（未来开 tz_aware 也不重复转换）。 */
function toLocalDate(iso: string): Date {
  let s = String(iso).trim().replace(' ', 'T');
  if (!/(?:Z|[+-]\d{2}:?\d{2})$/i.test(s)) s += 'Z';
  return new Date(s);
}

/** ISO 时间 → `YYYY-MM-DD HH:mm`（浏览器按本地时区渲染）。 */
export function fmtTime(iso: string | null | undefined): string {
  if (!iso) return '—';
  const d = toLocalDate(iso);
  if (Number.isNaN(d.getTime())) return '—';
  const p = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} `
    + `${p(d.getHours())}:${p(d.getMinutes())}`;
}

/** `2026-04` → `2026 年 4 月`。 */
function fmtMonth(key: string): string {
  const [year, month] = key.split('-');
  if (!year || !month) return key;
  return `${year} 年 ${Number(month)} 月`;
}

export function auditSourceLabel(source: string): string {
  if (source === 'weekly') return '每周';
  if (source === 'manual') return '手动';
  return '对话';
}

/** 旧缓存没有 `weeks`（或项里没有 week_id）→ 需要刷新一次计划清单。 */
function rosterSchemaStale(rows: DrillAuditPlanRow[] | undefined): boolean {
  const list = rows ?? [];
  if (!list.length) return false;
  return list.some((r) => !Array.isArray(r.weeks)
    || r.weeks.some((w) => !w.week_id));
}

/** 单周执行入参；空 dept_id / year 不传（后端按 week_id 自行解析）。 */
function toRunItem(weekId: string, deptId: string, year?: number): DrillAuditRunItem {
  return {
    week_id: weekId,
    ...(deptId ? { dept_id: deptId } : {}),
    ...(year ? { year } : {}),
  };
}

/** 排期清单存上游码值（'3'），已审周记录存中文标签；两种都要能显示。 */
function finishLabel(status: string): string {
  return FINISH_STATUS[status] || status || '';
}

/** 可执行 = 有 week_id 且本周已交记录（未开始周 fileStatus 会预填，仍不可审）。 */
function scheduleRunnable(w: DrillAuditPlanWeek): boolean {
  if (!w.week_id || !w.file_status) return false;
  return w.finish_status === '' || w.finish_status === '3'
    || w.finish_status === '4';
}

const GROUP_STATUS: Record<string, { label: string; className: string }> = {
  ok: { label: '已审查', className: 'bg-success/12 text-success' },
  missing: { label: '无记录文件', className: 'bg-muted text-muted-foreground' },
  ocr_failed: { label: '解析失败', className: 'bg-destructive/10 text-destructive' },
  no_eop: { label: '无 EOP', className: 'bg-warning/12 text-warning' },
  judge_failed: { label: '判定失败', className: 'bg-warning/12 text-warning' },
};

function groupStatus(status: string) {
  return GROUP_STATUS[status] ?? { label: status || '—', className: 'bg-muted text-muted-foreground' };
}

/** 五个考核维度的中文名（顺序由后端 `_DIMENSIONS` 保证）。 */
const DIMENSION_LABEL: Record<string, string> = {
  role_assignment: '岗位分配',
  step_completeness: '步骤完整性',
  time_limit: '时限执行',
  compliance: '操作合规',
  critical_items: '关键项',
};

/** 维度结论色：达标=绿 / 偏差·缺失=橙红 / 无法判定=灰。 */
function verdictChip(verdict: string): string {
  if (verdict === '达标') return 'bg-success/12 text-success';
  if (verdict === '偏差') return 'bg-warning/12 text-warning';
  if (verdict === '缺失') return 'bg-destructive/10 text-destructive';
  return 'bg-muted text-muted-foreground';
}

/** 步骤标注状态色（与严重程度色分开：超时=橙、违规=红）。 */
function findingStatusChip(status: string): string {
  if (status === '超时') return 'bg-warning/15 text-warning';
  return 'bg-destructive/10 text-destructive';
}

function SeverityChips({ counts }: { counts: Record<string, number | undefined> }) {
  return (
    <>
      {SEVERITY_ORDER.map((level) => (
        <span key={level}
          className={cn('inline-flex items-center rounded-full px-2 py-0.5 text-xs tabular-nums',
            severityChip(level))}>
          {level} {counts[level] ?? 0}
        </span>
      ))}
    </>
  );
}

function weekLabel(w: DrillAuditWeekSummary | { week_no: string; plan_name: string }): string {
  const prefix = w.plan_name || '演练周';
  return w.week_no ? `${prefix} · 第 ${w.week_no} 周` : prefix;
}

interface MatrixColumn {
  key: string;
  label: string;
  range: string;
  sortKey: string;
  monthKey: string;
}

interface MatrixRow {
  key: string;
  dept: string;
  deptId: string;
  year: number;
  plan: string;
  speciality: string;
  /** 已审查的周记录（列 key → 记录） */
  cells: Map<string, DrillAuditWeekSummary>;
  /** 该计划已排期的周（列 key → 排期，含 week_id 与执行状态） */
  scheduled: Map<string, DrillAuditPlanWeek>;
}

export function DrillAuditBoard({
  data, running, notice, onRun, onRefreshRoster, onClose,
}: {
  data: DrillAuditOverview;
  running: boolean;
  notice: string;
  onRun: (items: DrillAuditRunItem[]) => void;
  onRefreshRoster: () => void;
  onClose: () => void;
}) {
  const [year, setYear] = useState('');
  const [month, setMonth] = useState('');
  const [dept, setDept] = useState('');
  const [speciality, setSpeciality] = useState('');
  const [selected, setSelected] = useState('');
  const [detail, setDetail] = useState<DrillAuditWeekDetail | null>(null);
  const [loadingDetail, setLoadingDetail] = useState(false);
  const [detailError, setDetailError] = useState('');
  const [selectMode, setSelectMode] = useState(false);
  const [picked, setPicked] = useState<Map<string, DrillAuditRunItem>>(new Map());
  const autoRefreshed = useRef(false);

  // 旧清单缓存（无 weeks/week_id）→ 自动刷新一次，之后由面板的 24h 规则接管。
  useEffect(() => {
    if (autoRefreshed.current || data.plan_refreshing) return;
    if (!rosterSchemaStale(data.plan_rows)) return;
    autoRefreshed.current = true;
    onRefreshRoster();
  }, [data.plan_rows, data.plan_refreshing, onRefreshRoster]);

  // 切换筛选时清空勾选，避免执行到当前视野外的周。
  useEffect(() => {
    setPicked(new Map());
  }, [dept, speciality, year, month]);

  const togglePick = (item: DrillAuditRunItem) => {
    setPicked((cur) => {
      const next = new Map(cur);
      if (next.has(item.week_id)) next.delete(item.week_id);
      else next.set(item.week_id, item);
      return next;
    });
  };

  const runPicked = () => {
    if (!picked.size || running) return;
    onRun([...picked.values()]);
    setPicked(new Map());
    setSelectMode(false);
  };

  useEffect(() => {
    if (!selected) {
      setDetail(null);
      setDetailError('');
      return;
    }
    let alive = true;
    setLoadingDetail(true);
    setDetailError('');
    getDrillAuditWeek(selected)
      .then((d) => { if (alive) setDetail(d); })
      .catch(() => { if (alive) setDetailError('该演练周详情读取失败'); })
      .finally(() => { if (alive) setLoadingDetail(false); });
    return () => { alive = false; };
  }, [selected]);

  // 审查跑完（running true→false）时重拉当前周详情，刷新「审查于」与结果。
  const prevRunning = useRef(running);
  useEffect(() => {
    const finished = prevRunning.current && !running;
    prevRunning.current = running;
    if (!finished || !selected) return;
    let alive = true;
    getDrillAuditWeek(selected)
      .then((d) => { if (alive) setDetail(d); })
      .catch(() => {});
    return () => { alive = false; };
  }, [running, selected]);

  const monthOpts = useMemo(() => {
    const keys = new Set<string>();
    for (const w of data.weeks) {
      const key = (w.stime || w.etime || '').slice(0, 7);
      if (key) keys.add(key);
    }
    for (const c of data.plan_columns ?? []) {
      const key = (c.stime || c.etime || '').slice(0, 7);
      if (key) keys.add(key);
    }
    return [...keys].sort().reverse()
      .map((key) => ({ value: key, label: fmtMonth(key) }));
  }, [data.weeks, data.plan_columns]);

  const yearOpts = useMemo(() => {
    const keys = new Set<string>();
    for (const w of data.weeks) {
      const key = String(w.year || (w.stime || w.etime || '').slice(0, 4) || '');
      if (key) keys.add(key);
    }
    for (const c of data.plan_columns ?? []) {
      const key = (c.stime || c.etime || '').slice(0, 4);
      if (key) keys.add(key);
    }
    return [...keys].sort().reverse()
      .map((key) => ({ value: key, label: `${key} 年` }));
  }, [data.weeks, data.plan_columns]);

  const specialityOpts = useMemo(() => {
    const keys = new Set<string>();
    for (const p of data.plan_rows ?? []) {
      if (p.speciality) keys.add(p.speciality);
    }
    for (const w of data.weeks) {
      if (w.speciality) keys.add(w.speciality);
    }
    return [...keys].sort((a, b) => a.localeCompare(b, 'zh'))
      .map((key) => ({ value: key, label: key }));
  }, [data.plan_rows, data.weeks]);

  const deptOpts = useMemo(() => data.depts
    .filter((d) => d.dept_id || d.dept_name)
    .map((d) => ({
      value: d.dept_id || d.dept_name,
      label: d.company_name ? `${d.company_name} · ${d.dept_name}` : d.dept_name,
    })), [data.depts]);

  // 必须按部门查看：未选择时回退第一个部门（不提供「全部部门」，全量铺行会卡）。
  const activeDept = dept || deptOpts[0]?.value
    || (data.plan_rows?.[0]
      ? (data.plan_rows[0].dept_id || data.plan_rows[0].dept_name) : '')
    || (data.weeks[0] ? (data.weeks[0].dept_id || data.weeks[0].dept_name) : '');

  // 部门范围内已审周；月份只收窄矩阵的列（行要按可见列决定是否出现）。
  const scoped = useMemo(() => data.weeks.filter((w) => (
    activeDept ? w.dept_id === activeDept || w.dept_name === activeDept : true
  )), [data.weeks, activeDept]);

  const weeks = useMemo(() => scoped.filter((w) => {
    const monthKey = (w.stime || w.etime || '').slice(0, 7);
    const yearKey = String(w.year || (w.stime || w.etime || '').slice(0, 4) || '');
    return (month ? monthKey === month : true) && (year ? yearKey === year : true);
  }), [scoped, month, year]);

  const matrix = useMemo(() => {
    const inYear = (monthKey: string) => !year || monthKey.slice(0, 4) === year;
    const inMonth = (monthKey: string) => !month || monthKey === month;
    const colMap = new Map<string, MatrixColumn>();
    const addColumn = (key: string, label: string, stime: string, etime: string) => {
      if (!key || colMap.has(key)) return;
      const monthKey = (stime || etime || '').slice(0, 7);
      if (!inYear(monthKey) || !inMonth(monthKey)) return;
      const start = stime.slice(5, 10);
      const end = etime.slice(5, 10);
      colMap.set(key, {
        key,
        label,
        range: start && end ? `${start} ~ ${end}` : '',
        sortKey: stime || etime || '9999-12-31',
        monthKey,
      });
    };
    // 列 = 已排期周（含未到期）+ 已审周（跨年记录照旧保留）
    for (const c of data.plan_columns ?? []) {
      addColumn(c.key, c.week_no ? `第 ${Number(c.week_no) || c.week_no} 周` : '演练周',
        c.stime, c.etime);
    }
    for (const w of scoped) {
      addColumn(weekColKey(w), w.week_no ? `第 ${Number(w.week_no) || w.week_no} 周` : '演练周',
        w.stime || '', w.etime || '');
    }
    const columns = [...colMap.values()].sort((a, b) => (
      a.sortKey.localeCompare(b.sortKey) || a.label.localeCompare(b.label)
    ));
    const months: { key: string; label: string; count: number }[] = [];
    for (const col of columns) {
      const last = months[months.length - 1];
      if (last && last.key === col.monthKey) last.count += 1;
      else {
        months.push({
          key: col.monthKey || 'unknown',
          label: col.monthKey ? fmtMonth(col.monthKey) : '未注明月份',
          count: 1,
        });
      }
    }
    const rowMap = new Map<string, MatrixRow>();
    const ensureRow = (
      deptName: string, planKey: string, plan: string, speciality: string,
      deptId: string, planYear: number,
    ) => {
      const key = `${deptName}|${planKey}`;
      let row = rowMap.get(key);
      if (!row) {
        row = {
          key, dept: deptName, deptId, year: planYear, plan: plan || '演练计划',
          speciality, cells: new Map(), scheduled: new Map(),
        };
        rowMap.set(key, row);
      }
      return row;
    };
    // 行 = 全部演练计划（扫描时缓存的清单），再叠加已审周
    for (const p of data.plan_rows ?? []) {
      if (activeDept && p.dept_id !== activeDept && p.dept_name !== activeDept) continue;
      const row = ensureRow(
        p.dept_name, p.plan_id || p.plan_name, p.plan_name, p.speciality,
        p.dept_id, p.year,
      );
      for (const w of p.weeks ?? []) row.scheduled.set(w.key, w);
    }
    for (const w of scoped) {
      const row = ensureRow(
        w.dept_name, w.plan_id || w.plan_name, w.plan_name, w.speciality,
        w.dept_id, w.year || Number((w.stime || '').slice(0, 4)) || 0,
      );
      row.cells.set(weekColKey(w), w);
    }
    // 月份/年份筛选只收窄列，行只保留「在可见列里有排期或审查记录」的那些；
    // 专业筛选只过滤行、不动列。
    const rows = [...rowMap.values()].filter((row) => {
      if (speciality && row.speciality !== speciality) return false;
      if (!month && !year) return true;
      return columns.some((col) => row.cells.has(col.key) || row.scheduled.has(col.key));
    }).sort((a, b) => (
      a.dept.localeCompare(b.dept, 'zh') || a.plan.localeCompare(b.plan, 'zh')
    ));
    return { columns, months, rows };
  }, [scoped, month, year, speciality, activeDept, data.plan_columns, data.plan_rows]);

  const drillRowCount = useMemo(
    () => matrix.rows.filter((row) => row.cells.size > 0 || row.scheduled.size > 0).length,
    [matrix.rows],
  );

  const severityTotals = useMemo(() => {
    const out: Record<string, number> = { 高: 0, 中: 0, 低: 0 };
    for (const w of weeks) {
      for (const g of w.groups) {
        for (const level of SEVERITY_ORDER) {
          out[level] += g.severity_counts?.[level] ?? 0;
        }
      }
    }
    return out;
  }, [weeks]);

  /** 问题条目总数（高+中+低），与「已审几周」无关。 */
  const totalIssues = SEVERITY_ORDER.reduce((n, l) => n + (severityTotals[l] ?? 0), 0);

  const latest = data.latest_run;
  const crumbs: Crumb[] = [{ label: '合规检查', onClick: onClose }];
  if (selected) {
    crumbs.push({ label: '应急演练审查', onClick: () => setSelected('') });
    crumbs.push({ label: detail ? weekLabel(detail.week) : '演练周详情' });
  } else {
    crumbs.push({ label: '应急演练审查' });
  }

  return (
    <FullScreenOverlay>
      <BoardHeader
        onBack={selected ? () => setSelected('') : onClose}
        crumbs={crumbs}
        meta={(
          <span className="hidden text-xs text-muted-foreground sm:inline">
            {running ? '正在审查…' : latest?.triggered_at
              ? `最近运行 ${fmtTime(latest.triggered_at)}` : '尚未运行'}
          </span>
        )}
        actions={(
          <Button size="sm" variant="outline"
            disabled={running || data.plan_refreshing} onClick={onRefreshRoster}>
            <RefreshCw className={cn('size-4', data.plan_refreshing && 'animate-spin')} />
            {data.plan_refreshing ? '刷新中' : '刷新清单'}
          </Button>
        )}
      />

      <div className="min-h-0 flex-1 overflow-y-auto">
        <div className={cn('px-4 py-4 sm:px-6',
          selected ? 'mx-auto max-w-5xl space-y-5' : 'flex h-full flex-col gap-3')}>
          {notice && <p className="text-sm text-muted-foreground">{notice}</p>}

          {selected ? (
            loadingDetail ? (
              <p className="py-10 text-center text-sm text-muted-foreground">
                <Loader2 className="mr-2 inline size-4 animate-spin" />读取演练周详情…
              </p>
            ) : detailError ? (
              <p className="py-10 text-center text-sm text-destructive">{detailError}</p>
            ) : detail ? (
              <>
                {running && <AuditProgress step={data.progress} />}
                <WeekDetail key={detail.week.week_id} data={detail} running={running}
                  onRerun={() => onRun([toRunItem(
                    detail.week.week_id, detail.week.dept_id, detail.week.year,
                  )])} />
              </>
            ) : null
          ) : (
            <>
              <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
                <span className="flex size-8 items-center justify-center rounded-lg
                  bg-chart-2/15 text-chart-2">
                  <ClipboardCheck className="size-4" />
                </span>
                <span className="text-lg font-semibold">应急演练合规审查</span>
                <span className="text-xs tabular-nums text-muted-foreground">
                  计划 {matrix.rows.length} 个 · 有排期 {drillRowCount} 个 · 已审 {weeks.length} 周
                </span>
                {weeks.length > 0 && (
                  <span
                    className="flex items-center gap-1.5 text-xs text-muted-foreground"
                    title="高/中/低 = 已审周内所有班组发现的问题条目按严重程度计数，与周数无关"
                  >
                    问题 {totalIssues} 项
                    <SeverityChips counts={severityTotals} />
                  </span>
                )}
                {latest?.status === 'failed' && (
                  <span className="text-xs text-destructive">最近一次运行失败</span>
                )}
                <span className="ml-auto text-xs text-muted-foreground">
                  {data.weeks.length ? `最近审查 ${fmtTime(data.weeks[0].audited_at)}`
                    : '尚无审查记录'}
                </span>
              </div>

              <div className="flex flex-wrap items-center gap-2">
                <FilterSelect icon={CalendarDays} value={year} ariaLabel="年份"
                  options={[{ value: '', label: '全部年份' }, ...yearOpts]}
                  onChange={(v) => setYear(String(v))} />
                <FilterSelect icon={RefreshCw} value={month} ariaLabel="月份"
                  options={[{ value: '', label: '全部月份' }, ...monthOpts]}
                  onChange={(v) => setMonth(String(v))} />
                <FilterSelect value={activeDept} ariaLabel="部门"
                  options={deptOpts}
                  onChange={(v) => setDept(String(v))} />
                <FilterSelect icon={Tag} value={speciality} ariaLabel="专业"
                  options={[{ value: '', label: '全部专业' }, ...specialityOpts]}
                  onChange={(v) => setSpeciality(String(v))} />
                <span className="text-xs text-muted-foreground">
                  共 {matrix.columns.length} 周
                </span>
                {selectMode ? (
                  <span className="ml-auto flex flex-wrap items-center gap-2">
                    <span className="text-xs tabular-nums text-muted-foreground">
                      已选 {picked.size} 周
                    </span>
                    <Button size="sm" disabled={!picked.size || running} onClick={runPicked}>
                      <Play className="size-4" />
                      执行选中
                    </Button>
                    <Button size="sm" variant="ghost"
                      onClick={() => { setSelectMode(false); setPicked(new Map()); }}>
                      取消
                    </Button>
                  </span>
                ) : (
                  <Button size="sm" variant="outline" className="ml-auto"
                    onClick={() => setSelectMode(true)}>
                    <ListChecks className="size-4" />批量执行
                  </Button>
                )}
              </div>

              {matrix.rows.length === 0 ? (
                <p className="flex-1 rounded-xl border border-border bg-card px-4 py-10
                  text-center text-sm text-muted-foreground">
                  {data.plan_refreshing
                    ? '正在加载计划清单…（首次约 30 秒）'
                    : '该范围内还没有演练计划。点上方「刷新清单」加载计划；'
                      + '加载后点右上角「批量执行」，勾选「已完成」的周即可审查。'}
                </p>
              ) : (
                <div className="min-h-0 flex-1 overflow-auto rounded-xl border border-border bg-card">
                  <table className="min-w-max border-separate border-spacing-0 text-[13px]">
                    <thead>
                      <tr>
                        <th rowSpan={2} scope="col"
                          className="sticky left-0 top-0 z-30 min-w-60 max-w-80 border-b
                            border-r border-border bg-card px-4 py-2 text-left align-middle
                            text-xs font-medium text-muted-foreground">
                          演练任务
                        </th>
                        {/* 专业单独成列（不 sticky：任务列宽度随内容变，左偏移对不齐） */}
                        <th rowSpan={2} scope="col"
                          className="sticky top-0 z-20 min-w-16 border-b border-border
                            bg-card px-3 py-2 text-center align-middle text-xs font-medium
                            text-muted-foreground">
                          专业
                        </th>
                        {matrix.months.map((month) => (
                          <th key={month.key} colSpan={month.count} scope="col"
                            className="sticky top-0 z-20 h-8 border-b border-border bg-muted
                              px-3 text-center align-middle text-xs font-medium
                              text-muted-foreground">
                            {month.label}
                          </th>
                        ))}
                      </tr>
                      <tr>
                        {matrix.columns.map((col) => (
                          <th key={col.key} scope="col"
                            className="sticky top-8 z-20 min-w-24 border-b border-border
                              bg-card px-3 py-2 text-center align-middle text-xs font-medium">
                            <span className="block whitespace-nowrap">{col.label}</span>
                            {col.range && (
                              <span className="block whitespace-nowrap text-[11px] font-normal
                                tabular-nums text-muted-foreground">{col.range}</span>
                            )}
                          </th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {matrix.rows.map((row, idx) => {
                        // 有排期或已审查的行整行高亮 + 左侧色条，方便从上千个无排期计划里找出来
                        const hasDrill = row.cells.size > 0 || row.scheduled.size > 0;
                        return (
                          <tr key={row.key} className={cn(hasDrill && 'bg-warning/5')}>
                            <th scope="row"
                              className={cn(
                                'sticky left-0 z-10 min-w-60 max-w-80 border-b border-r',
                                'border-border px-4 py-3 text-left align-top font-normal',
                                // sticky 列必须不透明，否则横向滚动会透出后面的单元格
                                hasDrill
                                  ? 'bg-[color-mix(in_oklab,var(--color-warning)_5%,var(--color-card))]'
                                  : 'bg-card',
                              )}>
                              {hasDrill && (
                                <span aria-hidden
                                  className="absolute inset-y-0 left-0 w-1 bg-warning/70" />
                              )}
                              <span className="block font-medium leading-snug break-words">
                                <span className="mr-1.5 tabular-nums text-muted-foreground/70">
                                  {idx + 1}
                                </span>
                                {row.plan}
                              </span>
                            </th>
                            <td className="min-w-16 border-b border-border px-3 py-3
                              text-center align-top text-xs text-muted-foreground">
                              {row.speciality || '—'}
                            </td>
                            {matrix.columns.map((col) => {
                              const week = row.cells.get(col.key);
                              const sched = week ? undefined : row.scheduled.get(col.key);
                              return (
                                <td key={col.key}
                                  className="border-b border-border p-0 align-middle
                                    text-center">
                                  {week ? (
                                    <MatrixCell week={week}
                                      selecting={selectMode}
                                      selected={picked.has(week._id)}
                                      onToggle={() => togglePick(toRunItem(
                                        week._id, week.dept_id, week.year,
                                      ))}
                                      onOpen={() => setSelected(week._id)} />
                                  ) : sched ? (
                                    <ScheduleCell sched={sched} running={running}
                                      selectMode={selectMode}
                                      selected={picked.has(sched.week_id)}
                                      onToggle={() => togglePick(toRunItem(
                                        sched.week_id, row.deptId, row.year,
                                      ))} />
                                  ) : (
                                    <span className="text-muted-foreground/25">—</span>
                                  )}
                                </td>
                              );
                            })}
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              )}
            </>
          )}
        </div>
      </div>
    </FullScreenOverlay>
  );
}

/** 周列 key：按「年份 + 周号」对齐各计划；无周号时退回开始日期。 */
function weekColKey(w: DrillAuditWeekSummary): string {
  const no = String(w.week_no ?? '').trim();
  if (no) {
    const year = w.year || (w.stime || '').slice(0, 4);
    const n = Number(no);
    return `${year}-W${Number.isFinite(n) && n > 0 ? n : no}`;
  }
  return (w.stime || '').slice(0, 10) || `${w.dept_id}|${w._id}`;
}

/** 一个周记录的严重程度与问题总数（与面板同口径）。 */
function weekCounts(week: DrillAuditWeekSummary) {
  const counts: Record<string, number> = { 高: 0, 中: 0, 低: 0 };
  let issues = 0;
  for (const g of week.groups) {
    issues += g.issue_count || 0;
    for (const level of SEVERITY_ORDER) counts[level] += g.severity_counts?.[level] ?? 0;
  }
  return { counts, issues };
}

function MatrixCell({
  week, onOpen, selecting, selected, onToggle,
}: {
  week: DrillAuditWeekSummary;
  onOpen: () => void;
  selecting?: boolean;
  selected?: boolean;
  onToggle?: () => void;
}) {
  const { counts, issues } = weekCounts(week);
  const summary = (week.summary || '').trim();
  const levels = SEVERITY_ORDER.filter((level) => (counts[level] ?? 0) > 0);
  const finish = finishLabel(week.finish_status);
  const title = `${auditSourceLabel(week.audit_source)} · 审查于 ${fmtTime(week.audited_at)}`
    + (finish ? ` · ${finish}` : '')
    + (summary ? ` · ${summary.slice(0, 80)}` : '');
  return (
    <button type="button" title={title}
      onClick={selecting && onToggle ? onToggle : onOpen}
      className={cn('relative block h-full w-full px-3 py-3 transition-colors',
        'hover:bg-muted/50', selecting && selected && 'ring-2 ring-inset ring-primary')}>
      {selecting && selected && (
        <Check className="absolute right-1 top-1 size-3.5 text-primary" />
      )}
      {issues === 0 ? (
        <span className="inline-flex items-center rounded-full bg-success/12 px-2 py-0.5
          text-xs font-medium text-success">无问题</span>
      ) : levels.length ? (
        <span className="inline-flex flex-wrap justify-center gap-1">
          {levels.map((level) => (
            <span key={level}
              className={cn(
                'inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium tabular-nums',
                severityChip(level),
              )}>
              {level} {counts[level]}
            </span>
          ))}
        </span>
      ) : (
        <span className="text-xs tabular-nums text-muted-foreground">问题 {issues}</span>
      )}
    </button>
  );
}

/** 未审的排期格：只展示执行状态；批量模式下可勾选（单周审查＝勾选 1 个后执行）。 */
function ScheduleCell({
  sched, running, selectMode, selected, onToggle,
}: {
  sched: DrillAuditPlanWeek;
  running: boolean;
  selectMode: boolean;
  selected: boolean;
  onToggle: () => void;
}) {
  const runnable = scheduleRunnable(sched);
  const status = finishLabel(sched.finish_status);

  if (!runnable) {
    const reason = !sched.file_status
      ? '本周还没有上传任何班组记录，无法审查'
      : sched.finish_status === '1'
        ? '本周尚未开始，结束后再审查'
        : sched.finish_status === '2'
          ? '本周进行中，记录尚未齐全，结束后再审查'
          : '本周条件不满足，暂不能审查';
    return (
      <span title={reason} className="block px-3 py-3">
        <span className="inline-flex items-center rounded-full bg-muted px-2 py-0.5
          text-xs font-medium text-muted-foreground/70">
          {status || '无记录'}
        </span>
      </span>
    );
  }

  const chip = (
    <span className="inline-flex items-center rounded-full border border-warning/40
      bg-warning/10 px-2 py-0.5 text-xs font-medium text-warning">
      {status || '已排期'}
    </span>
  );

  if (!selectMode) {
    return (
      <span title={`${status || '已排期'} · 已交 ${sched.file_status} 班组记录（用「批量执行」勾选后审查）`}
        className="block px-3 py-3">
        {chip}
      </span>
    );
  }

  return (
    <button type="button" disabled={running} title="点此选择本周"
      aria-label={selected ? '取消选择本周' : '选择本周'}
      onClick={onToggle}
      className={cn('relative block h-full w-full px-3 py-3 transition-colors',
        'hover:bg-muted/50 disabled:cursor-not-allowed disabled:opacity-60',
        selected && 'ring-2 ring-inset ring-primary')}>
      {selected && (
        <Check className="absolute right-1 top-1 size-3.5 text-primary" />
      )}
      {chip}
    </button>
  );
}

/** 审查进行中：分步文案 + 不确定进度条（与 doc-update-progress 同一套 rz-indeterminate 样式）。 */
function AuditProgress({ step }: { step?: string }) {
  return (
    <div className="rounded-xl border border-primary/25 bg-primary/5 px-3 py-2.5">
      <div className="flex items-center gap-2 text-xs">
        <Loader2 className="size-3.5 shrink-0 animate-spin text-primary" />
        <span className="truncate font-medium text-primary" title={step || '正在审查…'}>
          {step || '正在审查…'}
        </span>
      </div>
      <div className="rz-indeterminate relative mt-1.5 h-1.5 overflow-hidden rounded-full bg-primary/15" />
    </div>
  );
}

function WeekDetail({
  data, running, onRerun,
}: {
  data: DrillAuditWeekDetail;
  running: boolean;
  onRerun: () => void;
}) {
  const groups = data.groups ?? [];
  const [active, setActive] = useState(() => groups[0]?.letter ?? '');
  const activeGroup = groups.find((g) => g.letter === active) ?? groups[0];
  // 问题行字母全在班组集合里才按班组过滤，否则退回全量（数据兜底）
  const letters = new Set(groups.map((g) => g.letter));
  const canFilter = groups.length > 0
    && data.issues.every((i) => letters.has(String(i.group || '').toUpperCase()));
  const issues = canFilter && activeGroup
    ? data.issues.filter((i) => String(i.group || '').toUpperCase() === activeGroup.letter)
    : data.issues;

  return (
    <>
      <WeekHeader week={data.week} running={running} onRerun={onRerun} />
      <SummaryPanel summary={data.summary} note={data.note} />
      {groups.length > 1 && (
        <GroupTabs groups={groups} active={activeGroup?.letter ?? ''} onSelect={setActive} />
      )}
      {groups.length === 0 ? (
        <p className="text-sm text-muted-foreground">该周记录没有班组信息。</p>
      ) : activeGroup ? (
        <GroupCard key={activeGroup.letter} group={activeGroup} />
      ) : null}
      <IssuesSection
        issues={issues}
        filterLabel={canFilter && activeGroup ? ` · ${activeGroup.letter} 班组` : ''}
        activeGroup={activeGroup}
        hasGroups={groups.length > 0}
      />
    </>
  );
}

/** 非卡片页头：计划信息在面包屑与矩阵单元格里已有，这里只补操作与元信息。 */
function WeekHeader({ week, running, onRerun }: {
  week: DrillAuditWeekMeta;
  running: boolean;
  onRerun: () => void;
}) {
  return (
    <header className="border-b border-border pb-3">
      <div className="flex flex-wrap items-center gap-2">
        <h2 className="text-lg font-semibold">{week.plan_name || '演练计划'}</h2>
        {week.speciality && <Badge variant="outline">{week.speciality}</Badge>}
        <Badge variant="outline">{auditSourceLabel(week.audit_source)}</Badge>
        <span className="ml-auto text-xs tabular-nums text-muted-foreground">
          审查于 {fmtTime(week.audited_at)}
        </span>
        <Button size="sm" variant="outline" disabled={running} onClick={onRerun}>
          {running
            ? <Loader2 className="size-4 animate-spin" />
            : <Play className="size-4" />}
          重新审查
        </Button>
      </div>
      <div className="mt-1.5 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs
        text-muted-foreground">
        <span>部门：{week.dept_name || '—'}</span>
        <span>周次：{week.week_no ? `第 ${week.week_no} 周` : '—'}</span>
        <span className="tabular-nums">{week.stime || '—'} ~ {week.etime || '—'}</span>
        {week.finish_status && <span>完成状态：{finishLabel(week.finish_status)}</span>}
      </div>
    </header>
  );
}

/** 浅色摘要面板：与白卡区分开，避免与看板列表态的大卡视觉重复。 */
function SummaryPanel({ summary, note }: { summary: string; note: string }) {
  return (
    <section className="rounded-xl bg-chart-2/5 px-5 py-4 ring-1 ring-chart-2/15">
      <h3 className="flex items-center gap-1.5 text-sm font-semibold">
        <span className="size-1.5 rounded-full bg-chart-2" />
        整体演练情况
      </h3>
      <p className="mt-2 whitespace-pre-line text-sm leading-relaxed">
        {summary || '无综合评价'}
      </p>
      {note && (
        <p className="mt-2 text-xs leading-relaxed text-muted-foreground">{note}</p>
      )}
    </section>
  );
}

/** 班组胶囊切换：一次只看一个班组，问题表随当前班组联动。 */
function GroupTabs({ groups, active, onSelect }: {
  groups: DrillAuditGroup[];
  active: string;
  onSelect: (letter: string) => void;
}) {
  return (
    <div role="group" aria-label="按班组切换" className="flex flex-wrap items-center gap-2">
      {groups.map((group) => {
        const on = group.letter === active;
        return (
          <button
            key={group.letter}
            type="button"
            aria-pressed={on}
            onClick={() => onSelect(group.letter)}
            className={cn(
              'flex items-center gap-1.5 rounded-full border px-3.5 py-2 text-sm',
              'transition-colors focus-visible:outline-none focus-visible:ring-2',
              'focus-visible:ring-ring',
              on
                ? 'border-primary bg-primary/10 text-primary'
                : 'border-border hover:bg-muted/50',
            )}
          >
            <span className="font-medium">{group.letter} 班组</span>
            <GroupTabBadge group={group} />
          </button>
        );
      })}
    </div>
  );
}

/** 胶囊里的紧凑状态：非 ok 出状态徽标，ok 出「无问题」或非零严重程度计数。 */
function GroupTabBadge({ group }: { group: DrillAuditGroup }) {
  const status = groupStatus(group.status);
  if (group.status !== 'ok') {
    return (
      <span className={cn('rounded-full px-1.5 py-0.5 text-[11px]', status.className)}>
        {status.label}
      </span>
    );
  }
  if (!group.issue_count) {
    return (
      <span className="flex items-center gap-0.5 text-success">
        <CheckCircle2 className="size-3" />无问题
      </span>
    );
  }
  const levels = SEVERITY_ORDER.filter((level) => (group.severity_counts?.[level] ?? 0) > 0);
  if (!levels.length) {
    return <span className="text-muted-foreground">问题 {group.issue_count}</span>;
  }
  return (
    <span className="flex items-center gap-1">
      {levels.map((level) => (
        <span key={level}
          className={cn('rounded-full px-1.5 py-0.5 text-[11px] tabular-nums', severityChip(level))}>
          {level}{group.severity_counts?.[level] ?? 0}
        </span>
      ))}
    </span>
  );
}

const ISSUE_HEADERS = ['序号', '班组', '涉及环节', '考核维度', '问题', 'EOP 依据',
  '演练记录证据', '严重程度', '建议'];

/** 问题明细：有行出表（可按当前班组过滤），空行按班组状态出空状态。 */
function IssuesSection({ issues, filterLabel, activeGroup, hasGroups }: {
  issues: DrillAuditIssue[];
  filterLabel: string;
  activeGroup?: DrillAuditGroup;
  hasGroups: boolean;
}) {
  if (issues.length > 0) {
    return (
      <section className="overflow-hidden rounded-xl border border-border bg-card">
        <header className="border-b border-border px-5 py-3.5 text-sm font-medium">
          问题明细（{issues.length} 项{filterLabel}）
        </header>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="bg-muted/40 text-muted-foreground">
              <tr>
                {ISSUE_HEADERS.map((h) => (
                  <th key={h} className="whitespace-nowrap px-4 py-3 text-left font-medium">
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {issues.map((issue, idx) => (
                <tr key={idx} className="align-top even:bg-muted/25 hover:bg-muted/50">
                  <td className="px-4 py-3 tabular-nums">{idx + 1}</td>
                  <td className="whitespace-nowrap px-4 py-3">{issue.group || '—'}</td>
                  <td className="px-4 py-3">{issue.aspect || '—'}</td>
                  <td className="whitespace-nowrap px-4 py-3">
                    {DIMENSION_LABEL[issue.dimension ?? ''] || issue.dimension || '—'}
                  </td>
                  <td className="px-4 py-3">{issue.problem || '—'}</td>
                  <td className="px-4 py-3">{issue.eop_basis || '—'}</td>
                  <td className="px-4 py-3">{issue.drill_evidence || '—'}</td>
                  <td className="whitespace-nowrap px-4 py-3">
                    <span className={cn('rounded-full px-2 py-0.5 text-xs',
                      severityChip(issue.severity))}>
                      {issue.severity || '—'}
                    </span>
                  </td>
                  <td className="px-4 py-3">{issue.suggestion || '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
    );
  }
  if (!hasGroups) return null;
  return (
    <section className="rounded-xl border border-dashed border-border px-4 py-8 text-center
      text-sm text-muted-foreground">
      {activeGroup?.status === 'ok' ? (
        <span className="inline-flex items-center gap-1.5 text-success">
          <CheckCircle2 className="size-4" />
          该班组未发现与 EOP 不符的环节
        </span>
      ) : (
        `该班组未产出问题明细（${groupStatus(activeGroup?.status ?? '').label}）`
      )}
    </section>
  );
}

/** 五维评估块：岗位分配/步骤完整性/时限执行/操作合规/关键项。 */
function DimensionEval({ dimensions }: { dimensions: DrillAuditDimension[] }) {
  if (!dimensions.length) return null;
  return (
    <section className="mt-3">
      <div className="flex items-center gap-1.5 text-sm font-medium text-muted-foreground">
        <ClipboardCheck className="size-4" />
        维度评估
      </div>
      <div className="mt-1.5 divide-y divide-border overflow-hidden rounded-xl border
        border-border">
        {dimensions.map((d) => (
          <div key={d.key}
            className="flex items-start gap-2 px-4 py-3 text-sm even:bg-muted/20">
            <span className="w-24 shrink-0 font-medium">{DIMENSION_LABEL[d.key] || d.key}</span>
            <span className={cn('shrink-0 rounded-full px-2 py-0.5 text-xs',
              verdictChip(d.verdict))}>
              {d.verdict || '无法判定'}
            </span>
            <div className="min-w-0 flex-1">
              <p className="leading-relaxed">{d.detail || '—'}</p>
              {d.basis && (
                <p className="mt-0.5 text-xs text-muted-foreground">依据：{d.basis}</p>
              )}
            </div>
          </div>
        ))}
      </div>
    </section>
  );
}

/** 漏做步骤块：EOP 要求有、演练记录里没做——最关心的查漏结果。 */
function MissingSteps({ items }: { items: DrillAuditMissingStep[] }) {
  if (!items.length) return null;
  return (
    <section className="mt-3">
      <div className="flex items-center gap-1.5 text-sm font-medium text-destructive">
        <AlertTriangle className="size-4" />
        漏做步骤（{items.length}）
      </div>
      <ul className="mt-1.5 space-y-2">
        {items.map((m, i) => (
          <li key={i}
            className="rounded-xl border border-destructive/25 bg-destructive/5 px-4 py-3 text-sm">
            <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
              <span className="font-medium">{m.eop_step}</span>
              <span className={cn('rounded-full px-2 py-0.5 text-xs', severityChip(m.severity))}>
                {m.severity || '—'}
              </span>
              {m.executor && (
                <span className="text-xs text-muted-foreground">执行人：{m.executor}</span>
              )}
              {m.time_limit && (
                <span className="text-xs text-muted-foreground">时限：{m.time_limit}</span>
              )}
            </div>
            {m.note && (
              <p className="mt-1 text-sm leading-relaxed text-muted-foreground">{m.note}</p>
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}

/** 分车道时间线：一条车道一列，列高随自身步数——短车道天然结束，不像合并泳道图那样
 *  在最长车道下方留一大块空白。多列用 CSS columns（masonry 式）避免行内对齐留白。
 *  `findings`（seq → 步骤标注）给做错/超时的步加状态 chip；漏做步骤没有演练侧 seq，不进时间轴。 */
function LaneTimeline({ lanes, findings }: {
  lanes: DrillAuditLane[];
  findings?: Map<number, DrillAuditStepFinding>;
}) {
  return (
    <div className="mt-3 columns-1 gap-3 sm:columns-2 lg:columns-3">
      {lanes.map((lane) => (
        <div key={lane.name}
          className="mb-3 break-inside-avoid rounded-xl border border-border bg-card">
          <div className="flex items-center justify-between gap-2 border-b border-border px-4 py-3">
            <span className="truncate text-sm font-medium text-muted-foreground">{lane.name}</span>
            <span className="shrink-0 text-xs tabular-nums text-muted-foreground/70">
              {lane.steps.length} 步
            </span>
          </div>
          <ol className="px-4 py-3">
            {lane.steps.map((step, i) => {
              const finding = typeof step.seq === 'number' ? findings?.get(step.seq) : undefined;
              return (
                <li key={i} className="flex gap-3 pb-3.5 last:pb-0">
                  <div className="relative flex w-3 shrink-0 justify-center">
                    <span className={cn('absolute top-1.5 size-1.5 rounded-full',
                      finding ? 'bg-destructive/70' : 'bg-primary/60')} />
                    {i < lane.steps.length - 1 && (
                      <span className="absolute top-4 bottom-[-0.875rem] w-px bg-border" />
                    )}
                  </div>
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center gap-1.5">
                      <span className="text-xs tabular-nums text-muted-foreground">
                        {step.time}
                      </span>
                      {typeof step.delta === 'number' && (
                        <span title="本步耗时"
                          className="rounded-full bg-muted px-1.5 py-0.5 text-xs tabular-nums
                            text-muted-foreground/80">
                          +{step.delta}s
                        </span>
                      )}
                      {finding && (
                        <span className={cn('rounded-full px-1.5 py-0.5 text-xs',
                          findingStatusChip(finding.status))}>
                          {finding.status}
                        </span>
                      )}
                    </div>
                    <p className="mt-0.5 text-sm leading-relaxed">{step.text}</p>
                    {finding?.note && (
                      <p className="mt-0.5 text-xs text-muted-foreground">{finding.note}</p>
                    )}
                  </div>
                </li>
              );
            })}
          </ol>
        </div>
      ))}
    </div>
  );
}

function GroupCard({ group }: { group: DrillAuditGroup }) {
  const status = groupStatus(group.status);
  const [flowOpen, setFlowOpen] = useState(false);
  const hasLanes = !!group.lanes && group.lanes.length > 0;
  const findings = useMemo(() => {
    const map = new Map<number, DrillAuditStepFinding>();
    for (const f of group.step_findings ?? []) {
      if (typeof f.seq === 'number') map.set(f.seq, f);
    }
    return map;
  }, [group.step_findings]);
  return (
    <section aria-label={`${group.letter}班组详情`}
      className="rounded-xl border border-border bg-card p-5">
      <header className="flex flex-wrap items-center gap-2">
        <span className={cn('rounded-full px-2 py-0.5 text-xs', status.className)}>
          {status.label}
        </span>
        {group.file_name && (
          <span className="truncate text-xs text-muted-foreground">{group.file_name}</span>
        )}
      </header>

      <p className="mt-2 text-sm leading-relaxed">
        {group.summary || (group.error ? `无法评价：${group.error}` : '无综合评价')}
      </p>

      <DimensionEval dimensions={group.dimensions ?? []} />
      <MissingSteps items={group.missing_steps ?? []} />

      {(group.scene || group.roles) && (
        <dl className="mt-3 grid gap-3 text-sm leading-relaxed sm:grid-cols-2">
          {group.scene && (
            <div>
              <dt className="text-xs text-muted-foreground">演练场景</dt>
              <dd className="mt-0.5">{group.scene}</dd>
            </div>
          )}
          {group.roles && (
            <div>
              <dt className="text-xs text-muted-foreground">人员分配</dt>
              <dd className="mt-0.5">{group.roles}</dd>
            </div>
          )}
        </dl>
      )}

      {hasLanes && <LaneTimeline lanes={group.lanes!} findings={findings} />}

      {group.flow && (
        <div className="mt-3">
          <button
            type="button"
            onClick={() => setFlowOpen((v) => !v)}
            className="inline-flex items-center gap-1 text-sm text-muted-foreground
              hover:text-foreground"
          >
            <Workflow className="size-4" />
            {flowOpen ? '收起泳道图' : '查看泳道图'}
          </button>
          {flowOpen && (
            <div className="mt-2 overflow-hidden rounded-xl border border-border bg-card">
              <MermaidRenderer code={group.flow} />
            </div>
          )}
        </div>
      )}

      {!hasLanes && group.steps && group.steps.length > 0 ? (
        <details className="mt-3 rounded-xl border border-border bg-muted/20 px-4 py-3">
          <summary className="cursor-pointer text-sm text-muted-foreground">
            演练步骤（{group.steps.length} 步）
          </summary>
          <ol className="mt-2 space-y-1.5 text-sm leading-relaxed">
            {group.steps.map((step, idx) => (
              <li key={idx} className="flex gap-2">
                <span className="shrink-0 tabular-nums text-muted-foreground">{step.time}</span>
                <span>{step.text}</span>
              </li>
            ))}
          </ol>
        </details>
      ) : group.status === 'ok' && !group.steps_available ? (
        <p className="mt-3 text-sm text-muted-foreground">
          该班组的记录文本已不在内容库中，步骤与流程图缺省；重跑该周可补齐。
        </p>
      ) : null}

      {group.raw_text && (
        <details className="mt-3 rounded-xl border border-border bg-muted/20 px-4 py-3">
          <summary className="cursor-pointer text-sm text-muted-foreground">
            原始解析文本（OCR）
          </summary>
          <pre className="mt-2 max-h-96 overflow-auto whitespace-pre-wrap break-words
            text-sm leading-relaxed text-foreground/90">
            {group.raw_text}
          </pre>
        </details>
      )}
    </section>
  );
}

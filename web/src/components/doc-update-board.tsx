import { Fragment, useEffect, useMemo, useState } from 'react';
import {
  Building2, Camera, CalendarRange, ChevronDown, ChevronRight, History, Loader2, Play, Square,
} from 'lucide-react';
import client from '@/api/client';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { FilterSelect } from '@/components/filter-select';
import { FullScreenOverlay } from '@/components/full-screen-overlay';
import { BoardHeader, type Crumb } from '@/components/board-header';
import { DocUpdateProgressBar } from '@/components/doc-update-progress';
import { useConfirm } from '@/components/confirm-dialog';
import { cn } from '@/lib/utils';
import {
  STATUS_ORDER, countStatus, deptKey, deptLabel, fmtTime, statusVisual,
} from '@/components/doc-update-status';
import type {
  DocUpdateItem, DocUpdateJudge, DocUpdateOverview, DocUpdateStatus,
} from '@/api/scheduledTasks';

const MODE_TEXT: Record<string, string> = {
  cross: '与上一期比', baseline: '首期基线',
};

// 业务域 = 清单编号首位的章节（与后端章节骨架的第一段同名），月度 25 条只落在 1/2/3/4/6。
const DOMAIN_ORDER = ['1', '2', '3', '4', '6', 'other'];
const DOMAIN_LABELS: Record<string, string> = {
  '1': '1.规划管理',
  '2': '2.运行维护管理',
  '3': '3.流程管理',
  '4': '4.EHS管理',
  '6': '6.人员配置及组织管理',
  other: '其他',
};
const KINDS: { kind: DocUpdateJudge; label: string; hint: string }[] = [
  { kind: 'new', label: '待新增', hint: '查新增：看本期是否交了新文件，不下载' },
  { kind: 'update', label: '待更新', hint: '查更新：下载算 sha256，与上一期比内容' },
];

function domainKeyOf(item: DocUpdateItem): string {
  const prefix = String(item.number ?? '').trim().charAt(0);
  return DOMAIN_LABELS[prefix] ? prefix : 'other';
}

/** 老快照可能没有 judge（后端按形态回退推断过），缺省按查新增处理。 */
function judgeKindOf(item: DocUpdateItem): DocUpdateJudge {
  return item.judge === 'update' ? 'update' : 'new';
}

const MONTH_RE = /^\d{4}-(0[1-9]|1[0-2])$/;

function fmtMonth(d: Date): string {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`;
}

function currentMonth(): string {
  return fmtMonth(new Date());
}

/** 'YYYY-MM' 往前一个月；传空串时用当前月（补跑默认选最新一期的上一月）。 */
function shiftMonth(period: string): string {
  const m = MONTH_RE.exec(period);
  const now = new Date();
  const year = m ? Number(m[1]) : now.getFullYear();
  const month = m ? Number(m[2]) : now.getMonth() + 1;
  return fmtMonth(new Date(year, month - 2, 1));
}

/** 状态用「圆点 + 文本」，不再用底色胶囊：一行里四种状态并排时不抢视线。 */
function StatusChip({ status, label }: { status: DocUpdateStatus; label: string }) {
  return (
    <span className="inline-flex items-center gap-1.5 whitespace-nowrap text-xs text-foreground/80">
      <span className={cn('size-1.5 shrink-0 rounded-full', statusVisual(status).dot)} />
      {label}
    </span>
  );
}

/** 本期构成：一条细堆叠条，比例一眼可见，不占版面（计数已在筛选项上）。 */
function CompositionBar({
  counts, labels,
}: {
  counts: Partial<Record<DocUpdateStatus, number>>;
  labels: Partial<Record<DocUpdateStatus, string>>;
}) {
  const parts = STATUS_ORDER.map((k) => ({
    key: k, label: labels[k] ?? k, rows: counts[k] ?? 0,
  })).filter((p) => p.rows > 0);
  const total = parts.reduce((n, p) => n + p.rows, 0);
  if (!total) return null;
  return (
    <div
      className="flex h-1.5 overflow-hidden rounded-full bg-muted"
      role="img"
      aria-label={parts.map((p) => `${p.label} ${p.rows} 项`).join('，')}
    >
      {parts.map((p) => (
        <span
          key={p.key}
          className={statusVisual(p.key).bar}
          style={{ width: `${(p.rows / total) * 100}%` }}
          title={`${p.label} ${p.rows} 项`}
        />
      ))}
    </div>
  );
}

/** 目录截图：平台端点要 Bearer，`<img>` 带不了 → 点击时 axios 拉 blob 内联预览。 */
function ScreenshotButton({ shot }: { shot: NonNullable<DocUpdateItem['screenshot']> }) {
  const [src, setSrc] = useState('');
  const [loading, setLoading] = useState(false);
  const [failed, setFailed] = useState('');
  useEffect(() => () => {
    if (src) URL.revokeObjectURL(src);
  }, [src]);

  const toggle = async () => {
    if (src) {
      setSrc('');
      return;
    }
    setLoading(true);
    setFailed('');
    try {
      const resp = await client.get(
        `/scheduled-tasks/doc-update/screenshots/${encodeURIComponent(shot.name)}`,
        { responseType: 'blob' },
      );
      setSrc(URL.createObjectURL(resp.data));
    } catch {
      setFailed('截图无法加载');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="mt-2">
      <Button variant="outline" size="sm" className="h-7 gap-1.5 text-xs"
        onClick={toggle} disabled={loading}>
        {loading ? <Loader2 className="size-3.5 animate-spin" /> : <Camera className="size-3.5" />}
        {src ? '收起截图' : '查看截图'}
      </Button>
      {failed && <span className="ml-2 text-xs text-muted-foreground/70">{failed}</span>}
      {src && (
        <a href={src} target="_blank" rel="noreferrer" className="mt-2 block">
          <img src={src} alt="目录截图" className="max-h-96 w-auto rounded border border-border" />
        </a>
      )}
    </div>
  );
}

/** 疑似文件正常是字符串；容忍后端旧数据/契约漂移下发的文件对象。 */
function fileNameOf(name: unknown): string {
  if (typeof name === 'string') return name;
  if (name && typeof name === 'object') {
    const row = name as { topic?: string; file_name?: string; name?: string };
    return row.topic || row.file_name || row.name || '';
  }
  return '';
}

/** 匹配文件的公共父目录（含年/月层级）；老快照没落 search_path 时也能显示真实位置。 */
function commonParent(paths: string[]): string {
  const chains = paths.filter(Boolean).map((p) => p.split(' / '));
  if (!chains.length) return '';
  let common = chains[0];
  for (const segs of chains.slice(1)) {
    let n = 0;
    while (n < common.length && n < segs.length && common[n] === segs[n]) n += 1;
    common = common.slice(0, n);
  }
  return common.join(' / ');
}

/** 两条路径取更深的那条（段数多者优先）。 */
function pickDeeper(a: string, b: string): string {
  return b.split(' / ').length > a.split(' / ').length ? b : a;
}

/** 查询路径按 ` / ` 切段渲染成胶囊，长路径自然换行，不再一整行灰字。 */
function PathChain({ path }: { path: string }) {
  const segs = (path || '').split('/').map((s) => s.trim()).filter(Boolean);
  if (!segs.length) {
    return <span className="text-sm text-muted-foreground/70">（本期快照未记录，重跑本期可补齐）</span>;
  }
  return (
    <div className="flex flex-wrap items-center gap-x-1 gap-y-1">
      {segs.map((seg, i) => (
        <span key={`${seg}-${i}`} className="flex items-center gap-x-1">
          {i > 0 && <ChevronRight className="size-3 shrink-0 text-muted-foreground/40" />}
          <span className="rounded-md bg-muted px-1.5 py-0.5 text-xs text-foreground/80">{seg}</span>
        </span>
      ))}
    </div>
  );
}

/** 一行清单项；点开列该项匹配到的文件（状态 + 待复核说明）。 */
function ItemRow({ item, labels, open, onToggle, showDept }: {
  item: DocUpdateItem;
  labels: Partial<Record<DocUpdateStatus, string>>;
  open: boolean;
  onToggle: () => void;
  showDept: boolean;
}) {
  const age = item.changed_days_ago == null ? '—' : `${item.changed_days_ago} 天前`;
  const isLlm = item.mapped_by === 'llm';
  const isAgent = item.mapped_by === 'agent';
  // 未找到项也要能点开：让用户看到查过哪个目录、该目录里有哪些真实文件
  const expandable = item.files.length > 0 || item.mapped_by === 'path'
    || (item.dir_files?.length ?? 0) > 0 || (item.review_files?.length ?? 0) > 0
    || (item.pending_files?.length ?? 0) > 0 || !!item.screenshot
    || item.status === 'missing';
  const isPath = item.mapped_by === 'path';
  const source = isPath ? '配置路径'
    : isLlm ? '模型识别'
    : isAgent ? '子代理定位' : '自动匹配';
  const sourceHint = isLlm ? '确定性匹配未命中，模型在候选里选中'
    : isAgent ? '疑似停在模板上，子代理按名称模糊匹配重新定位' : '';
  const configPath = (item.config_path ?? []).join(' / ');
  // 命中文件的实际目录（含年/月）：老快照的 search_path 只到钉点，按文件路径补深
  const filePath = commonParent(item.files.map((f) => f.path ?? ''));
  // 命中文件是否分散在多个文件夹：跨文件夹时公共父目录会塌成很浅的上级，
  // 需要逐文件列出所在目录（f.path 是目录链，不含文件名）
  const folders = new Set(item.files.map((f) => f.path ?? '').filter(Boolean));
  const multiFolder = folders.size > 1;
  // 配置项同时显示钉点（配置查询路径）与命中文件实际目录（含年/月）；
  // 未找到项没有命中文件，回退到钉点，便于确认查的是哪个目录
  const actualPath = pickDeeper(item.search_path || '', filePath) || configPath;
  const periodSource = item.period_source === 'name'
    ? '文件名'
    : item.period_source === 'llm' ? '模型' : '';
  const confidence = item.confidence;
  const dept = deptLabel(item);
  return (
    <>
      <tr
        onClick={expandable ? onToggle : undefined}
        title={expandable ? '展开文件列表' : undefined}
        className={cn(
          'h-12 border-b border-border/60 transition-colors last:border-0',
          expandable && 'cursor-pointer hover:bg-muted/40',
        )}
      >
        {showDept && (
          <td className="px-4">
            <span className="block truncate text-sm" title={dept}>{dept}</span>
          </td>
        )}
        <td className="whitespace-nowrap px-4 text-xs tabular-nums text-muted-foreground">
          {item.number}
        </td>
        <td className="pl-12 pr-4">
          <span className="block truncate text-sm font-medium" title={item.name}>{item.name}</span>
        </td>
        <td className="px-4">
          <div className="flex flex-col items-start gap-1">
            <StatusChip status={item.status} label={item.status_label} />
            {confidence === 'low' ? (
              <Badge
                className="h-5 border-warning/40 bg-warning/10 px-1.5 text-[11px] font-normal text-warning"
                title={item.confidence_reason || '模型判定的置信度较低，建议人工核对实际查找路径与匹配文件'}
              >
                建议人工复核
              </Badge>
            ) : confidence === 'high' ? (
              <Badge
                variant="outline"
                className="h-5 px-1.5 text-[11px] font-normal"
                title="该结论由模型参与得出（同主题复核或子代理定位），展开可见查找路径与匹配文件"
              >
                模型判定
              </Badge>
            ) : null}
          </div>
        </td>
        <td className="whitespace-nowrap px-4 text-right text-xs tabular-nums text-muted-foreground">
          {item.file_count}
        </td>
        <td className="whitespace-nowrap px-4 text-right text-xs tabular-nums text-muted-foreground">
          {age}
        </td>
        <td className="px-4">
          <span className="block truncate text-xs text-muted-foreground/80"
            title={item.error || undefined}>{item.error || '—'}</span>
        </td>
        <td className="px-4 text-right text-muted-foreground/50">
          {expandable && (open
            ? <ChevronDown className="size-3.5" />
            : <ChevronRight className="size-3.5" />)}
        </td>
      </tr>
      {open && expandable && (
        <tr className="border-b border-border/60 bg-muted/20 last:border-0">
          <td colSpan={showDept ? 8 : 7} className="py-3 pr-4 pl-12">
            <div className="space-y-3">
              <dl className="grid grid-cols-[5.5rem_1fr] items-start gap-x-3 gap-y-2">
                <dt className="pt-0.5 text-xs text-muted-foreground">匹配方式</dt>
                <dd className="flex flex-wrap items-center gap-2 text-sm text-foreground/85">
                  <span>{source}</span>
                  {sourceHint && (
                    <span className="text-xs text-muted-foreground/70">{sourceHint}</span>
                  )}
                  {periodSource && (
                    <Badge variant="outline" className="h-5 px-1.5 text-[11px] font-normal">
                      期号来源 {periodSource}
                    </Badge>
                  )}
                </dd>
                {item.confidence_reason && (
                  <>
                    <dt className="pt-0.5 text-xs text-muted-foreground">人工复核原因</dt>
                    <dd className="text-sm leading-relaxed text-warning">
                      {item.confidence_reason}
                    </dd>
                  </>
                )}
                {isPath && (
                  <>
                    <dt className="pt-0.5 text-xs text-muted-foreground">配置查询路径</dt>
                    <dd>
                      <PathChain path={configPath} />
                    </dd>
                  </>
                )}
                <dt className="pt-0.5 text-xs text-muted-foreground">实际查找路径</dt>
                <dd>
                  {actualPath
                    ? <PathChain path={actualPath} />
                    : item.error
                      ? <span className="text-sm text-warning">{item.error}</span>
                      : <PathChain path="" />}
                </dd>
                {item.match_reason && (
                  <>
                    <dt className="pt-0.5 text-xs text-muted-foreground">复核依据</dt>
                    <dd className="text-sm leading-relaxed break-all text-foreground/85">
                      {item.match_reason}
                    </dd>
                  </>
                )}
              </dl>

              {item.warn && (
                <p className="rounded-md bg-warning/10 px-3 py-1.5 text-sm text-warning">
                  {item.warn}
                </p>
              )}

              {item.files.length > 0 && (
                <div>
                  <p className="mb-1 text-xs font-medium text-muted-foreground">
                    匹配文件（{item.files.length}）
                  </p>
                  <ul className="divide-y divide-border/60 overflow-hidden rounded-lg border border-border/70 bg-card">
                    {item.files.map((f, i) => (
                      <li key={`${f.file_name}-${i}`}
                        className="flex flex-wrap items-center gap-x-3 gap-y-1 px-3 py-2">
                        <StatusChip status={f.status} label={labels[f.status] ?? f.status} />
                        <span className="min-w-0 flex-1 break-all text-sm text-foreground/90">
                          {f.file_name || '（无文件名）'}
                        </span>
                        {f.period && (
                          <span className="shrink-0 text-xs tabular-nums text-muted-foreground">
                            期号 {f.period}
                          </span>
                        )}
                        {multiFolder && f.path && (
                          <span className="w-full break-all text-xs text-muted-foreground/70">
                            所在目录：{f.path}
                          </span>
                        )}
                        {f.warn && <span className="w-full text-xs text-warning">{f.warn}</span>}
                        {f.error && (
                          <span className="w-full text-xs text-destructive/80">{f.error}</span>
                        )}
                      </li>
                    ))}
                  </ul>
                  {item.matched_count != null && item.matched_count > item.files.length && (
                    <p className="mt-1 text-xs text-muted-foreground/70">
                      另有 {item.matched_count - item.files.length} 个历史文件未列出（不影响判定）
                    </p>
                  )}
                </div>
              )}

              {(item.review_files?.length ?? 0) > 0 && (
                <div className="rounded-lg border border-chart-1/25 bg-chart-1/[0.06] px-3 py-2.5">
                  <p className="mb-1.5 text-sm font-medium text-chart-1">
                    疑似文件（{item.review_files!.length}）— 归属待人工确认
                  </p>
                  <ul className="space-y-1">
                    {item.review_files!.map((name, i) => (
                      <li key={`${fileNameOf(name)}-${i}`}
                        className="break-all text-sm text-foreground/85">
                        {fileNameOf(name)}
                      </li>
                    ))}
                  </ul>
                </div>
              )}

              {(item.pending_files?.length ?? 0) > 0 && (
                <div className="rounded-lg border border-warning/25 bg-warning/[0.06] px-3 py-2.5">
                  <p className="mb-1.5 text-sm font-medium text-warning">
                    待人工确认（{item.pending_files!.length}）— 月份无年份锚
                  </p>
                  <ul className="space-y-1">
                    {item.pending_files!.map((name, i) => (
                      <li key={`${fileNameOf(name)}-${i}`}
                        className="break-all text-sm text-foreground/85">
                        {fileNameOf(name)}
                      </li>
                    ))}
                  </ul>
                </div>
              )}

              {(item.dir_files?.length ?? 0) > 0 && (
                <details>
                  <summary className="cursor-pointer text-xs font-medium text-muted-foreground">
                    该目录实际文件（{item.dir_files!.length}）
                  </summary>
                  <ul className="mt-1.5 max-h-48 space-y-1 overflow-y-auto rounded-lg border border-border/70 bg-card px-3 py-2">
                    {item.dir_files!.map((name, i) => (
                      <li key={`${fileNameOf(name)}-${i}`}
                        className="break-all text-sm text-foreground/85">
                        {fileNameOf(name)}
                      </li>
                    ))}
                  </ul>
                </details>
              )}

              {item.screenshot?.name && <ScreenshotButton shot={item.screenshot} />}
              {item.screenshot?.error && (
                <p className="text-xs text-muted-foreground/70">
                  目录截图未生成：{item.screenshot.error}
                </p>
              )}
            </div>
          </td>
        </tr>
      )}
    </>
  );
}

/** 文档更新检查看板：概览（范围 + 状态筛选 + 构成）→ 清单 → 变更/待复核 → 判定口径。 */
export function DocUpdateBoard({
  data, running, notice, onCheck, onCheckDept, onBackfill, onCancel,
  cancelling = false, onClose, dept, onDeptChange, period, onPeriodChange,
}: {
  data: DocUpdateOverview;
  running: boolean;
  notice: string;
  onCheck: () => void;
  /** 只查当前选中的部门（其余部门沿用本期已有结果） */
  onCheckDept?: () => void;
  /** 按指定期间补跑一期；scope 不传＝全部部门，传了＝只跑该部门（并入该期快照） */
  onBackfill?: (period: string, force: boolean,
                scope?: { company_id: string; dept_name: string }) => void;
  /** 终止正在跑的检查（本轮结果不落库） */
  onCancel?: () => void;
  cancelling?: boolean;
  onClose: () => void;
  dept: string;
  onDeptChange: (value: string) => void;
  period: string;
  onPeriodChange: (value: string) => void;
}) {
  const confirm = useConfirm();
  const [filter, setFilter] = useState<DocUpdateStatus | ''>('');
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  // 分组折叠态：`dom:<域>` / `kind:<域>:<new|update>`，默认全部展开
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  // 补跑期间的月份/部门选择面板（展开时才渲染）
  const [backfillOpen, setBackfillOpen] = useState(false);
  const [backfillMonth, setBackfillMonth] = useState('');
  // 空串＝全部部门（约 3 小时）；默认取当前筛选的部门
  const [backfillDept, setBackfillDept] = useState('');

  const verdict = data.verdict;
  const items = useMemo(() => verdict?.items ?? [], [verdict]);
  const labels = verdict?.labels ?? {};
  const mode = verdict?.mode ?? 'baseline';
  const baseline = verdict?.baseline ?? true;

  // 部门列是常驻配置，但口径要按快照本身判断（旧单树快照没有部门维度）
  const deptColumns = data.dept_columns ?? [];
  const periods = data.periods ?? [];
  const deptOptions = deptColumns.map((col) => ({
    value: deptKey(col), label: deptLabel(col),
  }));
  // 补跑可选「全部部门」：单部门才快（8~10 分钟），23 个部门整轮约 3 小时
  const backfillDeptOptions = [
    { value: '', label: '全部部门（约 3 小时）' },
    ...deptOptions,
  ];
  const periodOptions = [
    { value: '', label: '最新' },
    ...periods.map((p) => ({
      value: p.period_key,
      label: `${p.period_label || p.period_key}${p.scope === 'tree' ? '（旧版口径）' : ''}`,
    })),
  ];
  // 补跑只接受合法且不晚于当前月的 YYYY-MM
  const backfillValid = MONTH_RE.test(backfillMonth) && backfillMonth <= currentMonth();
  const catalogSize = data.catalog?.length ?? 25;
  const isDept = verdict?.scope === 'dept';
  const unit = isDept ? '格' : '项';
  const deptFilter = isDept ? dept : '';
  const showDept = isDept && !deptFilter;
  const cols = showDept ? 8 : 7;
  const companyCount = new Set(deptColumns.map((c) => c.company_id || c.company_name || '')).size;
  const scopeTitle = companyCount > 1
    ? `${companyCount} 家公司 · ${deptColumns.length} 个部门` : `${deptColumns.length} 个部门`;
  const selectedDept = deptColumns.find((c) => deptKey(c) === deptFilter);
  const deptTitle = selectedDept ? deptLabel(selectedDept) : deptFilter;
  // 补跑目标部门（空串＝全部部门），供按钮 title 与回执文案使用
  const backfillCol = deptColumns.find((c) => deptKey(c) === backfillDept);
  const backfillWho = isDept && backfillCol ? deptLabel(backfillCol) : '全部部门';

  // 覆盖重跑与「全部部门约 3 小时」合并成一个弹窗：两条都命中时一次说清；
  // 点击时先把期间/force/范围冻结，轮询期间下拉变化不会影响已确认的目标
  const startBackfill = async () => {
    if (!backfillValid) return;
    const target = backfillMonth;
    const force = periods.some((p) => p.period_key === target);
    const scope = backfillCol
      ? { company_id: backfillCol.company_id || '', dept_name: backfillCol.dept_name }
      : undefined;
    const warnings = [
      force ? `${target} 已有快照，确认后会覆盖重跑。` : '',
      scope ? '' : '全部部门补跑约需 3 小时。',
    ].filter(Boolean);
    if (warnings.length) {
      const ok = await confirm({
        title: `开始补跑 ${target}？`,
        description: (
          <ul className="list-disc space-y-1 pl-4">
            {warnings.map((w) => <li key={w}>{w}</li>)}
          </ul>
        ),
        confirmText: '开始补跑',
        variant: force ? 'destructive' : 'default',
      });
      if (!ok) return;
    }
    onBackfill?.(target, force, scope);
    setBackfillOpen(false);
  };

  // 只显示所选部门的 25 项；dept 为空（旧口径/未选）时原样铺开
  const deptItems = deptFilter
    ? items.filter((i) => deptKey(i) === deptFilter)
    : items;
  // 该期快照里有没有这个部门：没有＝本期还没检查过它（不是「未找到」）
  const checkedDepts = new Set((verdict?.depts ?? []).map((d) => deptKey(d)));
  const deptUnchecked = isDept && !!deptFilter && items.length > 0
    && !checkedDepts.has(deptFilter);
  const rows = filter ? deptItems.filter((i) => i.status === filter) : deptItems;
  const groups = useMemo(() => {
    const buckets = new Map<string, {
      key: string; label: string; newItems: DocUpdateItem[]; updateItems: DocUpdateItem[];
    }>();
    for (const item of rows) {
      const key = domainKeyOf(item);
      let group = buckets.get(key);
      if (!group) {
        group = { key, label: DOMAIN_LABELS[key], newItems: [], updateItems: [] };
        buckets.set(key, group);
      }
      (judgeKindOf(item) === 'update' ? group.updateItems : group.newItems).push(item);
    }
    return DOMAIN_ORDER.filter((key) => buckets.has(key))
      .map((key) => buckets.get(key)!);
  }, [rows]);
  const counts = deptFilter ? countStatus(deptItems) : (verdict?.counts ?? {});
  const total = deptFilter ? deptItems.length : (verdict?.total ?? items.length);
  const changed = deptFilter
    ? (verdict?.changes?.changed ?? []).filter((c) => deptKey(c) === deptFilter)
    : (verdict?.changes?.changed ?? []);
  const warnings = deptFilter
    ? (verdict?.warnings ?? []).filter((w) => deptKey(w) === deptFilter)
    : (verdict?.warnings ?? []);

  const hints: Record<DocUpdateStatus, string> = isDept
    ? {
        updated: '查新：本期已交新文件；查更新：内容哈希与上期不同',
        stale: '查新：本期没有新文件；查更新：内容哈希与上期相同',
        missing: '该部门下未查询到相关文件',
        review: '找到疑似同主题文件，归属待人工确认（不计入已更新）',
        failed: '取不到字节或类型校验不过',
        baseline: '尚无上期快照（查更新项 / 无期号的查新增项）',
      }
    : {
        updated: '哈希与上一期不同（新增文件也算）',
        stale: '哈希与上一期相同',
        missing: '未查询到相关文件',
        review: '找到疑似同主题文件，归属待人工确认（不计入已更新）',
        failed: '取不到字节或类型校验不过',
        baseline: '',
      };

  const toggle = (key: string) => {
    setExpanded((cur) => {
      const next = new Set(cur);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  const toggleCollapsed = (key: string) => {
    setCollapsed((cur) => {
      const next = new Set(cur);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  const chipClass = (active: boolean) => cn(
    'inline-flex h-8 shrink-0 items-center gap-1.5 rounded-md border px-2.5 text-xs',
    'transition-colors',
    'outline-none focus-visible:ring-2 focus-visible:ring-ring',
    active
      ? 'border-primary/40 bg-primary/5 font-medium text-foreground'
      : 'border-transparent text-muted-foreground hover:bg-muted hover:text-foreground',
  );

  const crumbs: Crumb[] = [
    { label: '合规检查', onClick: onClose },
    { label: '文档更新检查' },
    { label: verdict?.period_label || '最新' },
  ];

  return (
    <FullScreenOverlay>
      <BoardHeader
        onBack={onClose}
        crumbs={crumbs}
        meta={(
          <>
            <span>快照 {fmtTime(data.latest?.taken_at)}</span>
            {data.latest && (
              <span>共匹配 {data.latest.file_count} 个文件 · {data.latest.elapsed}s</span>
            )}
          </>
        )}
      />

      <div className="min-h-0 flex-1 overflow-y-auto">
        <div className="mx-auto max-w-7xl space-y-4 px-4 py-4 sm:px-6">
          {/* 概览：范围一行 + 构成细条（筛选项与操作在下面独立的筛选栏里） */}
          <section className="space-y-3 rounded-lg border border-border bg-card p-4">
            <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
              <span className="text-sm font-semibold">{verdict?.period_label || '最新一期'}</span>
              <span className="text-xs text-muted-foreground">
                {isDept ? `${scopeTitle} × ${catalogSize} 项` : MODE_TEXT[mode] ?? mode}
                {` · 共 ${total} ${unit}`}
              </span>
              {!isDept && (
                <span className="text-xs text-muted-foreground/70">只判定 25 条月度项</span>
              )}
            </div>

            <CompositionBar counts={counts} labels={labels} />

            {baseline && (
              <p className="rounded-md border border-border bg-muted/40 px-3 py-2 text-xs leading-relaxed text-muted-foreground">
                {isDept
                  ? <>首轮：查新增项（有月份期号）已直接判定；查更新项与没有期号可比的查新增项
                    记为「基线」，<b>下期起</b>比对是否更新。</>
                  : <>首期只建立基线，<b>下期起</b>才比对是否更新；本次每项都记「基线」，不代表「未更新」。</>}
              </p>
            )}

            {running && <DocUpdateProgressBar progress={data.progress ?? null} />}
          </section>

          {/* 筛选栏：期间 / 部门 / 状态 + 操作，独立一条，不与表头、列表混在一起 */}
          <section className="rounded-lg border border-border bg-card px-4 py-2">
            <div className="flex flex-nowrap items-center gap-2 overflow-x-auto">
              {periods.length > 0 && (
                <FilterSelect ariaLabel="按期间筛选" icon={CalendarRange} value={period}
                  options={periodOptions} onChange={(v) => onPeriodChange(String(v))}
                  className="h-8 shrink-0 py-0" />
              )}
              {isDept && deptColumns.length > 0 && (
                <FilterSelect ariaLabel="按部门筛选" icon={Building2} value={dept}
                  options={deptOptions} onChange={(v) => onDeptChange(String(v))}
                  className="h-8 shrink-0 py-0" />
              )}
              <span className="mx-1 hidden h-5 w-px shrink-0 bg-border sm:block" />
              <div className="flex shrink-0 items-center gap-1.5"
                role="group" aria-label="按状态筛选">
                <button type="button" aria-pressed={filter === ''}
                  onClick={() => setFilter('')} className={chipClass(filter === '')}>
                  全部
                  <span className="tabular-nums text-muted-foreground">{total}</span>
                </button>
                {STATUS_ORDER.map((key) => {
                  const selected = filter === key;
                  return (
                    <button
                      key={key}
                      type="button"
                      aria-pressed={selected}
                      title={hints[key]}
                      onClick={() => setFilter(selected ? '' : key)}
                      className={chipClass(selected)}
                    >
                      <span className={cn('size-1.5 rounded-full', statusVisual(key).dot)} />
                      {labels[key] ?? key}
                      <span className={cn(
                        'tabular-nums',
                        (counts[key] ?? 0) === 0
                          ? 'text-muted-foreground/50' : 'text-muted-foreground',
                      )}>{counts[key] ?? 0}</span>
                    </button>
                  );
                })}
              </div>
              {filter && (
                <Button variant="ghost" className="shrink-0" onClick={() => setFilter('')}>
                  清除筛选
                </Button>
              )}
              <div className="ml-auto flex shrink-0 items-center gap-2">
                {isDept && deptColumns.length > 0 && dept && onCheckDept && (
                  <Button variant="outline" disabled={running}
                    title="只查这个部门，其余部门沿用本期已有结果"
                    onClick={onCheckDept}>
                    {running
                      ? <Loader2 className="size-3.5 animate-spin" />
                      : <Building2 className="size-3.5" />}
                    只查此部门
                  </Button>
                )}
                {!running && onBackfill && (backfillOpen ? (
                  <>
                    <input
                      type="month"
                      aria-label="补跑期间"
                      value={backfillMonth}
                      max={currentMonth()}
                      onChange={(e) => setBackfillMonth(e.target.value)}
                      className="h-8 shrink-0 rounded-md border border-border bg-background
                        px-2 text-xs tabular-nums text-foreground outline-none
                        focus-visible:ring-2 focus-visible:ring-ring"
                    />
                    {isDept && deptColumns.length > 0 && (
                      <FilterSelect ariaLabel="补跑的目标部门" icon={Building2}
                        value={backfillDept} options={backfillDeptOptions}
                        onChange={(v) => setBackfillDept(String(v))}
                        className="h-8 shrink-0 py-0" />
                    )}
                    <Button
                      disabled={!backfillValid}
                      title={backfillValid
                        ? `补跑 ${backfillWho} 的 ${backfillMonth}，结果并入该期快照`
                        : '请选一个不晚于当前月的期间'}
                      onClick={() => void startBackfill()}
                    >
                      <History className="size-3.5" />
                      开始补跑
                    </Button>
                    <Button variant="ghost" onClick={() => setBackfillOpen(false)}>
                      取消
                    </Button>
                  </>
                ) : (
                  <Button
                    variant="outline"
                    title="按指定历史期间重新检查（结果保存为该期快照）"
                    onClick={() => {
                      setBackfillMonth(shiftMonth(periods[0]?.period_key ?? ''));
                      setBackfillDept(dept);   // 默认补当前正在看的那一个部门
                      setBackfillOpen(true);
                    }}
                  >
                    <History className="size-3.5" />
                    补跑期间
                  </Button>
                ))}
                {running && onCancel ? (
                  <Button disabled={cancelling} onClick={onCancel}
                    title="终止本次检查（结果不保存）">
                    {cancelling
                      ? <Loader2 className="size-3.5 animate-spin" />
                      : <Square className="size-3.5" />}
                    {cancelling ? '终止中' : '终止'}
                  </Button>
                ) : (
                  <Button disabled={running} onClick={onCheck}
                    title={running ? '检查中' : '立即检查'}>
                    {running
                      ? <Loader2 className="size-3.5 animate-spin" />
                      : <Play className="size-3.5" />}
                    {running ? '检查中' : '立即检查'}
                  </Button>
                )}
              </div>
            </div>
          </section>

          {/* 清单项明细：只留标题与表格，筛选与操作都在上面的筛选栏 */}
          <section className="rounded-lg border border-border bg-card">
            <div className="flex h-12 shrink-0 items-center gap-2 border-b border-border px-4">
              <h2 className="shrink-0 text-sm font-semibold">清单项明细</h2>
              <span className="min-w-0 truncate text-xs tabular-nums text-muted-foreground">
                {deptFilter
                  ? `${deptTitle} · ${rows.length} 项${filter ? ` / 共 ${total}` : ''}`
                  : `${rows.length} 项${filter ? ` / 全部 ${total}` : ''}`}
              </span>
            </div>
            {rows.length === 0 ? (
              <p className="px-4 py-12 text-center text-xs text-muted-foreground">
                {items.length === 0
                  ? '尚无快照，点「立即检查」建立首期基线'
                  : deptUnchecked
                    ? `${deptTitle} 本期还没有检查结果，点上方「只查此部门」补齐`
                    : filter ? '没有该状态的清单项' : '该范围内没有清单项'}
              </p>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full min-w-[760px] table-fixed border-collapse text-left text-sm">
                  <colgroup>
                    {showDept ? (
                      <>
                        <col className="w-[14%]" />
                        <col className="w-[9%]" />
                        <col className="w-[26%]" />
                        <col className="w-[11%]" />
                        <col className="w-[7%]" />
                        <col className="w-[9%]" />
                        <col className="w-[20%]" />
                        <col className="w-[4%]" />
                      </>
                    ) : (
                      <>
                        <col className="w-[10%]" />
                        <col className="w-[30%]" />
                        <col className="w-[12%]" />
                        <col className="w-[8%]" />
                        <col className="w-[10%]" />
                        <col className="w-[26%]" />
                        <col className="w-[4%]" />
                      </>
                    )}
                  </colgroup>
                  <thead className="bg-muted/40 text-xs text-muted-foreground">
                    <tr className="h-9 border-b border-border">
                      {showDept && <th className="px-4 py-2 font-medium">部门</th>}
                      <th className="px-4 py-2 font-medium">编号</th>
                      <th className="px-4 py-2 font-medium">文档</th>
                      <th className="px-4 py-2 font-medium">状态</th>
                      <th className="px-4 py-2 text-right font-medium">列出文件</th>
                      <th className="px-4 py-2 text-right font-medium">上次变更</th>
                      <th className="px-4 py-2 font-medium">说明</th>
                      <th className="px-4 py-2" />
                    </tr>
                  </thead>
                  <tbody>
                    {groups.map((group) => {
                      const domKey = `dom:${group.key}`;
                      const domOpen = !collapsed.has(domKey);
                      return (
                        <Fragment key={domKey}>
                          <tr className="h-11 border-y border-border bg-muted/50">
                            <td colSpan={cols} className="px-4">
                              <button
                                type="button"
                                aria-expanded={domOpen}
                                onClick={() => toggleCollapsed(domKey)}
                                className="flex w-full items-center gap-1.5 text-left"
                              >
                                {domOpen
                                  ? <ChevronDown className="size-4" />
                                  : <ChevronRight className="size-4" />}
                                <span className="text-sm font-semibold text-foreground">
                                  {group.label}
                                </span>
                                <span className="ml-auto text-xs tabular-nums text-muted-foreground">
                                  待新增 {group.newItems.length} · 待更新 {group.updateItems.length}
                                </span>
                              </button>
                            </td>
                          </tr>
                          {domOpen && KINDS.map(({ kind, label, hint }) => {
                            const kindItems = kind === 'update'
                              ? group.updateItems : group.newItems;
                            if (kindItems.length === 0) return null;
                            const kindKey = `kind:${group.key}:${kind}`;
                            const kindOpen = !collapsed.has(kindKey);
                            return (
                              <Fragment key={kindKey}>
                                <tr className="h-9 border-b border-border/60 bg-muted/25">
                                  <td colSpan={cols} className="pl-8 pr-4">
                                    <button
                                      type="button"
                                      aria-expanded={kindOpen}
                                      title={hint}
                                      onClick={() => toggleCollapsed(kindKey)}
                                      className="flex w-full items-center gap-1.5 text-left"
                                    >
                                      {kindOpen
                                        ? <ChevronDown className="size-3" />
                                        : <ChevronRight className="size-3" />}
                                      <span className="text-xs font-medium text-foreground/80">
                                        {label}
                                      </span>
                                      <span className="text-xs tabular-nums text-muted-foreground">
                                        {kindItems.length}
                                      </span>
                                    </button>
                                  </td>
                                </tr>
                                {kindOpen && kindItems.map((item) => {
                                  const key = `${deptKey(item)}-${item.seq ?? item.number}`;
                                  return (
                                    <ItemRow
                                      key={key}
                                      item={item}
                                      labels={labels}
                                      showDept={showDept}
                                      open={expanded.has(key)}
                                      onToggle={() => toggle(key)}
                                    />
                                  );
                                })}
                              </Fragment>
                            );
                          })}
                        </Fragment>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </section>

          {changed.length > 0 && (
            <section className="rounded-lg border border-border bg-card">
              <div className="flex items-center gap-2 border-b border-border px-4 py-2">
                <h2 className="text-sm font-semibold">本期变更文件</h2>
                <span className="text-xs tabular-nums text-muted-foreground">{changed.length}</span>
              </div>
              <div className="overflow-x-auto">
                <table className="w-full table-fixed border-collapse text-left text-sm">
                  <colgroup>
                    {showDept ? (
                      <>
                        <col className="w-[15%]" />
                        <col className="w-[10%]" />
                        <col className="w-[28%]" />
                        <col className="w-[29%]" />
                        <col className="w-[18%]" />
                      </>
                    ) : (
                      <>
                        <col className="w-[10%]" />
                        <col className="w-[32%]" />
                        <col className="w-[34%]" />
                        <col className="w-[24%]" />
                      </>
                    )}
                  </colgroup>
                  <thead className="bg-muted/30 text-xs text-muted-foreground">
                    <tr className="border-b border-border">
                      {showDept && <th className="px-4 py-2 font-medium">部门</th>}
                      <th className="px-4 py-2 font-medium">编号</th>
                      <th className="px-4 py-2 font-medium">文档</th>
                      <th className="px-4 py-2 font-medium">文件名</th>
                      <th className="px-4 py-2 font-medium">变更时间</th>
                    </tr>
                  </thead>
                  <tbody>
                    {changed.map((c, i) => (
                      <tr key={`${deptKey(c)}-${c.number}-${c.file_name}-${i}`}
                        className="h-10 border-b border-border/60 last:border-0">
                        {showDept && (
                          <td className="px-4 text-xs">
                            <span className="block truncate" title={deptLabel(c)}>{deptLabel(c)}</span>
                          </td>
                        )}
                        <td className="whitespace-nowrap px-4 text-xs tabular-nums text-muted-foreground">
                          {c.number}
                        </td>
                        <td className="px-4 text-xs">
                          <span className="block truncate" title={c.name}>{c.name}</span>
                        </td>
                        <td className="px-4 text-xs text-muted-foreground">
                          <span className="block truncate" title={c.file_name}>{c.file_name}</span>
                        </td>
                        <td className="whitespace-nowrap px-4 text-xs tabular-nums text-muted-foreground">
                          {fmtTime(c.changed_at)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </section>
          )}

          {warnings.length > 0 && (
            <section className="rounded-lg border border-warning/30 bg-card">
              <div className="flex items-center gap-2 border-b border-border px-4 py-2">
                <h2 className="text-sm font-semibold">待复核</h2>
                <span className="text-xs tabular-nums text-muted-foreground">{warnings.length}</span>
                <span className="text-xs text-muted-foreground/80">
                  档案类型或大小与实际字节不符
                </span>
              </div>
              <div className="overflow-x-auto">
                <table className="w-full table-fixed border-collapse text-left text-sm">
                  <colgroup>
                    {showDept ? (
                      <>
                        <col className="w-[15%]" />
                        <col className="w-[10%]" />
                        <col className="w-[28%]" />
                        <col className="w-[29%]" />
                        <col className="w-[18%]" />
                      </>
                    ) : (
                      <>
                        <col className="w-[10%]" />
                        <col className="w-[30%]" />
                        <col className="w-[32%]" />
                        <col className="w-[28%]" />
                      </>
                    )}
                  </colgroup>
                  <thead className="bg-muted/30 text-xs text-muted-foreground">
                    <tr className="border-b border-border">
                      {showDept && <th className="px-4 py-2 font-medium">部门</th>}
                      <th className="px-4 py-2 font-medium">编号</th>
                      <th className="px-4 py-2 font-medium">文档</th>
                      <th className="px-4 py-2 font-medium">文件名</th>
                      <th className="px-4 py-2 font-medium">说明</th>
                    </tr>
                  </thead>
                  <tbody>
                    {warnings.map((w, i) => (
                      <tr key={`${deptKey(w)}-${w.number}-${w.file_name}-${i}`}
                        className="h-10 border-b border-border/60 last:border-0">
                        {showDept && (
                          <td className="px-4 text-xs">
                            <span className="block truncate" title={deptLabel(w)}>{deptLabel(w)}</span>
                          </td>
                        )}
                        <td className="whitespace-nowrap px-4 text-xs tabular-nums text-muted-foreground">
                          {w.number}
                        </td>
                        <td className="px-4 text-xs">
                          <span className="block truncate" title={w.name}>{w.name}</span>
                        </td>
                        <td className="px-4 text-xs text-muted-foreground">
                          <span className="block truncate" title={w.file_name}>{w.file_name}</span>
                        </td>
                        <td className="px-4 text-xs text-warning">
                          <span className="block truncate" title={w.warn}>{w.warn}</span>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </section>
          )}

          <section className="space-y-1.5 pb-2 text-xs leading-relaxed text-muted-foreground">
            <h2 className="text-sm font-semibold text-foreground">判定口径</h2>
            {isDept ? (
              <>
                <p>「未找到」「下载失败」不等于「未更新」：前者是该部门下未查询到相关文件，
                  后者是取不到字节或类型校验不过，需人工复核。</p>
                <p>判据按清单项固定（取自更新目录 PDF 的第三列）：标「线上更新」的项查内容是否变化，
                  下载算 sha256 与上一期比；其余项查本期是否交了新文件（有月份期号看期号，没有则看
                  上期没有的新文件），不下载。</p>
                <p>是否更新<b>只与上一期（上月）快照比</b>：同月内手动检查多次，结论都不会变。</p>
              </>
            ) : (
              <>
                <p>「未找到」「下载失败」不等于「未更新」：前者是未查询到相关文件，
                  后者是取不到字节或类型校验不过，需人工复核。</p>
                <p>判定用文件内容哈希（sha256），不用接口的修改时间字段；同一次检查只下载文件算哈希，不落盘。</p>
                <p>是否更新<b>只与上一期（上月）快照比</b>：同月内手动检查多次，结论都不会变。</p>
              </>
            )}
            {notice && <p className="text-foreground/70">{notice}</p>}
          </section>
        </div>
      </div>
    </FullScreenOverlay>
  );
}

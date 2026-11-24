import { useCallback, useEffect, useRef, useState } from 'react';
import { ClipboardCheck } from 'lucide-react';
import { ComplianceMetric, ComplianceTile } from '@/components/compliance-tile';
import {
  DrillAuditBoard, fmtTime, SEVERITY_ORDER, severityChip,
} from '@/components/drill-audit-board';
import {
  getDrillAuditOverview, refreshDrillAuditRoster, triggerDrillAuditRun,
  type DrillAuditOverview, type DrillAuditRunItem,
} from '@/api/scheduledTasks';

/** 每周任务按租约跑：running / 清单刷新期间 3s 拉一次，释放即停，以后端为准。 */
const POLL_INTERVAL = 3000;
/** 清单缓存超过该时长就在打开看板时自动刷新一次。 */
const ROSTER_TTL_MS = 24 * 60 * 60 * 1000;

function rosterStale(data: DrillAuditOverview): boolean {
  const rows = data.plan_rows ?? [];
  if (rows.length === 0) return true;
  // 旧缓存（行里没有 weeks 或项里没有 week_id）拿不到执行状态与可执行周 → 刷新一次
  if (rows.some((r) => !Array.isArray(r.weeks) || r.weeks.some((w) => !w.week_id))) {
    return true;
  }
  const ts = data.plan_updated_at ? Date.parse(data.plan_updated_at) : NaN;
  return !Number.isFinite(ts) || Date.now() - ts > ROSTER_TTL_MS;
}

export function DrillAuditPanel() {
  const [data, setData] = useState<DrillAuditOverview | null>(null);
  const [notice, setNotice] = useState('');
  const [open, setOpen] = useState(false);
  const autoRefreshed = useRef(false);

  const load = useCallback(async () => {
    try {
      setData(await getDrillAuditOverview());
    } catch {
      // 面板是附加信息，取不到不打断合规页
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  // 清单缓存为空或过期时自动后台刷新一次（每会话只触发一次，失败不重试）。
  useEffect(() => {
    if (autoRefreshed.current || !data?.enabled) return;
    if (data.running || data.plan_refreshing || !rosterStale(data)) return;
    autoRefreshed.current = true;
    void refreshDrillAuditRoster()
      .then((res) => {
        if (res.started) setData((cur) => (cur ? { ...cur, plan_refreshing: true } : cur));
      })
      .catch(() => {
        // 失败保持原样，用户可点「立即检查」或重开页面
      });
  }, [data]);

  // 先立即拉一次：一轮审查可能几十秒就结束，等第一个 3s tick 会错过过程。
  const busy = Boolean(data?.running || data?.plan_refreshing);
  useEffect(() => {
    if (!busy) return;
    let alive = true;
    const tick = async () => {
      try {
        const next = await getDrillAuditOverview();
        if (alive) setData(next);
      } catch {
        // 网络抖动继续轮询
      }
    };
    void tick();
    const timer = window.setInterval(() => { void tick(); }, POLL_INTERVAL);
    return () => { alive = false; window.clearInterval(timer); };
  }, [busy]);

  const onRun = async (items: DrillAuditRunItem[]) => {
    setNotice('');
    if (!items.length) return;
    try {
      const res = await triggerDrillAuditRun({ items });
      if (!res.started) {
        setNotice(res.reason || '已有一次审查在跑，请稍候');
        await load();
        return;
      }
      setData((cur) => (cur ? { ...cur, running: true } : cur));
    } catch {
      setNotice('启动审查失败，请稍后重试');
    }
  };

  const onRefreshRoster = async () => {
    setNotice('');
    try {
      const res = await refreshDrillAuditRoster();
      if (!res.started) {
        setNotice(res.reason || '清单正在刷新，请稍候');
        await load();
        return;
      }
      setData((cur) => (cur ? { ...cur, plan_refreshing: true } : cur));
    } catch {
      setNotice('刷新清单失败，请稍后重试');
    }
  };

  if (!data?.enabled) return null;

  const weeks = data.weeks ?? [];
  const severity = { 高: 0, 中: 0, 低: 0 };
  for (const w of weeks) {
    for (const g of w.groups) {
      for (const level of SEVERITY_ORDER) severity[level] += g.severity_counts?.[level] ?? 0;
    }
  }
  const latest = data.latest_run;

  // 一句语境：运行中/刷新中给状态，已审给周数，空态说明可做什么
  const summary = data.running ? '正在审查…'
    : weeks.length ? `已审 ${weeks.length} 周`
      : data.plan_refreshing ? '正在加载计划清单…'
        : '点开看板按周查看执行状态，进「批量执行」勾选后审查（勾 1 个即单周）';

  // 严重程度计数（全部已审周累计）：色表与看板同一份（severityChip），0 值照常显示
  const metrics = !data.running && weeks.length
    ? SEVERITY_ORDER.map((level) => (
      <ComplianceMetric key={level} label={level} value={severity[level]} cls={severityChip(level)} />
    ))
    : undefined;
  const metaText = latest?.triggered_at
    ? `${latest.status === 'failed' ? '最近运行失败' : '最近运行'} ${fmtTime(latest.triggered_at)}`
    : weeks.length ? `最近审查 ${fmtTime(weeks[0].audited_at)}`
      : '每周一自动扫描到点该审的周';

  return (
    <>
      <ComplianceTile
        icon={ClipboardCheck}
        tone="bg-chart-2/15 text-chart-2"
        title="应急演练合规审查"
        cadence="每周"
        summary={summary}
        metrics={metrics}
        meta={metaText}
        onClick={() => setOpen(true)}
      />
      {open && (
        <DrillAuditBoard
          data={data}
          running={data.running}
          notice={notice}
          onRun={(items) => void onRun(items)}
          onRefreshRoster={() => void onRefreshRoster()}
          onClose={() => setOpen(false)}
        />
      )}
    </>
  );
}

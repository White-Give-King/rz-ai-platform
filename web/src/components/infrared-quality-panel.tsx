import { useCallback, useEffect, useState } from 'react';
import { ScanEye } from 'lucide-react';
import { ComplianceMetric, ComplianceTile } from '@/components/compliance-tile';
import { COUNT_ORDER, InfraredQualityBoard } from '@/components/infrared-quality-board';
import {
  getInfraredQualityTasks,
  type InfraredQualityOverview,
} from '@/api/scheduledTasks';

/** 「已检测/未检测」以任务文档为准；轮询只在有租约时进行，后端释放即停。 */
const POLL_INTERVAL = 3000;

export function InfraredQualityPanel() {
  const [data, setData] = useState<InfraredQualityOverview | null>(null);
  const [deptId, setDeptId] = useState('');
  const [open, setOpen] = useState(false);

  const load = useCallback(async (refresh = false) => {
    try {
      const next = await getInfraredQualityTasks(deptId, refresh);
      setData(next);
      if (!deptId && next.dept?.dept_id) setDeptId(next.dept.dept_id);
    } catch {
      // 面板是附加信息，取不到不打断合规页
    }
  }, [deptId]);

  useEffect(() => { void load(); }, [load]);

  useEffect(() => {
    if (!data?.running) return;
    const timer = window.setInterval(() => { void load(); }, POLL_INTERVAL);
    return () => window.clearInterval(timer);
  }, [data?.running, load]);

  if (!data?.enabled) return null;

  const pending = data.tasks.filter((t) => !t.checked_at && t.status !== 1).length;
  const running = data.running;
  const progress = data.progress;
  const pct = progress && progress.total > 0
    ? Math.round((progress.done / progress.total) * 100) : 0;
  const latest = data.latest_run;
  const counts = latest?.counts;

  // 一句语境：运行中给进度；其余给检查范围（部门数），不点名某个部门（部门在看板里选）
  const deptCount = data.departments.length;
  const summary = running
    ? `正在检测 · ${progress && progress.total > 0
      ? `${pct}%（${progress.done}/${progress.total}）` : '准备中'}`
      + (progress?.current ? ` · ${progress.current}` : '')
    : deptCount ? `${deptCount} 个部门 · 按巡检任务检测` : '按巡检任务检测';

  // 四档判定计数：色表与看板同一份（COUNT_ORDER），0 值照常显示
  const metrics = !running && latest
    ? COUNT_ORDER.map(({ key, label, chip }) => (
      <ComplianceMetric key={key} label={label} value={counts?.[key] ?? 0} cls={chip} />
    ))
    : undefined;

  const metaText = `${data.tasks.length} 个任务 · 待检测 ${pending}`
    + (!running && latest?.status === 'failed' ? ' · 最近一次检测失败' : '');

  return (
    <>
      <ComplianceTile
        icon={ScanEye}
        tone="bg-chart-1/15 text-chart-1"
        title="红外巡检图片质量"
        cadence="按任务"
        summary={summary}
        metrics={metrics}
        meta={metaText}
        onClick={() => setOpen(true)}
      />

      {open && (
        <InfraredQualityBoard
          data={data}
          deptId={deptId}
          onDeptChange={setDeptId}
          running={running}
          onClose={() => setOpen(false)}
          onRefresh={() => void load(true)}
        />
      )}
    </>
  );
}

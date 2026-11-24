import { useCallback, useEffect, useState } from 'react';
import { FileCheck2 } from 'lucide-react';
import { ComplianceMetric, ComplianceTile } from '@/components/compliance-tile';
import { DocUpdateBoard } from '@/components/doc-update-board';
import { DocUpdateProgressBar } from '@/components/doc-update-progress';
import { useConfirm } from '@/components/confirm-dialog';
import {
  deptKey, deptLabel, fmtTime, STATUS_ORDER, STATUS_VISUALS,
} from '@/components/doc-update-status';
import {
  cancelDocUpdateSync, getDocUpdateOverview, triggerDocUpdateSync,
  type DocUpdateOverview, type DocUpdateStatus, type DocUpdateSyncBody,
} from '@/api/scheduledTasks';

/** 只按 §口径 展示四种状态；基线期另说。
 *  轮询期间按钮禁用，后端租约释放即停（租约 120s + 30s 心跳，进程被 kill 也会自愈）。 */
const POLL_INTERVAL = 3000;

/** 后端未下发 `labels` 时的兜底状态词（与 STATUS_VISUALS 的键一一对应）。 */
const STATUS_FALLBACK: Record<DocUpdateStatus, string> = {
  updated: '已更新', stale: '未更新', missing: '未找到', review: '待复核',
  failed: '下载失败', baseline: '基线',
};

export function DocUpdatePanel() {
  const confirm = useConfirm();
  const [data, setData] = useState<DocUpdateOverview | null>(null);
  const [notice, setNotice] = useState('');
  const [open, setOpen] = useState(false);
  const [period, setPeriod] = useState('');
  // 已请求终止，等租约释放（running=false）
  const [cancelling, setCancelling] = useState(false);
  // 正在补跑的目标期与部门：结束后切到该期看结果（跑的过程中不切）
  const [backfill, setBackfill] = useState<{ period: string; dept: string } | null>(null);
  // 默认选第一个部门：打开看板就是该部门的 25 行列表
  const [dept, setDept] = useState('');

  const load = useCallback(async () => {
    try {
      setData(await getDocUpdateOverview(period));
    } catch {
      // 面板是附加信息，取不到不打断合规页
    }
  }, [period]);

  useEffect(() => { void load(); }, [load]);

  // 部门列到达后默认选第一个；之后保持用户的选择
  const firstDept = data?.dept_columns?.length ? deptKey(data.dept_columns[0]) : '';
  useEffect(() => { if (firstDept) setDept(firstDept); }, [firstDept]);

  // 后端持有租约期间（running=true）每 3s 拉一次；租约释放即停，以后端为准。
  // 先立即拉一次：单部门检查整轮只有几十秒，等第一个 3s tick 会一帧进度都看不到。
  useEffect(() => {
    if (!data?.running) return;
    let alive = true;
    const tick = async () => {
      try {
        const next = await getDocUpdateOverview(period);
        if (alive) setData(next);
      } catch {
        // 网络抖动继续轮询
      }
    };
    void tick();
    const timer = window.setInterval(() => { void tick(); }, POLL_INTERVAL);
    return () => { alive = false; window.clearInterval(timer); };
  }, [data?.running, period]);

  // 补跑结束（租约释放）：切到该期看结果，期间下拉里就会出现这条快照
  useEffect(() => {
    if (!backfill || data?.running !== false) return;
    setNotice(`补跑完成：${backfill.dept} · ${backfill.period}（已并入该期快照）`);
    setPeriod(backfill.period);
    setBackfill(null);
  }, [backfill, data?.running]);

  // 已请求终止且租约已释放：提示结果未保存，并刷新回上一期快照
  useEffect(() => {
    if (data?.running || !cancelling) return;
    setCancelling(false);
    setNotice('检查已终止，本次结果未保存');
    void load();
  }, [data?.running, cancelling, load]);

  const onCancel = async () => {
    const ok = await confirm({
      title: '终止本次检查？',
      description: '本次结果不会保存。',
      confirmText: '终止',
      variant: 'destructive',
    });
    if (!ok) return;
    try {
      const res = await cancelDocUpdateSync();
      if (!res.cancelling) {
        setNotice(res.reason || '当前没有正在跑的检查');
        await load();
        return;
      }
      setCancelling(true);
      setBackfill(null);   // 终止后别再往不存在的补跑期切
      setNotice('');   // 进度条自身显示「正在终止…」，这里不重复
    } catch {
      setNotice('终止请求失败，请稍后重试');
    }
  };

  const start = async (target?: DocUpdateSyncBody) => {
    setNotice('');
    setCancelling(false);
    try {
      const res = await triggerDocUpdateSync(target ?? {});
      if (!res.started) {
        setNotice(res.reason || '已有一次检查在跑，请稍候');
        await load();
        return;
      }
      if (target?.period) {
        // 补跑：跑的过程中看板保持原期间（该期快照还没生成），结束后再切
        const col = target.dept_name
          ? data?.dept_columns?.find((c) => c.company_id === target.company_id
            && c.dept_name === target.dept_name)
          : undefined;
        const who = target.dept_name ? (col ? deptLabel(col) : target.dept_name) : '全部部门';
        setBackfill({ period: target.period, dept: who });
        setNotice(`正在补跑 ${who} · ${target.period}（结果并入该期快照）`);
      } else {
        setBackfill(null);
        setPeriod('');   // 新跑的是当前月，回到最新一期
      }
      setData((cur) => (cur ? { ...cur, running: true } : cur));
    } catch {
      setNotice('启动检查失败，请稍后重试');
    }
  };

  const onCheck = () => start();

  // 只查当前选中的部门：其余部门沿用本期已有结果（后端并回同一期快照）
  const onCheckDept = () => {
    const col = data?.dept_columns?.find((c) => deptKey(c) === dept);
    if (!col) {
      setNotice('请先选择部门');
      return;
    }
    return start({ company_id: col.company_id || '', dept_name: col.dept_name });
  };

  // 按指定历史期间补跑一期（可选只跑一个部门；已有快照时 force 覆盖重跑）
  const onBackfill = (target: string, force: boolean,
                      scope?: { company_id: string; dept_name: string }) => {
    void start({ period: target, force, ...scope });
  };

  if (!data?.enabled) return null;

  const verdict = data.verdict;
  const deptColumns = data.dept_columns ?? [];
  const deptCount = deptColumns.length || data.latest?.dept_count || 0;
  const companyCount = new Set(deptColumns.map((c) => c.company_id || c.company_name || '')).size;
  const scopeTitle = companyCount > 1
    ? `${companyCount} 家公司 · ${deptCount} 个部门` : `${deptCount} 个部门`;
  const periodLabel = verdict?.period_label ?? '';

  // 一句语境：运行中显示紧凑进度条；有快照时给检查范围（多公司 · 部门数）与期间
  const summary = data.running
    ? <DocUpdateProgressBar progress={data.progress ?? null} compact />
    : data.missing_period
      ? `该期没有快照（${data.missing_period}），请在看板换一个期间`
      : !data.latest
        ? '尚未建立首期基线，点开看板执行首次检查'
        : [scopeTitle, periodLabel].filter(Boolean).join(' · ');

  // 各状态计数：色表与看板同一份（STATUS_VISUALS），0 值照常显示
  const metrics = !data.running && data.latest
    ? STATUS_ORDER.map((key) => (
      <ComplianceMetric
        key={key}
        label={verdict?.labels?.[key] ?? STATUS_FALLBACK[key]}
        value={verdict?.counts?.[key] ?? 0}
        cls={STATUS_VISUALS[key].chip}
      />
    ))
    : undefined;
  const metaText = data.latest
    ? [periodLabel, `快照 ${fmtTime(data.latest.taken_at)}`].filter(Boolean).join(' · ')
    : scopeTitle;

  return (
    <>
    <ComplianceTile
      icon={FileCheck2}
      tone="bg-chart-2/15 text-chart-2"
      title="文档更新检查"
      cadence="月度"
      summary={summary}
      metrics={metrics}
      meta={metaText}
      onClick={() => setOpen(true)}
    />
    {open && (
      <DocUpdateBoard
        data={data}
        running={data.running}
        notice={notice}
        onCheck={() => void onCheck()}
        onCheckDept={() => void onCheckDept()}
        onBackfill={onBackfill}
        onCancel={() => void onCancel()}
        cancelling={cancelling}
        onClose={() => setOpen(false)}
        dept={dept}
        onDeptChange={setDept}
        period={period}
        onPeriodChange={setPeriod}
      />
    )}
    </>
  );
}

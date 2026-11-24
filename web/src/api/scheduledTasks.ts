import client, { getAccessToken } from './client';
import type { Message } from '../types';

export interface ScheduledTask {
  task_id: string;
  name: string;
  description: string;
  schedule_type: 'once' | 'interval' | 'cron';
  once_at: string | null;
  interval_seconds: number | null;
  cron_expr: string | null;
  prompt: string;
  task_mode: string;
  thread_strategy: 'new' | 'reuse';
  enabled: boolean;
  next_run_at: string | null;
  created_by: string;
  created_at: string;
  updated_at: string;
  /** 由「分析报告」页从模板创建时写入；普通自定义任务为 null —— 用它筛出报告类任务 */
  template_id?: string | null;
}

export interface ReportPreset {
  key: string;
  name: string;
  summary: string;
  capability_id: string;
  frequency: string;
  cron: string;
  ticket_type: string;    // change / breakdown / question / event / threshold
  ticket_name: string;    // 变更工单 / 故障工单 / …
  space: 'nationwide' | 'by_park';
  space_label: string;    // 全国 / 廊坊园区（园区级显示具体园区名）
  park: string;           // nationwide 或园区短名
  park_label: string;
  period: 'weekly' | 'monthly';
  period_label: string;   // 每周 / 每月
  task_id: string;        // 对应的系统任务（未初始化时为空串）
  enabled: boolean;
  next_run_at: string | null;
}

export interface PresetRun {
  run_id: string;
  status: 'pending' | 'running' | 'success' | 'failed';
  triggered_at: string;
  duration_ms: number;
  error: string;
  artifact_urls: string[];
  preset_key: string;
  preset_name: string;
  ticket_name: string;     // 事件工单 / 变更工单 …（列表行主文本用）
  space_label: string;     // 全国 / 廊坊园区 …（与 ticket_name 组成「区分性信息」）
  period_key: string;      // 2026-W41 / 2026-09
  period_label: string;    // 2026 第 41 周 / 2026 年 9 月
  period_type_label: string; // 每周 / 每月（行标题用；与期号 period_label 不同义）
  teaser: string;          // 一句话摘要（列表行当 excerpt 用；后端只取一句，不回整段回复）
}

export interface PresetFilter {
  ticket_type?: string;
  space?: string;
  period?: string;
  park?: string;          // nationwide 或园区短名
  days?: number;
}

export interface ScheduledTaskRun {
  run_id: string;
  task_id: string;
  thread_id: string;
  status: 'pending' | 'running' | 'success' | 'failed';
  triggered_at: string;
  final_reply: string;
  artifact_urls: string[];
  error: string;
  duration_ms: number;
  created_at: string;
}

export interface ScheduledTaskNotification {
  notification_id: string;
  user_id: string;
  task_id: string;
  task_name: string;
  run_id: string;
  status: 'success' | 'failed';
  title: string;
  body: string;
  created_at: string;
}

export interface ScheduledTaskInput {
  name: string;
  description?: string;
  schedule_type: 'once' | 'interval' | 'cron';
  once_at?: string | null;
  interval_seconds?: number | null;
  cron_expr?: string | null;
  prompt: string;
  task_mode?: string;
  thread_strategy?: 'new' | 'reuse';
  enabled?: boolean;
}

async function unwrap<T>(resp: { data: { data: T } }): Promise<T> {
  return resp.data.data;
}

export async function listTasks(): Promise<ScheduledTask[]> {
  const resp = await client.get('/scheduled-tasks');
  return unwrap<ScheduledTask[]>(resp);
}

export async function createTask(input: ScheduledTaskInput): Promise<ScheduledTask> {
  const resp = await client.post('/scheduled-tasks', input);
  return unwrap<ScheduledTask>(resp);
}

export async function updateTask(
  taskId: string,
  input: Partial<ScheduledTaskInput>,
): Promise<ScheduledTask> {
  const resp = await client.patch(`/scheduled-tasks/${taskId}`, input);
  return unwrap<ScheduledTask>(resp);
}

export async function deleteTask(taskId: string): Promise<void> {
  await client.delete(`/scheduled-tasks/${taskId}`);
}

export async function triggerTask(taskId: string): Promise<void> {
  await client.post(`/scheduled-tasks/${taskId}/trigger`);
}

export async function listRuns(taskId: string): Promise<ScheduledTaskRun[]> {
  const resp = await client.get(`/scheduled-tasks/${taskId}/runs`);
  return unwrap<ScheduledTaskRun[]>(resp);
}

export async function listReportPresets(
  params: PresetFilter & { offset?: number; limit?: number } = {},
): Promise<{
  presets: ReportPreset[]; runs: PresetRun[]; total: number;
  statusCounts: Record<string, number>;   // 全量口径的状态计数（后端聚合，不随分页变）
}> {
  const resp = await client.get('/scheduled-tasks/presets', { params });
  const d = resp.data.data ?? { presets: [], runs: [] };
  // 兜底：旧版后端不返回 total/status_counts（滚动重启期间），用当前页条数顶上，别让界面出现空白
  return {
    presets: d.presets ?? [],
    runs: d.runs ?? [],
    total: d.total ?? (d.runs?.length ?? 0),
    statusCounts: d.status_counts ?? {},
  };
}

/** 取报告产物（PDF）为 Blob —— 该端点需 Bearer 头，iframe 带不了，必须先 fetch。 */
export async function fetchPresetArtifactBlob(runId: string, filename: string): Promise<Blob> {
  const token = getAccessToken();
  const resp = await fetch(presetArtifactUrl(runId, filename), {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  });
  if (!resp.ok) throw new Error('报告文件获取失败');
  return resp.blob();
}

export async function getPresetRunMessages(runId: string): Promise<Message[]> {
  const resp = await client.get(`/scheduled-tasks/presets/runs/${runId}/messages`);
  return resp.data.messages ?? [];
}

export function presetArtifactUrl(runId: string, filename: string): string {
  return `/api/v1/scheduled-tasks/presets/runs/${runId}/artifacts/${filename}`;
}

// ==================== 告警不合规检查（系统预设 · 共享只读）====================

/** 检查项**完全由后端注册表驱动**：加检查项时前端不用改（别在页面里写死名字）。 */
export interface ComplianceCheck {
  key: string;
  name: string;
  cadence: string;      // 班次 / 以后可能有 周
  description: string;  // 口径说明
  sheet: string;
}

export interface ComplianceSpec {
  title: string;
  checks: ComplianceCheck[];
  cadences: string[];
}

export interface ComplianceRun {
  run_id: string;
  status: 'pending' | 'running' | 'success' | 'failed';
  triggered_at: string;
  duration_ms: number;
  error: string;
  period_key: string;     // 2026-10-27-n / 2026-10-27-d
  period_label: string;   // 2026-10-27 夜班（10-26 17:30 ~ 10-27 08:30）
  teaser: string;
  /** 各检查项命中数 {check_key: rows}；失败或老记录为空 → 卡片按「无数字」降级 */
  check_counts?: Record<string, number>;
  files: string[];        // 产物文件名（走 run_id + filename 下载）
}

export interface ComplianceSummaryItem extends ComplianceCheck {
  rows: number;
}

export interface ComplianceRunSheets {
  run_id: string;
  period_key: string;
  period_label: string;
  triggered_at: string;
  filename: string;
  summary: ComplianceSummaryItem[];
  sheets: { key: string; name: string; rows: number }[];
}

export async function getComplianceSpec(): Promise<ComplianceSpec> {
  const resp = await client.get('/scheduled-tasks/compliance/spec');
  return unwrap<ComplianceSpec>(resp);
}

export async function listComplianceRuns(
  params: {
    offset?: number; limit?: number; days?: number; cadence?: string;
    /** 期号日期区间（YYYY-MM-DD，含起止两天）；不传＝不限 */
    start?: string; end?: string;
  } = {},
): Promise<{ runs: ComplianceRun[]; total: number; statusCounts: Record<string, number> }> {
  const resp = await client.get('/scheduled-tasks/compliance', { params });
  const d = resp.data.data ?? { runs: [], total: 0 };
  return {
    runs: d.runs ?? [],
    total: d.total ?? (d.runs?.length ?? 0),
    statusCounts: d.status_counts ?? {},
  };
}

export async function getComplianceRunSheets(runId: string): Promise<ComplianceRunSheets> {
  const resp = await client.get(`/scheduled-tasks/compliance/runs/${runId}/sheets`);
  return unwrap<ComplianceRunSheets>(resp);
}

/** 单个检查项明细的**原始** JSON 地址（`RecordsTable` 自己拉，不套 `{ok, data}`）。 */
export function complianceSheetUrl(runId: string, key: string): string {
  return `/api/v1/scheduled-tasks/compliance/runs/${runId}/sheets/${encodeURIComponent(key)}`;
}

export function complianceArtifactUrl(runId: string, filename: string): string {
  return `/api/v1/scheduled-tasks/compliance/runs/${runId}/artifacts/${encodeURIComponent(filename)}`;
}

/** 下载 xlsx：端点要 Bearer 头，`<a download>` 带不了，先 fetch 成 Blob。 */
export async function fetchComplianceArtifactBlob(runId: string, filename: string): Promise<Blob> {
  const token = getAccessToken();
  const resp = await fetch(complianceArtifactUrl(runId, filename), {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  });
  if (!resp.ok) throw new Error('文件下载失败');
  return resp.blob();
}

// ==================== 数据中心事件统计表（系统预设 · 共享只读）====================
// 入口是飞书园区群通报（速报 + 首报）。现在**一班次一条 run、一份 xlsx**（全部园区合并）；
// 历史期仍是旧的「一园区一条 run」，后端按规则二选一，不会累加。

export interface IncidentParkRun {
  park: string;          // all（全部园区合并）/ 廊坊 / 平湖 …（preset_key 里的园区短名）
  park_label: string;    // 全部园区 / 廊坊园区
  run_id: string;
  status: 'pending' | 'running' | 'success' | 'failed';
  count: number | null;  // 本期事件条数；失败或老记录取不到为 null
  triggered_at: string;
  duration_ms: number;
  error: string;
  files: string[];       // 走 run_id + filename 下载
}

export interface IncidentPeriod {
  period_key: string;    // 2026-11-06-d（班次）/ 2026-W43（历史周报）
  period_label: string;  // 2026-11-06 白班（11-06 08:30 ~ 11-06 17:30）
  window: string;        // 2026-10-19 ~ 2026-10-25；失败/老记录为空串
  parks: IncidentParkRun[];
  total_count: number;
  parks_total: number;
  finished: number;
  failed: number;
}

export async function listIncidentPeriods(periods = 4): Promise<IncidentPeriod[]> {
  const resp = await client.get('/scheduled-tasks/incidents', { params: { periods } });
  return resp.data.data?.periods ?? [];
}

export function incidentArtifactUrl(runId: string, filename: string): string {
  return `/api/v1/scheduled-tasks/incidents/runs/${runId}/artifacts/${encodeURIComponent(filename)}`;
}

/** 下载 xlsx：端点要 Bearer 头，`<a download>` 带不了，先 fetch 成 Blob。 */
export async function fetchIncidentArtifactBlob(runId: string, filename: string): Promise<Blob> {
  const token = getAccessToken();
  const resp = await fetch(incidentArtifactUrl(runId, filename), {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  });
  if (!resp.ok) throw new Error('文件下载失败');
  return resp.blob();
}

/** 整期（各园区合并）的 Sheet 清单：sheet 名与合并后的行数。 */
export interface IncidentSheetInfo { key: string; name: string; rows: number }

export interface IncidentPeriodSheets {
  period_key: string;
  period_label: string;
  window: string;
  failed: number;
  sheets: IncidentSheetInfo[];
  /** 产物缺失时为 false（端点仍返回 200，看板显示空状态而不是报错） */
  available?: boolean;
  reason?: string;
}

export async function getIncidentPeriodSheets(periodKey: string): Promise<IncidentPeriodSheets> {
  const resp = await client.get(
    `/scheduled-tasks/incidents/periods/${encodeURIComponent(periodKey)}/sheets`,
  );
  return resp.data.data as IncidentPeriodSheets;
}

/** 整期合并的单个 Sheet：列与 xlsx 完全一致，直接给 RecordsTable 消费。 */
export function incidentPeriodSheetUrl(periodKey: string, name: string): string {
  return `/api/v1/scheduled-tasks/incidents/periods/${encodeURIComponent(periodKey)}`
    + `/sheets/${encodeURIComponent(name)}`;
}

// ==================== 每日变更统计（园区日常运维群 · 共享只读）====================
// 数据来自飞书园区群「人发的变更通知」，按自然日（Asia/Shanghai 00:00–24:00）统计。

export interface ChangeNoticePark {
  park: string;
  total: number;
  by_type: Record<string, number>;
  by_status: Record<string, number>;
  /** 等级匹配计数（符合 / 不符合 / 无法判定） */
  by_match?: Record<string, number>;
}

export interface ChangeNoticeOverview {
  date: string;
  total: number;
  /** 只含当天有数据的园区（筛选下拉用前端固定列表，不看这里） */
  parks: ChangeNoticePark[];
  /** 未配置园区群等场景的说明（此时 parks 为空） */
  note?: string;
}

export interface ChangeNoticeRecords {
  columns: string[];
  records: Record<string, unknown>[];
  note?: string;
}

/** 某自然日各园区的变更条数与类型/状态分布（`date` 空 = 今天）。 */
export async function getChangeNoticeOverview(date = ''): Promise<ChangeNoticeOverview> {
  // 冷算（首次进入 / 新消息后第一轮）偶尔接近 30s，默认超时不够用
  const resp = await client.get('/scheduled-tasks/change-notices/overview', {
    timeout: 60000,
    ...(date ? { params: { date } } : {}),
  });
  return resp.data.data as ChangeNoticeOverview;
}

/** 明细原始地址（`RecordsTable` 自己拉，不套 `{ok, data}`）。 */
export function changeNoticeRecordsUrl(params: { date?: string; park?: string } = {}): string {
  const q = new URLSearchParams();
  if (params.date) q.set('date', params.date);
  if (params.park) q.set('park', params.park);
  const qs = q.toString();
  return `/api/v1/scheduled-tasks/change-notices/records${qs ? `?${qs}` : ''}`;
}

/** 导出 Excel 地址（给 `downloadArtifact` 直接使用，**不带** `/api/v1` 前缀）。 */
export function changeNoticeExportUrl(params: { date?: string; park?: string } = {}): string {
  const q = new URLSearchParams();
  if (params.date) q.set('date', params.date);
  if (params.park) q.set('park', params.park);
  const qs = q.toString();
  return `/scheduled-tasks/change-notices/export${qs ? `?${qs}` : ''}`;
}

// ==================== 文档更新检查（月度快照 · 共享只读）====================

export type DocUpdateStatus =
  | 'updated' | 'stale' | 'missing' | 'review' | 'failed' | 'baseline';

export type DocUpdateLayout = 'period' | 'flat';
/** 判定口径（取自更新目录 PDF 第三列）：update = 查更新（sha256），new = 查新增 */
export type DocUpdateJudge = 'update' | 'new';

export interface DocUpdateFile {
  file_name: string;
  status: DocUpdateStatus;
  status_label?: string;
  /** 文件所在目录名解析出的期号（查新增且路径有月份目录时才有） */
  period?: string;
  path?: string;
  warn: string;
  error: string;
}

export interface DocUpdateItem {
  seq?: number;
  company_id?: string;
  company_name?: string;
  dept_id?: string;
  dept_name?: string;
  number: string;
  name: string;
  cadence: string;
  note: string;
  /** update = 查更新（比 sha256）；new = 查新增（看本期是否交了新文件） */
  judge?: DocUpdateJudge;
  /** 查新增项的形态：period = 看月份期号；flat = 看上期没有的新文件 */
  layout?: DocUpdateLayout;
  expected_period?: string;
  latest_period?: string;
  /** 扁平查新增项的期号来源：name = 文件名确定性解析；llm = 模型兜底；'' = 没解析出 */
  period_source?: 'name' | 'llm' | '';
  /** 实际命中的目录名：清单名只是模糊叫法，以实际目录为准 */
  actual_dirs?: string[];
  /**
   * path = 只在该清单项配置的目录子树里取文件；heuristic = 自动匹配；
   * llm = 确定性匹配未命中、由模型在候选清单里复核到；
   * agent = 疑似停在模板上，由受控子 agent 在目录图内按名称模糊匹配重定位到
   */
  mapped_by?: 'path' | 'heuristic' | 'llm' | 'agent';
  /** 模型判定置信度：high = 模型判定；low = 建议人工复核；'' = 非模型判定 */
  confidence?: 'high' | 'low' | '';
  /** 低置信度时说明为何判不了、为何需人工复核（如「文件只写了月份、没有年份」） */
  confidence_reason?: string;
  /** 模型复核/子 agent 定位给出的一句依据（其余来源为空） */
  match_reason?: string;
  /** 实际查找路径（优先命中文件真实目录、含年/月；无命中回退公共父目录/钉点层） */
  search_path?: string;
  /** 「未找到」项在该范围内看到的真实文件名（已命中时为空） */
  dir_files?: string[];
  /** 模型给出的疑似文件（仅「待复核」非空）；只是提示，不算已命中 */
  review_files?: string[];
  /** 带月份线索但没有年份锚的文件（仅线上更新项改判为查新增的部门）；待人工确认，不改状态 */
  pending_files?: string[];
  /** 「未找到」项的目录截图（同步时自动截；失败只有 error，不影响判定） */
  screenshot?: { name: string; error: string; captured_at: string } | null;
  /** 配置的目录段（相对部门根），未配置为空 */
  config_path?: string[];
  /** 该项的提示（如配置路径末段是期号容器） */
  warn?: string;
  status: DocUpdateStatus;
  status_label: string;
  error: string;
  /** 列出的文件数（查新增项只列本期相关文件，历史文件不列） */
  file_count: number;
  /** 匹配到的文件总数（含未列出的历史文件） */
  matched_count?: number;
  changed_at: string | null;
  changed_days_ago: number | null;
  files: DocUpdateFile[];
}

export interface DocUpdateDept {
  company_id?: string;
  company_name?: string;
  dept_id: string;
  dept_name: string;
  root_fid?: string;
  error: string;
  counts: Partial<Record<DocUpdateStatus, number>>;
  total: number;
}

export interface DocUpdateWarning {
  company_id?: string;
  company_name?: string;
  dept_id?: string;
  dept_name?: string;
  number: string;
  name: string;
  file_name: string;
  warn: string;
}

export interface DocUpdateChange {
  company_id?: string;
  company_name?: string;
  dept_id?: string;
  dept_name?: string;
  number: string;
  name: string;
  file_name: string;
  changed_at: string | null;
}

export interface DocUpdateVerdict {
  baseline: boolean;
  /** cross 与上一期（上月）比 / baseline 没有上期，只建基线 */
  mode: 'cross' | 'baseline';
  /** dept = 部门 × 清单项矩阵；tree = 旧单树（无 depts 时回退） */
  scope?: 'dept' | 'tree';
  /** 状态展示词（已更新 / 未更新 / 未找到 / 下载失败 / 基线） */
  labels: Partial<Record<DocUpdateStatus, string>>;
  period_key: string;
  period_label: string;
  taken_at: string | null;
  counts: Partial<Record<DocUpdateStatus, number>>;
  total: number;
  dept_count?: number;
  depts?: DocUpdateDept[];
  items: DocUpdateItem[];
  warnings: DocUpdateWarning[];
  changes: {
    changed: DocUpdateChange[];
    added_fids: string[];
    removed_fids: string[];
  };
}

export interface DocUpdateSnapshotMeta {
  snapshot_id: string;
  taken_at: string;
  period_key: string;
  period_label: string;
  file_count: number;
  elapsed: number;
  scope?: 'dept' | 'tree';
  dept_count?: number;
}

/** 后台检查进度：有租约即 running，进程被 kill 后 ≤2 分钟自动消失 */
export interface DocUpdateProgress {
  mode: string;              // manual / auto / legacy
  user_id: string;
  started_at: number | null; // epoch 秒
  /** 每部门 = 遍历 25 步 + 逐项 25 步，两阶段都推进 */
  done: number;
  total: number;
  /** 当前项，如「A7 · 1.4.5 能源管理报告」；遍历阶段带「已访问 N 个目录 / M 个文件」 */
  current: string;
  /** walk＝正在并发遍历共享目录；items＝逐项判定（两者都推进 done/total） */
  phase?: 'walk' | 'items' | '';
  /** 已请求终止，等正在跑的 worker 走到下一个进度点 */
  cancelling?: boolean;
}

/** 固定部门列：来自配置 + 注册表缓存，不等快照、不随每次检查变化（同名部门按公司区分） */
export interface DocUpdateDeptColumn {
  company_id?: string;
  company_name?: string;
  dept_id: string;
  dept_name: string;
  error?: string;
}

/** 25 条月度项（行顺序固定，页面按它对齐） */
export interface DocUpdateCatalogItem {
  seq: number;
  number: string;
  name: string;
}

/** 历史期（页面「期间」下拉） */
export interface DocUpdatePeriod {
  period_key: string;
  period_label: string;
  taken_at: string;
  scope: 'dept' | 'tree';
  dept_count: number;
}

export interface DocUpdateOverview {
  enabled: boolean;
  latest: DocUpdateSnapshotMeta | null;
  verdict: DocUpdateVerdict | null;
  summary: string;
  running: boolean;
  progress?: DocUpdateProgress | null;
  dept_columns?: DocUpdateDeptColumn[];
  catalog?: DocUpdateCatalogItem[];
  periods?: DocUpdatePeriod[];
  /** 请求的历史期没有快照时为该期号，否则空串 */
  missing_period?: string;
}

export async function getDocUpdateOverview(period = ''): Promise<DocUpdateOverview> {
  const resp = await client.get('/scheduled-tasks/doc-update/overview',
    period ? { params: { period } } : undefined);
  return resp.data.data as DocUpdateOverview;
}

export interface DocUpdateSyncBody {
  /** 补跑历史期（YYYY-MM）；不传＝当月 */
  period?: string;
  /** 与 period 联用：该期已有快照时覆盖重跑 */
  force?: boolean;
  /** 只跑一个部门（与 DOC_UPDATE_DEPTS 固定清单比对，不在清单 400） */
  company_id?: string;
  dept_name?: string;
}

export async function triggerDocUpdateSync(body: DocUpdateSyncBody = {}): Promise<{
  started: boolean;
  reason?: string;
  scope?: string;
  dept?: string;
  /** 补跑的目标期（不传 period 时为空串） */
  period?: string;
}> {
  const resp = await client.post('/scheduled-tasks/doc-update/sync', body);
  return resp.data.data as {
    started: boolean;
    reason?: string;
    scope?: string;
    dept?: string;
    period?: string;
  };
}

/** 终止正在跑的检查：写 Redis 取消旗标，跑着的 worker 在下一个进度点停下，本轮不落库 */
export async function cancelDocUpdateSync(): Promise<{
  cancelling: boolean;
  reason?: string;
}> {
  const resp = await client.post('/scheduled-tasks/doc-update/cancel');
  return resp.data.data as { cancelling: boolean; reason?: string };
}

// ==================== 红外巡检图片质量检测（按任务勾选 · 共享只读）====================

export interface InfraredQualityDept {
  company_id?: string;
  company_name?: string;
  dept_id: string;
  dept_name: string;
  error?: string;
}

/** 判定四档（与后端 counts 的键逐字一致） */
export interface InfraredQualityCounts {
  合格?: number;
  待复核?: number;
  不合格?: number;
  检测失败?: number;
}

export interface InfraredQualityTask {
  task_id: string;
  inspector: string;
  status: number | null;        // 1 = 进行中（不可勾选）
  device_count: number;
  photo_count: number | null;   // 本次巡检有图片的设备数（未检测过为 null）
  start_time: number;           // epoch ms；进行中为 0
  end_time: number;
  checked_at: string | null;    // 空 = 未检测
  result_status: 'success' | 'partial' | 'failed' | 'no_image' | '';
  counts: InfraredQualityCounts;
}

/** 后台进度：phase = detail（取详情）/ predict（推理） */
export interface InfraredQualityProgress {
  mode: string;
  user_id: string;
  started_at: number | null;
  phase: 'detail' | 'predict' | '';
  done: number;
  total: number;
  current: string;
}

export interface InfraredQualityRun {
  run_id: string;
  status: string;
  created_at: string | null;
  counts: InfraredQualityCounts;
  error: string;
  elapsed: number;
  task_ids: string[];
}

export interface InfraredQualityOverview {
  enabled: boolean;
  departments: InfraredQualityDept[];
  dept: InfraredQualityDept | null;
  tasks: InfraredQualityTask[];
  running: boolean;
  progress: InfraredQualityProgress | null;
  latest_run: InfraredQualityRun | null;
}

export async function getInfraredQualityTasks(
  deptId = '', refresh = false,
): Promise<InfraredQualityOverview> {
  const resp = await client.get('/scheduled-tasks/infrared-quality/tasks', {
    params: { dept_id: deptId, refresh },
  });
  return resp.data.data as InfraredQualityOverview;
}

export async function triggerInfraredQualityCheck(input: {
  dept_id: string; task_ids: string[]; refresh?: boolean;
}): Promise<{ started: boolean; reason?: string; task_ids?: string[] }> {
  const resp = await client.post('/scheduled-tasks/infrared-quality/check', input);
  return resp.data.data as { started: boolean; reason?: string; task_ids?: string[] };
}

/** 明细 JSON 的原始地址（`RecordsTable` 自己拉，不套 `{ok, data}`）。 */
export function infraredQualityRecordsUrl(params: { runId?: string; taskId?: string }): string {
  const q = new URLSearchParams();
  if (params.runId) q.set('run_id', params.runId);
  if (params.taskId) q.set('task_id', params.taskId);
  return `/api/v1/scheduled-tasks/infrared-quality/records?${q.toString()}`;
}

// ==================== 应急演练记录合规审查（按周 · 共享只读）====================

export interface DrillAuditDept {
  company_id: string;
  company_name: string;
  dept_id: string;
  dept_name: string;
  error: string;
}

export interface DrillAuditSeverityCounts {
  高?: number;
  中?: number;
  低?: number;
}

export interface DrillAuditStep {
  seconds: number;
  time: string;
  text: string;
  /** 1 基步骤序号 */
  seq?: number;
  /** 本步耗时（秒，＝下一步 seconds − 本步；末步 null） */
  delta?: number | null;
}

export interface DrillAuditDimension {
  /** role_assignment / step_completeness / time_limit / compliance / critical_items */
  key: string;
  /** 达标 / 偏差 / 缺失 / 无法判定 */
  verdict: string;
  detail: string;
  basis: string;
}

/** 漏做步骤：EOP 要求有、演练记录里没做（没有演练侧 seq） */
export interface DrillAuditMissingStep {
  eop_step: string;
  executor: string;
  time_limit: string;
  severity: string;
  note: string;
  basis: string;
}

/** 演练里存在、但做错的步（有 seq，可标在时间轴） */
export interface DrillAuditStepFinding {
  seq: number;
  /** 超时 / 违规 */
  status: string;
  /** 累计 / 本步 */
  metric: string;
  limit: string;
  actual?: number | null;
  note: string;
}

export interface DrillAuditTimelineStats {
  count?: number;
  first_seconds?: number;
  total_seconds?: number;
  max_delta?: number | null;
  min_delta?: number | null;
  avg_delta?: number | null;
}

export interface DrillAuditGroup {
  letter: string;
  file_id?: string;
  file_name?: string;
  /** ok / missing / ocr_failed / no_eop / judge_failed */
  status: string;
  chars?: number;
  issue_count: number;
  severity_counts: DrillAuditSeverityCounts;
  summary: string;
  error: string;
  /** 详情端点补充：内容库文本仍可读时为 true（否则场景/步骤/流程图缺省） */
  steps_available?: boolean;
  scene?: string;
  roles?: string;
  steps?: DrillAuditStep[];
  /** 分车道结构化步骤（页面画「分车道时间线」；空则回退到 steps） */
  lanes?: DrillAuditLane[];
  /** 已剥掉 ```mermaid 围栏，可直接交给 MermaidRenderer */
  flow?: string;
  /** 五维评估（岗位分配/步骤完整性/时限执行/操作合规/关键项） */
  dimensions?: DrillAuditDimension[];
  /** 漏做步骤（EOP 有、演练缺） */
  missing_steps?: DrillAuditMissingStep[];
  /** 演练里做错的步（超时/违规，带 seq） */
  step_findings?: DrillAuditStepFinding[];
  /** 时间轴汇总 */
  timeline_stats?: DrillAuditTimelineStats;
  /** 原始解析文本（OCR 逐行文字；内容库文本已淘汰时为空串） */
  raw_text?: string;
}

export interface DrillAuditLane {
  name: string;
  steps: DrillAuditStep[];
}

export interface DrillAuditIssue {
  group: string;
  aspect: string;
  problem: string;
  eop_basis: string;
  drill_evidence: string;
  severity: string;
  suggestion: string;
  /** 考核维度（步骤完整性/时限执行/…；镜像进来的漏做步骤为 step_completeness） */
  dimension?: string;
  /** 对应的演练侧步骤序号（可标在时间轴；漏做无此值） */
  seq?: number;
}

export interface DrillAuditWeekSummary {
  _id: string;              // = week_id
  plan_id: string;
  plan_name: string;
  speciality: string;
  dept_id: string;
  dept_name: string;
  year: number;
  week_no: string;
  stime: string;
  etime: string;
  finish_status: string;
  groups: DrillAuditGroup[];
  summary: string;
  note: string;
  /** dialog = 对话审查落库；weekly = 每周任务落库 */
  audit_source: string;
  audited_at: string;
  run_id?: string;
  period_key?: string;
  period_label?: string;
}

export interface DrillAuditLatestRun {
  status?: string;
  triggered_at?: string;
  final_reply?: string;
  error?: string;
  check_counts?: Record<string, number>;
}

export interface DrillAuditPeriod {
  key: string;   // 2026-04
  label: string;
}

/** 周执行状态（与后端 codes.FINISH_STATUS 同值）。 */
export const FINISH_STATUS: Record<string, string> = {
  '1': '未开始',
  '2': '进行中',
  '3': '已完成',
  '4': '已逾期',
};

/** 计划里的一周：`key` 是列 key（如 2026-W45），`week_id` 用于单周执行。 */
export interface DrillAuditPlanWeek {
  key: string;
  week_id: string;
  finish_status: string;
  /** 已上传记录的班组合集（如 "A,B"；空 = 本周还没交记录） */
  file_status: string;
}

/** 扫描时缓存的演练计划（看板矩阵的行）；`weeks` 是该计划的已排期周。 */
export interface DrillAuditPlanRow {
  plan_id: string;
  plan_name: string;
  dept_id: string;
  dept_name: string;
  company_name: string;
  speciality: string;
  year: number;
  weeks: DrillAuditPlanWeek[];
}

/** 单周/批量执行入参：页面从排期格里拿到的周。 */
export interface DrillAuditRunItem {
  week_id: string;
  dept_id?: string;
  year?: number;
}

/** 全部排期周（看板矩阵的列；未审的周没有周记录可借用时间）。 */
export interface DrillAuditPlanColumn {
  key: string;
  week_no: string;
  stime: string;
  etime: string;
}

export interface DrillAuditOverview {
  enabled: boolean;
  depts: DrillAuditDept[];
  periods: DrillAuditPeriod[];
  weeks: DrillAuditWeekSummary[];
  /** 计划清单缓存（从未扫描过时为空数组，前端退回只看已审行） */
  plan_rows: DrillAuditPlanRow[];
  plan_columns: DrillAuditPlanColumn[];
  running: boolean;
  /** 审查进行中的分步进度文案（未运行或拿不到为空串） */
  progress?: string;
  latest_run: DrillAuditLatestRun;
  /** 计划清单是否正在后台刷新（打开看板时缓存为空或超 24h 自动触发） */
  plan_refreshing: boolean;
  /** 清单最近一次刷新时间（ISO；从未刷新过为空串） */
  plan_updated_at?: string;
}

export interface DrillAuditWeekMeta {
  week_id: string;
  plan_id: string;
  plan_name: string;
  speciality: string;
  dept_id: string;
  dept_name: string;
  year?: number;
  week_no: string;
  stime: string;
  etime: string;
  finish_status: string;
  audit_source: string;
  audited_at: string;
  run_id?: string;
  period_key?: string;
  period_label?: string;
}

export interface DrillAuditWeekDetail {
  week: DrillAuditWeekMeta;
  groups: DrillAuditGroup[];
  issues: DrillAuditIssue[];
  summary: string;
  note: string;
}

export async function getDrillAuditOverview(period = ''): Promise<DrillAuditOverview> {
  const resp = await client.get('/scheduled-tasks/drill-audit/overview',
    period ? { params: { period } } : undefined);
  return resp.data.data as DrillAuditOverview;
}

export async function triggerDrillAuditRun(body: {
  dept_names?: string[];
  /** 单周/批量勾选执行；非空时后端只跑这些周 */
  items?: DrillAuditRunItem[];
  refresh?: boolean;
} = {}): Promise<{ started: boolean; reason?: string }> {
  const resp = await client.post('/scheduled-tasks/drill-audit/run', body);
  return resp.data.data as { started: boolean; reason?: string };
}

/** 只刷新看板用的计划清单（不跑审查；看板打开且缓存空/过期时自动调用）。 */
export async function refreshDrillAuditRoster(body: {
  dept_names?: string[];
} = {}): Promise<{ started: boolean; reason?: string }> {
  const resp = await client.post('/scheduled-tasks/drill-audit/roster', body);
  return resp.data.data as { started: boolean; reason?: string };
}

export async function getDrillAuditWeek(weekId: string): Promise<DrillAuditWeekDetail> {
  const resp = await client.get(
    `/scheduled-tasks/drill-audit/weeks/${encodeURIComponent(weekId)}`,
  );
  return resp.data.data as DrillAuditWeekDetail;
}

export async function listNotifications(): Promise<ScheduledTaskNotification[]> {
  const resp = await client.get('/scheduled-tasks/notifications');
  return unwrap<ScheduledTaskNotification[]>(resp);
}

export async function getRunMessages(taskId: string, runId: string): Promise<Message[]> {
  const resp = await client.get(`/scheduled-tasks/${taskId}/runs/${runId}/messages`);
  return resp.data.messages ?? [];
}

export function artifactDownloadUrl(taskId: string, runId: string, filename: string): string {
  return `/api/v1/scheduled-tasks/${taskId}/runs/${runId}/artifacts/${filename}`;
}

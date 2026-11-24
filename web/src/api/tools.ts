import client from './client';

/** always = 常驻可见；on_demand = 经 tool_search 按需检索；mode_gated = 仅特定任务模式可用 */
export type ToolVisibility = 'always' | 'on_demand' | 'mode_gated';

export interface ToolItem {
  /** 工具名，如 mgmt__query_my_info / analysis_exec */
  name: string;
  title: string;
  /** 后端自动截取的描述首句（≤60 字），供页面展示 */
  summary: string;
  description: string;
  category_label: string;
  domain_label: string;
  /** 调用该工具所需的额外权限（空 = 沿用门面权限） */
  required_perm: string;
  visibility: ToolVisibility;
  mode_gated: boolean;
}

export interface ToolGroup {
  key: string;
  label: string;
  description: string;
  /** 内置工具组的配置开关（常开组为空串） */
  switch?: string;
  tools: ToolItem[];
}

async function unwrap<T>(resp: { data: { data: T } }): Promise<T> {
  return resp.data.data;
}

let cache: ToolGroup[] | null = null;
let inflight: Promise<ToolGroup[]> | null = null;

/** 同步取缓存：进页面已预取过则打开小窗直接渲染，不闪加载态 */
export function getCachedTools(): ToolGroup[] | null {
  return cache;
}

/** 进页面预取；失败静默，首次打开小窗会重试并提示 */
export function prefetchTools(): void {
  void listTools().catch(() => {});
}

export async function listTools(): Promise<ToolGroup[]> {
  if (cache) return cache;
  if (!inflight) {
    inflight = (async () => {
      try {
        const resp = await client.get('/tools');
        const data = await unwrap<{ groups: ToolGroup[] }>(resp);
        const groups = data.groups ?? [];
        cache = groups;
        return groups;
      } finally {
        inflight = null;
      }
    })();
  }
  return inflight;
}

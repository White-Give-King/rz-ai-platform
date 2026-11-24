import {
  useState, useCallback, useEffect, useRef, useMemo, memo, type ReactNode,
} from 'react';
import { createPortal } from 'react-dom';
import {
  Bot, Sparkles, Trash2, Search, Globe, Wrench, SquareTerminal, NotebookPen, BookOpenText,
  ChevronUp, ChevronDown, Dot, Clock3, FileText, Copy, Check,
  AlertCircle, RotateCcw, Loader2, MoreHorizontal, Pin, PinOff, Pencil, User,
} from 'lucide-react';
import { Collapsible } from '@base-ui/react/collapsible';
import { Input } from './ui/input';
import MarkdownContent from './MarkdownContent';
import { CodeHighlight } from './CodeHighlight';
import ChartRenderer from './ChartRenderer';
import FileCard from './FileCard';
import ArtifactPanel from './ArtifactPanel';
import TodoTray from './TodoTray';
import { useConfirm } from './confirm-dialog';

function fixMermaidBlocks(text: string): string {
  if (!text) return text;
  let result = text;
  result = result.replace(
    /mermaid(graph\s+\w+[\s\S]*?)(?=##|---|\*\*|```|$)/gm,
    (_match, code) => {
      let body = code.trim();
      const dirMatch = body.match(/^(graph\s+\w+)\s*/);
      const dir = dirMatch ? dirMatch[1] : 'graph LR';
      if (dirMatch) body = body.slice(dirMatch[0].length);
      body = body.replace(/\["\s+/g, '["').replace(/\s+"\]/g, '"]');
      // 节点标签含 | 但未加引号，自动加双引号（否则 mermaid 解析报错）
      body = body.replace(/\[([^\]"]*?[|][^\]"]*?)\]/g, '["$1"]');
      body = body.replace(/\(([^)"]*?[|][^)"]*?)\)/g, '("$1")');
      body = body
        .replace(/(\)\]?)\s+(\w)/g, '$1\n$2')
        .replace(/]\s+(\w)/g, ']\n$1')
        .replace(/"\s+(\w)/g, '"\n$1')
        .trim();
      return `\n\n\`\`\`mermaid\n${dir}\n${body}\n\`\`\`\n`;
    },
  );
  return result;
}

function hideIncompleteMermaid(text: string): string {
  // 流式输出时，隐藏未闭合的 mermaid 代码块以避免显示原始语法
  const lastOpen = text.lastIndexOf('```mermaid');
  if (lastOpen === -1) return text;
  const afterOpen = text.slice(lastOpen + '```mermaid'.length);
  // 检查是否有闭合的 ```
  const closeIdx = afterOpen.indexOf('\n```');
  if (closeIdx !== -1) return text; // 已闭合，正常渲染
  // 未闭合：截掉 mermaid 块内容，显示占位文字
  return text.slice(0, lastOpen) + '\n\n> 流程图生成中...\n\n';
}

function hideIncompleteChart(text: string): string {
  // 流式输出时，隐藏未闭合的 <chart> 标签以避免显示原始语法
  const lastOpen = text.lastIndexOf('<chart');
  if (lastOpen === -1) return text;
  if (text.indexOf('</chart>', lastOpen) !== -1) return text; // 已闭合，正常渲染
  return text.slice(0, lastOpen); // 未闭合：截掉从 <chart 起的部分
}

function hideIncompleteFile(text: string): string {
  // 流式输出时，隐藏未闭合的 <file> 标签以避免显示原始语法（<file .../> 自闭合）
  const lastOpen = text.lastIndexOf('<file');
  if (lastOpen === -1) return text;
  if (text.indexOf('/>', lastOpen) !== -1) return text; // 已闭合，正常渲染
  return text.slice(0, lastOpen); // 未闭合：截掉从 <file 起的部分
}
import { useAuthStore } from '../stores/authStore';
import { useChatStore, collectArtifacts } from '../stores/chatStore';
import ChatInput from './ChatInput';
import { cn } from '@/lib/utils';
import { copyText } from '@/lib/clipboard';
import type { Message } from '../types';

// ── Agent 工具调用段解析 ──
// chatStore 在 agent 流式时会插入 <tool-call name="x">args</tool-call> /
// <tool-result name="x">result</tool-result> 标记；LLM 输出图表时会在文本里带
// <chart ...>data</chart> 标记。这里统一解析成结构化段落。
type MsgSegment =
  | { kind: 'text'; content: string }
  | { kind: 'tool'; name: string; args?: string; result?: string }
  | { kind: 'chart'; attrs: Record<string, string>; data: string }
  | { kind: 'file'; url: string; name: string };

// 图表段落的最小列宽（px）。连续图表排成响应式网格：宽屏最多 2~3 列，每列不足这个宽度
// 就退回单列——宁可单列大图，也不把图压小看不清。
const CHART_MIN_COL_PX = 400;

// 同一次回答里「同一类图」的硬上限，对齐系统提示词的约束 9。模型不听时这里兜底：
// 同族第 3 张起不再渲染，末尾用小字提示省略了几张（数据仍留在 msg.content 里）。
const MAX_CHARTS_PER_FAMILY = 2;
// 「同类」按视觉同族算：column/bar 都是柱条、pie/donut 都是饼、line/area 都是线；
// 其余图型各自成一族（不合并，避免把不同图型误当同类）。
const CHART_FAMILY: Record<string, string> = {
  column: 'bar',
  bar: 'bar',
  pie: 'pie',
  donut: 'pie',
  line: 'line',
  area: 'line',
};

function chartFamily(type: string): string {
  return CHART_FAMILY[type] || type || 'unknown';
}

/** 图表内容指纹：type + 按键排序的属性 + data 原文（纯字符串比较，不引 crypto）。
 *  用于"同一张图只渲染一次"——模型常在过程叙述里出一遍、末尾总结里再出一遍。 */
function chartFingerprint(seg: ChartSegment): string {
  const attrs = Object.keys(seg.attrs)
    .sort()
    .map((k) => `${k}=${seg.attrs[k]}`)
    .join('&');
  return `${seg.attrs.type || ''}|${attrs}|${seg.data}`;
}

/** 已保留图表的去重状态（指纹集合 + 各族已用张数），可作下一次 cap 的起点。 */
type ChartCapState = { fps: Set<string>; families: Map<string, number> };

/** 图表限流 + 去重，返回保留段落、被丢张数与累计状态：
 *  ① 同一张图（指纹相同）**只留第一张**——幂等，让"重复出图"无害（`<file>` 卡早已按 url 去重，图缺的就是这层）；
 *  ② 同族最多 MAX_CHARTS_PER_FAMILY 张。
 *  `seed` 传入先前已保留的状态（先 cap 最终答案、再 cap 过程段），保证重复的图留在正文而不是折叠区。 */
function capChartsByFamily(
  segments: MsgSegment[],
  seed?: ChartCapState,
): { list: MsgSegment[]; dropped: number; state: ChartCapState } {
  const seen = new Map(seed?.families ?? []);
  const seenFp = new Set(seed?.fps ?? []);
  const list: MsgSegment[] = [];
  let dropped = 0;
  for (const seg of segments) {
    if (seg.kind !== 'chart') {
      list.push(seg);
      continue;
    }
    const fp = chartFingerprint(seg);
    if (seenFp.has(fp)) {
      dropped += 1;
      continue;
    }
    seenFp.add(fp);
    const family = chartFamily(String(seg.attrs.type || ''));
    const n = (seen.get(family) ?? 0) + 1;
    seen.set(family, n);
    if (n > MAX_CHARTS_PER_FAMILY) {
      dropped += 1;
      continue;
    }
    list.push(seg);
  }
  return { list, dropped, state: { fps: seenFp, families: seen } };
}

/** 按「最后一个工具段」切分：之前（含）是过程旁白 + 工具调用，之后是最终答案。
 *  依据：模型不可能在 on_tool_start 与 on_tool_end 之间输出文字，所以最终答案一定在最后一条工具结果之后。
 *  没有工具段 → 全部算答案，不出现折叠头。 */
function splitProcessAnswer(segments: MsgSegment[]): {
  processSegs: MsgSegment[];
  answerSegs: MsgSegment[];
} {
  let lastTool = -1;
  segments.forEach((seg, i) => {
    if (seg.kind === 'tool') lastTool = i;
  });
  if (lastTool === -1) return { processSegs: [], answerSegs: segments };
  return {
    processSegs: segments.slice(0, lastTool + 1),
    answerSegs: segments.slice(lastTool + 1),
  };
}

type NonChartSegment = Exclude<MsgSegment, { kind: 'chart' }>;
type ChartSegment = Extract<MsgSegment, { kind: 'chart' }>;

/** 段落编排：把连续 chart 段合成一组（宽屏并排），其余段落各自独占一行。 */
type MsgBlock =
  | { kind: 'chart-group'; items: { seg: ChartSegment; key: number }[] }
  | { kind: 'seg'; seg: NonChartSegment; key: number };

function groupChartSegments(segments: MsgSegment[]): MsgBlock[] {
  const blocks: MsgBlock[] = [];
  segments.forEach((seg, i) => {
    if (seg.kind !== 'chart') {
      blocks.push({ kind: 'seg', seg, key: i });
      return;
    }
    const last = blocks[blocks.length - 1];
    if (last?.kind === 'chart-group') last.items.push({ seg, key: i });
    else blocks.push({ kind: 'chart-group', items: [{ seg, key: i }] });
  });
  return blocks;
}

const TOOL_TAG_RE = /<tool-(call|result) name="([^"]*)">([\s\S]*?)<\/tool-\1>/g;
// `<chart …/>`（**当前形态**：只写属性，数据由服务端注入）与 `<chart …>JSON</chart>`（历史遗留）都认。
// 捕获组仍是 2 个（属性 / 标签体），否则下面按组号取值的逻辑会错位。
const CHART_TAG_RE = /<chart\b([^>]*?)(?:\/>|>([\s\S]*?)<\/chart>)/g;
const FILE_TAG_RE = /<file\b([^>]*)\/>/g;

// 产物 URL（Markdown 链接 / 裸 URL）识别——LLM 偶尔跑偏不输出 <file> 标签而写成链接，
// 这里兜底拆成 file 段，避免纯文字看不出是可下载文件。URL 归一化由 FileCard 内部完成。
const ARTIFACT_PATTERN = new RegExp(
  `\\[([^\\]]*)\\]\\(((?:https?://[^/]*)?/api/v1/chat/agent/artifacts/[^)\\s]*)\\)` +
  `|((?:https?://[^\\s/]+)?/api/v1/chat/agent/artifacts/[^\\s)\\]]+)`,
  'g',
);

// 裸 URL 时从路径尾部取文件名（剥离签名/查询串）
function artifactNameOf(url: string): string {
  const tail = url.split('?')[0].split('#')[0].split('/').pop();
  return tail || '文件';
}

function splitArtifactRefs(text: string): MsgSegment[] {
  const out: MsgSegment[] = [];
  let last = 0;
  let m: RegExpExecArray | null;
  ARTIFACT_PATTERN.lastIndex = 0;
  while ((m = ARTIFACT_PATTERN.exec(text)) !== null) {
    if (m.index > last) {
      const t = text.slice(last, m.index);
      if (t.trim()) out.push({ kind: 'text', content: t });
    }
    const url = m[2] || m[3];
    const name = m[1] || artifactNameOf(url);
    out.push({ kind: 'file', url, name });
    last = ARTIFACT_PATTERN.lastIndex;
  }
  if (last < text.length) {
    const t = text.slice(last);
    if (t.trim()) out.push({ kind: 'text', content: t });
  }
  return out.length ? out : [{ kind: 'text', content: text }];
}

function parseAttrs(raw: string): Record<string, string> {
  const attrs: Record<string, string> = {};
  const re = /([\w-]+)="([^"]*)"/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(raw)) !== null) {
    attrs[m[1]] = m[2];
  }
  return attrs;
}

/** 图表数据 → ChartRenderer 要的 JSON 字符串；取不到返回空串（渲染器走空态，不报错）。 */
function jsonOfChart(value: unknown): string {
  return value === undefined || value === null ? '' : JSON.stringify(value);
}

/** 已废弃的旧标记（计划卡片/澄清问卷时代的产物）：浏览器里尚未刷新完的旧流可能残留，
 * 统一剥掉，避免把内部 JSON 以裸文本显示给用户。 */
const LEGACY_BLOCK_RE = /<(plan|interview)\b[^>]*>[\s\S]*?<\/\1>/g;

function parseMessageSegments(raw: string): MsgSegment[] {
  const text = raw.replace(LEGACY_BLOCK_RE, '');
  if (
    !text.includes('<tool-') && !text.includes('<chart') && !text.includes('<file')
  ) {
    // 无结构化标签也可能带产物链接/裸 URL，仍走拆分
    return splitArtifactRefs(text);
  }
  const segments: MsgSegment[] = [];
  const re = new RegExp(
    `(?:${TOOL_TAG_RE.source})|(?:${CHART_TAG_RE.source})|(?:${FILE_TAG_RE.source})`,
    'g',
  );
  let lastIndex = 0;
  let m: RegExpExecArray | null;
  while ((m = re.exec(text)) !== null) {
    if (m.index > lastIndex) {
      const t = text.slice(lastIndex, m.index);
      if (t.trim()) segments.push(...splitArtifactRefs(t));
    }
    // 组：tool 1-3，chart 4-5，file 6（未参与匹配的组为 undefined）
    const [, tagType, name, toolBody, chartAttrs, chartBody, fileAttrs] = m;
    if (tagType) {
      if (tagType === 'call') {
        segments.push({ kind: 'tool', name, args: toolBody });
      } else {
        // result 挂到最近一个同名且还没有 result 的工具段上
        const target = [...segments].reverse().find(
          (s): s is Extract<MsgSegment, { kind: 'tool' }> =>
            s.kind === 'tool' && s.name === name && s.result === undefined,
        );
        if (target) target.result = toolBody;
        else segments.push({ kind: 'tool', name, result: toolBody });
      }
    } else if (chartAttrs !== undefined) {
      segments.push({ kind: 'chart', attrs: parseAttrs(chartAttrs || ''), data: chartBody || '' });
    } else if (fileAttrs !== undefined) {
      const attrs = parseAttrs(fileAttrs || '');
      segments.push({ kind: 'file', url: attrs.url || '', name: attrs.name || '' });
    }
    lastIndex = re.lastIndex;
  }
  if (lastIndex < text.length) {
    const t = text.slice(lastIndex);
    if (t.trim()) segments.push(...splitArtifactRefs(t));
  }
  return segments;
}

function toolIcon(name: string) {
  // 按工具类别取图标，一眼看出这一步在做什么（对齐 DeerFlow 的 kind → icon 映射）
  if (name === 'web__search') return Search;
  if (name === 'web__fetch') return Globe;
  if (name.startsWith('analysis_')) return SquareTerminal;
  if (name.startsWith('skill__')) return NotebookPen;
  if (name === 'rag_knowledge_search' || name.startsWith('history_')) return BookOpenText;
  return Wrench;
}

/** 时间线的一步（对齐 DeerFlow ChainOfThoughtStep）。
 *  节点只有一个：有工具图标就用工具图标，没有（旁白/图表/文件）就用 DeerFlow 的默认圆点 Dot。
 *  竖线在节点正下方，只画本步高度内的一段，不跨步相连。 */
function TimelineStep({
  icon: Icon,
  label,
  children,
  isLast = false,
  active = false,
}: {
  icon?: typeof Wrench;
  label: ReactNode;
  children?: ReactNode;
  isLast?: boolean;
  active?: boolean;
}) {
  return (
    <div
      className={cn(
        'flex gap-2 text-sm',
        active ? 'text-foreground' : 'text-muted-foreground',
      )}
    >
      <div className="relative mt-0.5 shrink-0">
        {Icon ? <Icon className="size-4" /> : <Dot className="size-4" />}
        {!isLast && (
          <div
            aria-hidden="true"
            className="pointer-events-none absolute left-1/2 top-5 bottom-0 -mx-px w-px bg-muted-foreground/40"
          />
        )}
      </div>
      <div className="min-w-0 flex-1 space-y-2 overflow-hidden">
        <div>{label}</div>
        {children}
      </div>
    </div>
  );
}

/** 解析成 JSON 对象（数组/标量/解析失败返回 null），语言判定要用同级的 `path` 字段。 */
function parseJsonObject(raw?: string): Record<string, unknown> | null {
  if (!raw) return null;
  try {
    const v = JSON.parse(raw);
    if (v && typeof v === 'object' && !Array.isArray(v)) return v as Record<string, unknown>;
  } catch {
    /* 非 JSON → 原样展示 */
  }
  return null;
}

/** 把工具参数/返回拆成可逐字段复制的行；只处理 JSON 对象，数组/标量返回 null 走整块展示。 */
function parseFields(raw?: string): { key: string; value: string }[] | null {
  const obj = parseJsonObject(raw);
  if (!obj) return null;
  const rows = Object.entries(obj).map(([k, val]) => ({
    key: k,
    value: typeof val === 'string' ? val : JSON.stringify(val, null, 2),
  }));
  return rows.length ? rows : null;
}

/** 写文件类工具里这些字段是文件内容，语言按同级 `path` 的扩展名判定。 */
const EXT_LANG: Record<string, string> = {
  py: 'python', js: 'javascript', jsx: 'javascript', ts: 'typescript', tsx: 'typescript',
  json: 'json', html: 'html', htm: 'html', xml: 'xml', css: 'css', sql: 'sql',
  md: 'markdown', sh: 'bash', yml: 'yaml', yaml: 'yaml',
};
const FILE_CONTENT_TOOLS = new Set(['analysis_write_file', 'analysis_str_replace']);
const FILE_CONTENT_KEYS = new Set(['content', 'old', 'new']);

/** 工具参数里某个字段的代码语言；返回 null 表示按纯文本展示。 */
function payloadLanguage(
  toolName: string | undefined,
  key: string,
  obj: Record<string, unknown> | null,
): string | null {
  if (toolName === 'analysis_exec' && key === 'python_code') return 'python';
  if (!toolName || !FILE_CONTENT_TOOLS.has(toolName) || !FILE_CONTENT_KEYS.has(key)) return null;
  const path = obj?.path;
  if (typeof path !== 'string') return null;
  const ext = path.split('.').pop()?.toLowerCase() ?? '';
  return EXT_LANG[ext] ?? null;
}

/** 单段文字的复制按钮（复制后 1.5 秒回弹）。 */
function FieldCopy({ text }: { text: string }) {
  const [ok, setOk] = useState(false);
  return (
    <button
      type="button"
      title={ok ? '已复制' : '复制'}
      onClick={async () => {
        const r = await copyText(text);
        setOk(r);
        setTimeout(() => setOk(false), 1500);
      }}
      className="shrink-0 rounded p-0.5 text-muted-foreground transition-colors hover:bg-muted/60 hover:text-foreground"
    >
      {ok ? <Check className="size-3" /> : <Copy className="size-3" />}
    </button>
  );
}

/** 工具参数/返回的一段：可拆字段时逐字段列表，否则整块；空值给「（空）」标记。 */
function ToolPayload({
  title,
  raw,
  toolName,
}: {
  title: string;
  raw: string;
  toolName?: string;
}) {
  const obj = parseJsonObject(raw);
  const fields = parseFields(raw);
  const empty = raw.trim() === '' || raw.trim() === '{}';
  return (
    <section className="min-w-0 space-y-1">
      <div className="flex items-center justify-between gap-2">
        <p className="text-muted-foreground">{title}</p>
        <FieldCopy text={raw} />
      </div>
      {empty ? (
        <p className="rounded bg-[var(--bubble)] px-2 py-1.5 text-muted-foreground">（空）</p>
      ) : fields ? (
        <div className="space-y-1.5">
          {fields.map((f) => {
            const language = payloadLanguage(toolName, f.key, obj);
            return (
              <div key={f.key} className="min-w-0 space-y-0.5">
                <div className="flex items-center justify-between gap-2">
                  <span className="min-w-0 truncate font-mono text-[11px] text-muted-foreground">
                    {f.key}
                  </span>
                  <FieldCopy text={f.value} />
                </div>
                <pre className="max-h-64 overflow-auto whitespace-pre-wrap break-all rounded bg-[var(--bubble)] p-2 font-mono">
                  {language ? <CodeHighlight code={f.value} language={language} /> : f.value}
                </pre>
              </div>
            );
          })}
        </div>
      ) : (
        <pre className="max-h-64 overflow-auto whitespace-pre-wrap break-all rounded bg-[var(--bubble)] p-2 font-mono">
          {raw}
        </pre>
      )}
    </section>
  );
}

/** 工具步骤：标题一行，参数/返回收在「详情」里（对齐 DeerFlow ToolCallDetails）。 */
function ToolStep({
  name,
  args,
  result,
  isLast,
}: {
  name: string;
  args?: string;
  result?: string;
  isLast: boolean;
}) {
  const [open, setOpen] = useState(false);
  const status = useChatStore((s) => s.toolStatus?.[name]);
  const done = result !== undefined;
  return (
    <TimelineStep
      icon={toolIcon(name)}
      isLast={isLast}
      active={!done}
      label={(
        // 工具框：沿用改造前的气泡式小卡片（描边 + --panel 底），点标题展开参数/返回
        <div className="overflow-hidden rounded-md border border-border bg-[var(--panel)] text-xs">
          <button
            type="button"
            onClick={() => setOpen((v) => !v)}
            aria-expanded={open}
            className="flex w-full items-center gap-2 px-2.5 py-1.5 text-left transition-colors hover:bg-muted/50"
          >
            <span className="min-w-0 flex-1 truncate font-mono font-medium">{name}</span>
            <span className="shrink-0 text-muted-foreground">
              {done ? '调用完成' : (status || '调用中…')}
            </span>
            <ChevronDown
              className={cn(
                'size-3.5 shrink-0 text-muted-foreground transition-transform',
                open && 'rotate-180',
              )}
            />
          </button>
          {open && (
            <div className="space-y-2.5 border-t border-border px-2.5 py-2">
              {args !== undefined && <ToolPayload title="参数" raw={args} toolName={name} />}
              {result !== undefined && <ToolPayload title="返回" raw={result} />}
            </div>
          )}
        </div>
      )}
    />
  );
}

/** 答案段渲染：图表网格 / 文件卡 / Markdown 正文（工具段按切分规则不会出现在这里）。 */
function MessageBlocks({
  blocks,
  chartData,
}: {
  blocks: MsgBlock[];
  chartData?: Record<string, unknown>;
}) {
  return (
    <>
      {blocks.map((block) => {
        if (block.kind === 'chart-group') {
          // 连续图表排成响应式网格：auto-fit 按容器实宽定列数（宽屏最多 2~3 列，每列不足
          // CHART_MIN_COL_PX 就退回单列）。不用 sm:/2xl: 视口断点——气泡宽度随右侧面板
          // 开合变化，断点会判错。
          return (
            <div
              key={block.items[0].key}
              className="my-2 grid gap-3"
              style={{
                gridTemplateColumns:
                  `repeat(auto-fit,minmax(min(${CHART_MIN_COL_PX}px,100%),1fr))`,
              }}
            >
              {block.items.map(({ seg, key }) => (
                <ChartRenderer
                  key={key}
                  spec={{
                    type: seg.attrs.type || '',
                    attrs: seg.attrs,
                    // 数据**不从正文取**（模型不再写 JSON —— 正文写会被引号截断、图直接崩）：
                    // 优先用历史遗留的标签体，否则按 `group` 从服务端注入的数据里查
                    data: seg.data || jsonOfChart(chartData?.[seg.attrs.group || '']),
                  }}
                />
              ))}
            </div>
          );
        }
        const { seg, key: i } = block;
        if (seg.kind === 'file') {
          return <FileCard key={i} url={seg.url} name={seg.name} />;
        }
        if (seg.kind === 'text') {
          return <MarkdownContent key={i} content={seg.content} />;
        }
        // 工具段按切分规则只出现在过程段，答案段不会走到这里
        return null;
      })}
    </>
  );
}

/** 处理过程：旁白 + 工具调用收进可折叠时间线，正文只留最终答案（对齐 DeerFlow）。
 *  用已装的 @base-ui/react Collapsible：Trigger 自带 aria-expanded / 键盘交互，
 *  Panel 暴露 --collapsible-panel-height，配合 CSS 过渡做展开/收起的高度动画。 */
function ProcessChain({
  segments,
  chartData,
  open,
  onOpenChange,
  streaming,
}: {
  segments: MsgSegment[];
  chartData?: Record<string, unknown>;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  streaming: boolean;
}) {
  return (
    <Collapsible.Root
      open={open}
      onOpenChange={(next) => onOpenChange(next)}
      className="not-prose w-full rounded-lg border border-border/70 p-0.5"
    >
      <Collapsible.Trigger className="flex w-full items-center gap-2 rounded-md px-1 py-1 text-sm text-foreground/70 transition-colors hover:bg-muted/50 hover:text-foreground">
        <ChevronUp
          className={cn(
            'size-4 shrink-0 transition-transform duration-200',
            open && 'rotate-180',
          )}
        />
        {streaming && <Loader2 className="size-4 shrink-0 animate-spin" />}
        <span className="flex-1 text-left">
          {open ? '隐藏步骤' : `查看其他 ${segments.length} 个步骤`}
        </span>
      </Collapsible.Trigger>
      <Collapsible.Panel
        keepMounted
        className="h-[var(--collapsible-panel-height)] overflow-hidden transition-[height] duration-200 ease-out data-[starting-style]:h-0 data-[ending-style]:h-0"
      >
        <div className="space-y-3 px-4 pt-1 pb-2">
          {segments.map((seg, i) => {
            const isLast = i === segments.length - 1;
            if (seg.kind === 'tool') {
              return (
                <ToolStep
                  key={i}
                  name={seg.name}
                  args={seg.args}
                  result={seg.result}
                  isLast={isLast}
                />
              );
            }
            if (seg.kind === 'file') {
              return (
                <TimelineStep key={i} isLast={isLast} label={<FileCard url={seg.url} name={seg.name} />} />
              );
            }
            if (seg.kind === 'chart') {
              return (
                <TimelineStep
                  key={i}
                  isLast={isLast}
                  label={(
                    <ChartRenderer
                      spec={{
                        type: seg.attrs.type || '',
                        attrs: seg.attrs,
                        data: seg.data || jsonOfChart(chartData?.[seg.attrs.group || '']),
                      }}
                    />
                  )}
                />
              );
            }
            return (
              <TimelineStep
                key={i}
                isLast={isLast}
                label={(
                  // 套上 assistant-msg 只为取标题/列表排版（这里没有 pre，不会撞工具详情的样式）
                  <div className="assistant-msg max-w-none text-muted-foreground">
                    <MarkdownContent content={seg.content} />
                  </div>
                )}
              />
            );
          })}
        </div>
      </Collapsible.Panel>
    </Collapsible.Root>
  );
}


const SUGGESTIONS = [
  '介绍一下平台和智能体有哪些能力',
  '查一下我的待办工单',
  '查询设备台账或人员信息',
  '分析数据并生成图表',
  '帮我创建一个自定义技能',
];

function formatDateTime(iso?: string): string {
  if (!iso) return '';
  try {
    return new Date(iso).toLocaleString('zh-CN', { dateStyle: 'short', timeStyle: 'short' });
  } catch {
    return '';
  }
}

/** 超长标题：无省略号，悬停时向后滚动（跑马灯）显示全文 */
function MarqueeTitle({ text }: { text: string }) {
  const ref = useRef<HTMLDivElement>(null);
  const [overflow, setOverflow] = useState(false);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    setOverflow(el.scrollWidth > el.clientWidth + 1);
  }, [text]);
  return (
    <div
      ref={ref}
      className="rz-marquee min-w-0 text-sm font-medium leading-tight"
      data-overflow={overflow ? 'true' : undefined}
    >
      <span className="rz-marquee__track">
        {overflow && <span className="pr-2" aria-hidden>{text}</span>}
        <span className="pr-2">{text}</span>
      </span>
    </div>
  );
}

export const MessageBubble = memo(function MessageBubble({
  msg,
  isStreaming,
  isLast,
  userName,
  chartData,
}: {
  msg: Message;
  isStreaming: boolean;
  isLast: boolean;
  userName: string;
  /** 本会话的图表数据 `{组名: rows}`：正文只写 `<chart group="…"/>`，数据按组名从这里取 */
  chartData?: Record<string, unknown>;
}) {
  const confirm = useConfirm();
  const segments = useMemo(() => {
    const raw = msg.content || '';
    const parsed = parseMessageSegments(raw);
    const mapped = parsed.map((seg) => {
      if (seg.kind !== 'text') return seg;
      const fixed = isStreaming && isLast
        ? fixMermaidBlocks(hideIncompleteFile(hideIncompleteChart(hideIncompleteMermaid(seg.content))))
        : fixMermaidBlocks(seg.content);
      return { ...seg, content: fixed };
    });
    // 同一消息内重复的 <file>（模型偶发重复输出）按 url 去重，只保留第一张卡
    const seen = new Set<string>();
    return mapped.filter((seg) => {
      if (seg.kind !== 'file') return true;
      const key = (seg.url || '').replace(/\/+$/, '');
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    });
  }, [msg.content, isStreaming, isLast]);
  // 按最后一个工具段切分：之前（含）是过程旁白 + 工具调用，之后是最终答案
  const { processSegs, answerSegs } = useMemo(() => splitProcessAnswer(segments), [segments]);
  // 图表限流/去重**先 cap 答案、再以答案的状态为 seed cap 过程**：同一张图在旁白与总结里
  // 各出一次时，保留正文那张，避免重复图被折叠区抢走
  const answerCapped = useMemo(() => capChartsByFamily(answerSegs), [answerSegs]);
  const processCapped = useMemo(
    () => capChartsByFamily(processSegs, answerCapped.state),
    [processSegs, answerCapped],
  );
  const droppedCharts = answerCapped.dropped + processCapped.dropped;
  const answerBlocks = useMemo(
    () => groupChartSegments(answerCapped.list),
    [answerCapped.list],
  );
  const isAssistant = msg.role === 'assistant';
  // 空气泡：流式且最后一条且尚无任何内容时，显示思考指示器替代空泡
  const showThinking = isAssistant && isStreaming && isLast && !(msg.content || '').trim();
  const deleteMessage = useChatStore((s) => s.deleteMessage);
  const retryFailed = useChatStore((s) => s.retryFailed);
  const [copied, setCopied] = useState(false);
  const [copyFailed, setCopyFailed] = useState(false);

  // 处理过程折叠：流式中展开，结束后约 1 秒自动收起；用户手动点过就不再强制收
  const [processOpen, setProcessOpen] = useState(false);
  const userToggled = useRef(false);
  const wasStreaming = useRef(false);
  const collapseTimer = useRef<number | null>(null);
  const streamingNow = isAssistant && isStreaming && isLast;
  useEffect(() => {
    if (streamingNow) {
      if (!wasStreaming.current) userToggled.current = false;
      wasStreaming.current = true;
      setProcessOpen(true);
    } else if (wasStreaming.current) {
      wasStreaming.current = false;
      if (userToggled.current) return;
      collapseTimer.current = window.setTimeout(() => setProcessOpen(false), 1000);
      return () => {
        if (collapseTimer.current !== null) {
          window.clearTimeout(collapseTimer.current);
          collapseTimer.current = null;
        }
      };
    }
  }, [streamingNow]);

  const handleProcessOpenChange = (next: boolean) => {
    userToggled.current = true;
    if (collapseTimer.current !== null) {
      window.clearTimeout(collapseTimer.current);
      collapseTimer.current = null;
    }
    setProcessOpen(next);
  };

  const handleCopy = async () => {
    // 只复制正文：msg.content 里混着 <tool-call>/<tool-result>/<chart>/<file>/<plan> 等内部标记，
    // 整段拷会把工具参数与返回（一大坨 JSON）一起塞给用户。优先只取最终答案段的文本，
    // 答案为空（例如整轮只调工具、没写最终文本）时回退到全部文本段。
    const textOf = (segs: MsgSegment[]) =>
      segs.filter((seg) => seg.kind === 'text').map((seg) => seg.content.trim())
        .filter(Boolean).join('\n\n');
    const text = textOf(answerSegs) || textOf(segments);
    if (!text) {
      // 整条回答只有工具卡片/图表、没有正文 → 没东西可拷，别谎报"已复制"
      setCopyFailed(true);
      setTimeout(() => setCopyFailed(false), 1500);
      return;
    }
    // 走共用工具：HTTP 入口下 navigator.clipboard 不存在，copyText 会降级到 execCommand；
    // 失败要给反馈（原来是空 catch 静默吞掉，用户只看到"点了没用"）
    const ok = await copyText(text);
    setCopied(ok);
    setCopyFailed(!ok);
    setTimeout(() => {
      setCopied(false);
      setCopyFailed(false);
    }, 1500);
  };

  const handleDelete = async () => {
    const ok = await confirm({
      title: '删除这条回答？',
      confirmText: '删除',
      variant: 'destructive',
    });
    if (ok) await deleteMessage(msg.session_id, msg.message_id);
  };

  return (
    <div
      data-mid={msg.message_id}
      className={cn('group flex gap-3', !isAssistant && 'flex-row-reverse')}
    >
      <div
        className={cn(
          'flex size-8 shrink-0 items-center justify-center rounded-lg',
          !isAssistant ? 'bg-secondary text-secondary-foreground' : 'bg-primary/20 text-primary',
        )}
      >
        {!isAssistant ? <User className="size-4" /> : <Bot className="size-4" />}
      </div>
      <div className={cn('min-w-0 max-w-[80%] break-words', !isAssistant && 'flex flex-col items-end')}>
        <p className="mb-1 text-xs font-medium text-muted-foreground">
          {!isAssistant ? userName : 'AI 助手'}
        </p>
        {!isAssistant && msg.uploaded_filenames && msg.uploaded_filenames.length > 0 && (
          <div className="mb-1.5 flex flex-wrap justify-end gap-1.5">
            {msg.uploaded_filenames.map((name) => (
              <span
                key={name}
                className="flex items-center gap-1.5 rounded-lg border border-border bg-secondary/60 px-2 py-1 text-xs text-secondary-foreground"
                title={name}
              >
                <FileText className="size-3.5 shrink-0 text-primary" />
                <span className="max-w-40 truncate">{name}</span>
              </span>
            ))}
          </div>
        )}
        <div
          className={cn(
            'rounded-2xl px-4 py-2.5 text-sm leading-relaxed',
            // 气泡底色走 --bubble（浅米色），不用全局 --secondary（后者还被输入框胶囊/按钮/徽标复用）
            'bg-[var(--bubble)] text-secondary-foreground',
            !isAssistant ? 'rounded-tr-sm' : 'rounded-tl-sm',
          )}
        >
          {isAssistant ? (
            showThinking ? (
              <div className="flex items-center gap-1.5 py-1">
                <span className="size-1.5 animate-bounce rounded-full bg-muted-foreground/60" />
                <span className="size-1.5 animate-bounce rounded-full bg-muted-foreground/60 [animation-delay:150ms]" />
                <span className="size-1.5 animate-bounce rounded-full bg-muted-foreground/60 [animation-delay:300ms]" />
                <span className="ml-1 text-xs text-muted-foreground">正在思考…</span>
              </div>
            ) : (
              <>
                {/* 处理过程放在 .assistant-msg 之外：避免继承正文的 ol/li/pre 排版（时间线会散） */}
                {processCapped.list.length > 0 && (
                  <ProcessChain
                    segments={processCapped.list}
                    chartData={chartData}
                    open={processOpen}
                    onOpenChange={handleProcessOpenChange}
                    streaming={streamingNow}
                  />
                )}
                <div className={cn('assistant-msg max-w-none', processCapped.list.length > 0 && 'mt-2')}>
                  <MessageBlocks blocks={answerBlocks} chartData={chartData} />
                  {droppedCharts > 0 && (
                    // 同族图超过上限被隐去（约束 9 的前端兜底）；明说一句，免得用户以为内容丢了
                    <p className="mt-1.5 text-xs text-muted-foreground">
                      （另有 {droppedCharts} 张同类/重复图已省略）
                    </p>
                  )}
                  {isStreaming && isLast && !msg.streamError && (
                    <div className="mt-1.5 flex items-center gap-1.5 text-xs text-muted-foreground">
                      <Clock3 className="size-3.5 shrink-0 animate-pulse" />
                      <span>正在生成…</span>
                    </div>
                  )}
                  {msg.streamError && (
                    <div className="mt-2 flex flex-col items-start gap-2 rounded-lg border border-destructive/40 bg-destructive/5 px-3 py-2">
                      <p className="flex items-center gap-1.5 text-sm text-destructive">
                        <AlertCircle className="size-4 shrink-0" />
                        {msg.streamError}
                      </p>
                      <button
                        type="button"
                        onClick={() => void retryFailed()}
                        className="inline-flex items-center gap-1.5 rounded-md border border-border px-2 py-1 text-xs text-foreground transition-colors hover:bg-muted"
                      >
                        <RotateCcw className="size-3" />
                        重试
                      </button>
                    </div>
                  )}
                </div>
              </>
            )
          ) : (
            <p className="whitespace-pre-line">{msg.content}</p>
          )}
        </div>
        {isAssistant && (
          <div className="mt-1.5 flex items-center gap-1.5">
            {msg.created_at && (
              <span className="text-xs text-muted-foreground/70">{formatDateTime(msg.created_at)}</span>
            )}
            <button
              type="button"
              onClick={handleCopy}
              title={copyFailed ? '复制失败' : copied ? '已复制' : '复制'}
              className="rounded p-1 text-muted-foreground/70 transition-colors hover:bg-muted/60 hover:text-foreground"
            >
              {copied ? <Check className="size-3.5" /> : <Copy className="size-3.5" />}
            </button>
            {copyFailed && (
              <span className="text-xs text-destructive">复制失败</span>
            )}
            <button
              type="button"
              onClick={() => void handleDelete()}
              data-tip="删除"
              className="rounded p-1 text-muted-foreground/70 transition-colors hover:bg-muted/60 hover:text-destructive"
            >
              <Trash2 className="size-3.5" />
            </button>
          </div>
        )}
      </div>
    </div>
  );
});

export default function ChatArea() {
  const confirm = useConfirm();
  const {
    messages, isStreaming, isLoadingMessages, sessions, currentSessionId,
    selectSession, deleteSession, pinSession, renameSession, sessionPanelOpen, closeSessionPanel,
    sessionTodos, sessionsTotal, loadingMoreSessions, loadMoreSessions,
    sessionChartData,
  } = useChatStore();
  const todos = (currentSessionId && sessionTodos[currentSessionId]) || [];
  const user = useAuthStore((s) => s.user);
  const pushArtifact = useChatStore((s) => s.pushArtifact);
  const pendingJump = useChatStore((s) => s.pendingJump);
  const clearPendingJump = useChatStore((s) => s.clearPendingJump);
  const listRef = useRef<HTMLDivElement>(null);
  // 是否贴着底部：用户上翻阅读时不跟随流式输出，只在回到底部后恢复自动滚动
  const atBottomRef = useRef(true);
  const [showJumpBottom, setShowJumpBottom] = useState(false);
  const [sessionSearch, setSessionSearch] = useState('');
  // 会话项省略号菜单 + 行内重命名状态。菜单用 fixed 定位向右弹（避开侧栏 overflow 裁剪），
  // 记录触发按钮的位置；省略号选中时显示气泡高亮。
  const [menu, setMenu] = useState<{ id: string; top: number; left: number } | null>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const [renamingId, setRenamingId] = useState<string | null>(null);
  const [renameValue, setRenameValue] = useState('');

  // 点菜单外关闭（省略号本身除外，toggle 交给其 click 处理）
  useEffect(() => {
    if (!menu) return;
    const onDocClick = (e: MouseEvent) => {
      const target = e.target as HTMLElement;
      if (target?.closest?.('[data-menu-trigger]')) return;
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) {
        setMenu(null);
      }
    };
    document.addEventListener('mousedown', onDocClick);
    return () => document.removeEventListener('mousedown', onDocClick);
  }, [menu]);

  const saveRename = (id: string) => {
    const title = renameValue.trim();
    if (title) renameSession(id, title);
    setRenamingId(null);
    setMenu(null);
  };

  // 流式/历史消息里的 HTML 产物自动进右侧面板：只推 store 里还没有的（新产物）。
  // 不能全量重推——pushArtifact 现在对已存在产物也会打开面板，历史会话预填后若重推
  // 会强制弹开面板；只推新的才能在流式生成新看板时自动打开、又不打扰用户手动开关。
  useEffect(() => {
    // 会话切换加载中：messages 可能仍是旧会话内容，跳过避免把旧会话产物错推进来
    if (isLoadingMessages) return;
    const known = new Set(useChatStore.getState().artifacts.map((a) => a.url));
    collectArtifacts(messages, currentSessionId)
      .filter((a) => !known.has(a.url))
      .forEach((a) => pushArtifact(a.url, a.name, a.type === 'records'));
  }, [messages, pushArtifact, currentSessionId, isLoadingMessages]);

  const scrollToBottom = useCallback(() => {
    if (listRef.current) {
      listRef.current.scrollTop = listRef.current.scrollHeight;
    }
    atBottomRef.current = true;
    setShowJumpBottom(false);
  }, []);

  // 只有贴着底部时才跟随新内容；用户上翻后流式输出不再把视图拽回底部。
  // 例外：用户刚发出提问（最后一条是 user）时强制回到底部，否则看不到自己的消息。
  useEffect(() => {
    const last = messages[messages.length - 1];
    if (atBottomRef.current || last?.role === 'user') scrollToBottom();
  }, [messages, scrollToBottom]);

  // 切会话：新会话一律从底部开始跟随
  useEffect(() => {
    atBottomRef.current = true;
    setShowJumpBottom(false);
  }, [currentSessionId]);

  const handleListScroll = () => {
    const el = listRef.current;
    if (!el) return;
    const near = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
    atBottomRef.current = near;
    setShowJumpBottom(!near);
  };

  // 铃铛通知跳转：切到目标会话后，滚动到通知对应的那条回答并短暂高亮；
  // 该回复已不可定位（重载后 id 变化/已删除）→ 回退到该会话最新一条助手回复，再兜底到底部。
  useEffect(() => {
    if (!pendingJump || pendingJump.sessionId !== currentSessionId) return;
    if (isLoadingMessages) return;
    const container = listRef.current;
    if (!container || messages.length === 0) return;
    const findEl = (mid?: string | null) => {
      if (!mid) return null;
      return (
        (Array.from(container.querySelectorAll('[data-mid]')).find(
          (el) => (el as HTMLElement).dataset.mid === mid,
        ) as HTMLElement | null) ?? null
      );
    };
    let target = findEl(pendingJump.messageId);
    if (!target) {
      const lastAsst = [...messages].reverse().find((m) => m.role === 'assistant');
      target = findEl(lastAsst?.message_id);
    }
    const anchor = target ?? (container.lastElementChild as HTMLElement | null);
    if (!anchor) return;
    anchor.scrollIntoView({ block: 'center', behavior: 'smooth' });
    clearPendingJump();
  }, [pendingJump, currentSessionId, isLoadingMessages, messages, clearPendingJump]);

  // 只藏「标题仍是占位名 且 一条消息都没有」的真·空壳（还没说话的占位会话）。
  // 判据必须用 message_count，**不能用标题**：标题可能长期停在「新对话」（改名要一次
  // 额外写入），按标题过滤会把有内容的会话一起藏掉、用户在侧栏再也找不到（2026-09 事故）。
  const displaySessions = sessions.filter(
    (s) =>
      s.title !== '新对话' ||
      s.message_count > 0 ||
      s.session_id === currentSessionId,
  );
  const filteredSessions = sessionSearch
    ? displaySessions.filter((s) => s.title.toLowerCase().includes(sessionSearch.toLowerCase()))
    : displaySessions;

  return (
    <div className="flex min-h-0 flex-1">
      {/* Backdrop */}
      {sessionPanelOpen && (
        <div
          className="fixed inset-0 z-40 bg-foreground/30"
          onClick={closeSessionPanel}
        />
      )}

      {/* Sessions sidebar — slide-out panel on all sizes */}
      <div
        className={cn(
          'flex flex-col overflow-hidden rounded-none border-0 lg:rounded-xl lg:border lg:border-border bg-card',
          'fixed inset-y-0 left-0 z-50 w-72 transition-transform',
          sessionPanelOpen ? 'translate-x-0 shadow-2xl' : '-translate-x-full',
        )}
      >
        <div className="border-b border-border px-3 py-3">
          <p className="text-sm font-semibold">历史会话</p>
        </div>
        <div className="border-b border-border px-3 py-2">
          <div className="relative">
            <Search className="pointer-events-none absolute left-2.5 top-1/2 size-3.5 -translate-y-1/2 text-muted-foreground" />
            <input
              placeholder="搜索会话"
              value={sessionSearch}
              onChange={(e) => setSessionSearch(e.target.value)}
              className="h-8 w-full rounded-md border border-border bg-secondary/60 pl-8 pr-2 text-sm outline-none focus:border-ring focus:bg-card"
            />
          </div>
        </div>
        <div
          className="flex-1 overflow-y-auto p-2"
          onScroll={(e) => {
            // 滚到接近底部就翻下一页（对齐 DeerFlow 的无限滚动）；搜索时不分页
            if (sessionSearch) return;
            const el = e.currentTarget;
            if (el.scrollHeight - el.scrollTop - el.clientHeight < 120) {
              void loadMoreSessions();
            }
          }}
        >
          {filteredSessions.length === 0 && (
            <p className="px-2 py-4 text-center text-xs text-muted-foreground">
              {sessionSearch ? '无匹配会话' : '暂无会话'}
            </p>
          )}
          {filteredSessions.map((s) => (
            <div key={s.session_id} className="group flex w-full items-center">
              <button
                onClick={() => { selectSession(s.session_id); closeSessionPanel(); }}
                className={cn(
                  'flex min-w-0 flex-1 items-center gap-2.5 rounded-lg px-2.5 py-2 text-left transition-colors',
                  s.session_id === currentSessionId
                    ? 'bg-accent text-accent-foreground'
                    : 'hover:bg-secondary',
                )}
              >
                {s.pinned
                  ? <Pin className="mt-0.5 size-3.5 shrink-0 text-primary" />
                  : <Sparkles className="mt-0.5 size-3.5 shrink-0 text-muted-foreground" />}
                <div className="min-w-0 flex-1">
                  {renamingId === s.session_id ? (
                    <Input
                      autoFocus
                      value={renameValue}
                      onChange={(e) => setRenameValue(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === 'Enter') saveRename(s.session_id);
                        if (e.key === 'Escape') { setRenamingId(null); setMenu(null); }
                      }}
                      onBlur={() => saveRename(s.session_id)}
                      onClick={(e) => e.stopPropagation()}
                      className="h-7 w-full text-sm"
                    />
                  ) : (
                    <MarqueeTitle text={s.title} />
                  )}
                  <p className="truncate text-xs text-muted-foreground">
                    {s.updated_at
                      ? new Date(s.updated_at).toLocaleDateString('zh-CN', { month: '2-digit', day: '2-digit' })
                      : ''}
                  </p>
                </div>
              </button>
              {/* 省略号菜单：置顶/重命名/删除（fixed 定位向右弹，避开侧栏 overflow 裁剪） */}
              <div className="mr-1 shrink-0">
                <button
                  data-menu-trigger
                  onClick={(e) => {
                    e.stopPropagation();
                    const rect = (e.currentTarget as HTMLButtonElement).getBoundingClientRect();
                    setMenu(menu?.id === s.session_id
                      ? null
                      : { id: s.session_id, top: rect.bottom + 4, left: rect.right + 4 });
                  }}
                  className={cn(
                    'rounded-full p-1 transition-colors',
                    menu?.id === s.session_id
                      ? 'bg-primary text-primary-foreground opacity-100 shadow-sm'
                      : 'text-muted-foreground opacity-0 group-hover:opacity-100 hover:bg-muted/60',
                  )}
                >
                  <MoreHorizontal className="size-3.5" />
                </button>
                {menu?.id === s.session_id && createPortal(
                  <div
                    ref={menuRef}
                    style={{ position: 'fixed', top: menu.top, left: menu.left, zIndex: 50 }}
                    className="min-w-32 overflow-hidden rounded-md border border-border bg-popover p-1 shadow-lg"
                  >
                    <button
                      onClick={(e) => { e.stopPropagation(); pinSession(s.session_id, !s.pinned); setMenu(null); }}
                      className="flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-xs hover:bg-muted/60"
                    >
                      {s.pinned ? <PinOff className="size-3" /> : <Pin className="size-3" />}
                      {s.pinned ? '取消置顶' : '置顶'}
                    </button>
                    <button
                      onClick={(e) => { e.stopPropagation(); setRenamingId(s.session_id); setRenameValue(s.title); setMenu(null); }}
                      className="flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-xs hover:bg-muted/60"
                    >
                      <Pencil className="size-3" />
                      重命名
                    </button>
                    <button
                      onClick={(e) => {
                        e.stopPropagation();
                        setMenu(null);
                        void confirm({
                          title: `删除会话「${s.title}」？`,
                          confirmText: '删除',
                          variant: 'destructive',
                        }).then((ok) => { if (ok) void deleteSession(s.session_id); });
                      }}
                      className="flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-xs text-destructive hover:bg-destructive/10"
                    >
                      <Trash2 className="size-3" />
                      删除
                    </button>
                  </div>,
                  document.body,
                )}
              </div>
            </div>
          ))}
          {/* 还有更早的会话没翻出来（后端 total > 已加载数）→ 给一行加载态 */}
          {!sessionSearch && sessions.length < sessionsTotal && (
            <p className="px-2 py-3 text-center text-xs text-muted-foreground">
              {loadingMoreSessions ? '加载中…' : '下滑加载更早的会话'}
            </p>
          )}
        </div>
      </div>

      {/* 聊天列（看板为 fixed 全屏覆盖层，不进卡片流，不占横向空间） */}
      <div className="flex min-h-0 flex-1 overflow-hidden rounded-none border-0 lg:rounded-xl lg:border lg:border-border bg-background">
        <div className="flex min-w-0 min-h-0 flex-1 flex-col">
          {/* Messages */}
          <div className="relative min-h-0 flex-1">
            <div
              ref={listRef}
              onScroll={handleListScroll}
              className="h-full space-y-5 overflow-y-auto p-4 [touch-action:pan-y]"
            >
              {isLoadingMessages ? (
                <div className="flex h-full items-center justify-center gap-2 text-sm text-muted-foreground">
                  <Loader2 className="size-4 animate-spin" />
                  加载会话…
                </div>
              ) : messages.length === 0 ? (
                <div className="flex h-full flex-col items-center justify-center gap-3 text-center">
                  <div className="flex size-14 items-center justify-center rounded-2xl bg-primary/10">
                    <Bot className="size-7 text-primary" />
                  </div>
                  <div>
                    <p className="text-sm font-semibold">智能运维助手</p>
                    <p className="mt-1 text-xs text-muted-foreground max-w-xs">
                      欢迎使用润泽 AI 平台，一个面向数据中心运维的超级智能体。可以帮您检索知识文档、分析业务数据，还能识别安全隐患、生成图表看板、制作报告与视频等。
                    </p>
                  </div>
                </div>
              ) : (
                messages.map((msg: Message, idx: number) => (
                  <MessageBubble
                    key={msg.message_id}
                    msg={msg}
                    isStreaming={isStreaming}
                    isLast={idx === messages.length - 1}
                    userName={user?.name || user?.phone || '我'}
                    chartData={currentSessionId ? sessionChartData[currentSessionId] : undefined}
                  />
                ))
              )}
            </div>
            {showJumpBottom && (
              <button
                type="button"
                aria-label="回到底部"
                title="回到底部"
                onClick={scrollToBottom}
                className="absolute bottom-4 right-4 z-10 flex size-8 items-center justify-center rounded-full border border-border bg-card text-muted-foreground shadow-md transition-colors hover:text-foreground"
              >
                <ChevronDown className="size-4" />
              </button>
            )}
          </div>

          {/* 计划模式任务清单：贴在输入框正上方（DeerFlow 同款位置），不占消息流 */}
          <TodoTray todos={todos} />

          {/* Input */}
          <ChatInput suggestions={SUGGESTIONS} />
        </div>
        <ArtifactPanel />
      </div>
    </div>
  );
}

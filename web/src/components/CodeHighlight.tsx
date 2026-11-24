import { useMemo, type ReactNode } from 'react';
import { common, createLowlight } from 'lowlight';
import { cn } from '@/lib/utils';

const lowlight = createLowlight(common);

type HastNode = {
  type: string;
  value?: string;
  tagName?: string;
  properties?: Record<string, unknown>;
  children?: HastNode[];
};

/** 只映射需要出现的两种节点：文本与带 `.hljs-*` 类名的 span。 */
function toReact(nodes: HastNode[], keyPrefix: string): ReactNode[] {
  return nodes.map((node, i) => {
    const key = `${keyPrefix}-${i}`;
    if (node.type === 'text') return node.value ?? '';
    if (node.type !== 'element') return null;
    const cls = node.properties?.className;
    return (
      <span key={key} className={Array.isArray(cls) ? cls.join(' ') : undefined}>
        {toReact(node.children ?? [], key)}
      </span>
    );
  });
}

/**
 * 非 Markdown 场景的语法高亮（工具详情里的分析代码）。
 *
 * 正文代码块走 `MarkdownContent` 的 `rehype-highlight`；那里拿不到"这段是哪种语言"的上下文，
 * 而工具参数（如 `analysis_exec.python_code`）需要按工具与字段自己判定语言，故单独用 lowlight。
 * token 色复用 `index.css` 里现有的 `.hljs-*`（三套主题），换行由调用方的 `<pre>` 负责。
 */
export function CodeHighlight({
  code,
  language,
  className,
}: {
  code: string;
  language: string;
  className?: string;
}) {
  const content = useMemo(() => {
    try {
      const nodes = lowlight.highlight(language, code).children as unknown as HastNode[];
      return toReact(nodes, 'hl');
    } catch {
      return code;
    }
  }, [code, language]);

  return <code className={cn('hljs font-mono', className)}>{content}</code>;
}

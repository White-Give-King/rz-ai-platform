import {
  createContext, useCallback, useContext, useEffect, useId, useRef, useState,
  type ReactNode,
} from 'react';
import { createPortal } from 'react-dom';
import { Button } from '@/components/ui/button';

export type ConfirmOptions = {
  title: string;
  description?: ReactNode;
  confirmText?: string;
  cancelText?: string;
  variant?: 'default' | 'destructive';
};

type ConfirmFn = (options: ConfirmOptions) => Promise<boolean>;
type Pending = { options: ConfirmOptions; resolve: (ok: boolean) => void };

/** 确认弹窗（受控）：portal 到 body —— 看板容器 `.rz-board-overlay` 是 fixed z-20 的
 *  层叠上下文，放在它内部即使 z-50 也会被顶栏（z-30）盖住。 */
export function ConfirmDialog({
  open, title, description, confirmText = '确定', cancelText = '取消',
  variant = 'default', onConfirm, onCancel,
}: ConfirmOptions & { open: boolean; onConfirm: () => void; onCancel: () => void }) {
  const titleId = useId();
  const descId = useId();
  const cancelRef = useRef<HTMLButtonElement | null>(null);
  const restoreRef = useRef<HTMLElement | null>(null);

  // 初始焦点落在「取消」：依赖只写 open，父级轮询重渲染时不抢焦点
  useEffect(() => {
    if (!open) return;
    const prev = document.activeElement;
    restoreRef.current = prev instanceof HTMLElement ? prev : null;
    const raf = requestAnimationFrame(() => cancelRef.current?.focus());
    return () => {
      cancelAnimationFrame(raf);
      const el = restoreRef.current;
      if (el && document.contains(el)) el.focus();
    };
  }, [open]);

  // Esc 关闭：捕获阶段拦下，避免被其它 window 级监听重复处理
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return;
      e.preventDefault();
      e.stopPropagation();
      onCancel();
    };
    document.addEventListener('keydown', onKey, true);
    return () => document.removeEventListener('keydown', onKey, true);
  }, [open, onCancel]);

  // 看板已锁滚动时只跟随、不争抢：prev 已是 hidden 就不在关闭时回写
  useEffect(() => {
    if (!open) return;
    const prev = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    return () => { if (prev !== 'hidden') document.body.style.overflow = prev; };
  }, [open]);

  if (!open) return null;

  return createPortal(
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      <div className="absolute inset-0 bg-foreground/30" onClick={onCancel} />
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={description ? descId : undefined}
        onClick={(e) => e.stopPropagation()}
        className="relative z-10 w-full max-w-sm rounded-xl border border-border
          bg-card p-6 shadow-lg"
      >
        <h3 id={titleId} className="text-base font-semibold">{title}</h3>
        {description && (
          <div id={descId}
            className="mt-2 space-y-1 text-sm leading-relaxed text-muted-foreground">
            {description}
          </div>
        )}
        <div className="mt-5 flex justify-end gap-2">
          <Button ref={cancelRef} size="sm" variant="outline" onClick={onCancel}>
            {cancelText}
          </Button>
          <Button size="sm" variant={variant} onClick={onConfirm}>
            {confirmText}
          </Button>
        </div>
      </div>
    </div>,
    document.body,
  );
}

const ConfirmContext = createContext<ConfirmFn | null>(null);

/** 全局挂一个宿主，页面里用 `await confirm({...})` 取代 `window.confirm`。 */
export function ConfirmProvider({ children }: { children: ReactNode }) {
  const [current, setCurrent] = useState<Pending | null>(null);
  const currentRef = useRef<Pending | null>(null);

  const settle = useCallback((ok: boolean) => {
    const p = currentRef.current;
    if (!p) return;
    currentRef.current = null;
    setCurrent(null);
    p.resolve(ok);
  }, []);

  const confirm = useCallback<ConfirmFn>((options) => {
    // 已有一个开着：直接按「取消」处理，防双击弹两个框、也避免 Promise 悬挂
    if (currentRef.current) return Promise.resolve(false);
    return new Promise<boolean>((resolve) => {
      const p = { options, resolve };
      currentRef.current = p;
      setCurrent(p);
    });
  }, []);

  useEffect(() => () => {
    const p = currentRef.current;
    if (!p) return;
    currentRef.current = null;
    p.resolve(false);
  }, []);

  return (
    <ConfirmContext.Provider value={confirm}>
      {children}
      <ConfirmDialog
        open={!!current}
        title={current?.options.title ?? ''}
        description={current?.options.description}
        confirmText={current?.options.confirmText}
        cancelText={current?.options.cancelText}
        variant={current?.options.variant}
        onConfirm={() => settle(true)}
        onCancel={() => settle(false)}
      />
    </ConfirmContext.Provider>
  );
}

export function useConfirm(): ConfirmFn {
  const ctx = useContext(ConfirmContext);
  if (!ctx) throw new Error('useConfirm 必须在 ConfirmProvider 内使用');
  return ctx;
}

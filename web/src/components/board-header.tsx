import type { ReactNode } from 'react';
import { createPortal } from 'react-dom';
import { ArrowLeft, ChevronRight } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { useShellHeaderSlots } from '@/components/platform-shell';
import { cn } from '@/lib/utils';

export interface Crumb {
  label: string;
  onClick?: () => void;
}

/** 详情看板的目录层级：返回 + 面包屑 + 右侧操作直接 portal 进平台顶栏，
 *  看板自身不再渲染独立的头部条。末项是当前页（不可点），其余可点即回退到该层。 */
export function BoardHeader({
  onBack, crumbs, meta, actions,
}: {
  onBack: () => void;
  crumbs: Crumb[];
  meta?: ReactNode;
  actions?: ReactNode;
}) {
  const { lead, actions: actionsSlot } = useShellHeaderSlots();
  if (!lead) return null;

  return (
    <>
      {createPortal(
        <div className="flex min-w-0 flex-1 items-center gap-1">
          <Button
            size="icon-sm"
            variant="ghost"
            className="-ml-1 shrink-0"
            onClick={onBack}
            aria-label="返回目录"
          >
            <ArrowLeft className="size-4" />
          </Button>
          <nav aria-label="目录层级" className="flex min-w-0 flex-1 items-center gap-1 text-sm">
            {crumbs.map((c, i) => {
              const last = i === crumbs.length - 1;
              return (
                <span key={`${c.label}-${i}`} className="flex min-w-0 items-center gap-1">
                  {i > 0 && <ChevronRight className="size-3.5 shrink-0 text-muted-foreground/60" />}
                  {c.onClick ? (
                    <button
                      type="button"
                      onClick={c.onClick}
                      title={c.label}
                      className={cn(
                        'truncate rounded px-0.5 transition-colors hover:text-foreground',
                        last ? 'font-medium text-foreground' : 'text-muted-foreground',
                      )}
                    >
                      {c.label}
                    </button>
                  ) : (
                    <span
                      title={c.label}
                      className={cn(
                        'truncate',
                        last ? 'font-medium text-foreground' : 'text-muted-foreground',
                      )}
                    >
                      {c.label}
                    </span>
                  )}
                </span>
              );
            })}
          </nav>
          {meta && (
            <div className="hidden shrink-0 items-center gap-2 text-xs tabular-nums text-muted-foreground md:flex">
              {meta}
            </div>
          )}
        </div>,
        lead,
      )}
      {actions && actionsSlot ? createPortal(actions, actionsSlot) : null}
    </>
  );
}

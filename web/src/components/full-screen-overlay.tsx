import { useEffect, type ReactNode } from 'react';
import { createPortal } from 'react-dom';

/** 详情看板外壳：portal 到 body，避免父级 space-y 的 margin 把 fixed 盒子压小。
 *  `space-y-*` 生成的是 `:where(... > :not(:last-child)) { margin-block-end }`，
 *  作用在 `fixed` 的元素上会从高度里扣掉该边距，底部露出底层页面。
 *
 *  定位见 `index.css` 的 `.rz-board-overlay`：始终从平台顶栏下方开始（目录层级与
 *  操作就在顶栏里），桌面端再从侧边栏右侧开始；`< lg` 的侧边栏是抽屉，抽屉与
 *  遮罩仍在看板之上。 */
export function FullScreenOverlay({ children }: { children: ReactNode }) {
  useEffect(() => {
    const prev = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    return () => { document.body.style.overflow = prev; };
  }, []);

  return createPortal(
    <div className="rz-board-overlay m-0 flex flex-col bg-background overscroll-contain">
      {children}
    </div>,
    document.body,
  );
}

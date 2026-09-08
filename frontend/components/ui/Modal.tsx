'use client';

import { useEffect, useRef } from 'react';

import { CloseIcon } from './Icons';
import { IconButton } from './IconButton';
import { isTopmost, popOverlay, pushOverlay } from './overlay-stack';

/**
 * A detail view, over the screen that opened it.
 *
 * Every list in this app opens one of these rather than navigating away: what
 * you clicked stays visible behind it, and closing puts you back exactly where
 * you were with your scroll position intact. Escape closes, the backdrop
 * closes, and the body cannot scroll behind it.
 *
 * **Modals nest.** Escape closes only the topmost one — a list opened from
 * inside an editor must not take the editor down with it. See
 * `overlay-stack.ts`.
 */
export function Modal({
  open,
  title,
  onClose,
  footer,
  children,
  nested = false,
}: {
  open: boolean;
  title: string;
  onClose: () => void;
  footer?: React.ReactNode;
  children?: React.ReactNode;
  /** Set when this modal was opened from inside another one. */
  nested?: boolean;
}) {
  const closeRef = useRef(onClose);
  closeRef.current = onClose;

  // Keyed on `open` ALONE, and the handler reads the latest callback from a ref.
  // An effect that also depends on `onClose` re-runs on every render — callers
  // pass an inline arrow, so its identity changes constantly — and each re-run
  // popped and re-pushed this overlay, quietly promoting it back to the top of
  // the stack. That is what made one Escape close a popover AND the editor
  // underneath it: position in the stack has to mean "opened after", not
  // "re-rendered most recently".
  useEffect(() => {
    if (!open) return;
    const token = pushOverlay();
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && isTopmost(token)) {
        event.stopPropagation();
        closeRef.current();
      }
    };
    window.addEventListener('keydown', onKey, true);
    return () => {
      window.removeEventListener('keydown', onKey, true);
      popOverlay(token);
    };
  }, [open]);

  if (!open) return null;
  // A nested modal has to sit above the one that opened it. Two levels is all
  // this app has ever needed; a third would be a sign the flow is wrong.
  const depth = nested ? 'z-[70]' : 'z-[60]';
  return (
    <div className={`fixed inset-0 flex items-center justify-center p-5 ${depth}`}>
      <div className="absolute inset-0 bg-surface-overlay backdrop-blur-[2px]" onClick={onClose} aria-hidden />
      <div
        role="dialog"
        aria-modal="true"
        aria-label={title}
        data-testid="modal"
        className="animate-fade-up relative flex max-h-[86vh] w-[min(560px,100%)] flex-col
                   overflow-hidden rounded-xl border border-surface-border bg-surface-raised shadow-panel"
      >
        <div className="flex shrink-0 items-center gap-3 px-5 py-3.5">
          <h2 className="min-w-0 flex-1 truncate text-[15px] font-medium text-ink">{title}</h2>
          <IconButton label="Close" data-testid="modal-close" className="-mr-1" onClick={onClose}>
            <CloseIcon />
          </IconButton>
        </div>
        <div className="h-px shrink-0 bg-surface-border" />
        <div className="scroll-quiet min-h-0 flex-1 overflow-y-auto px-5 py-4">{children}</div>
        {footer && (
          <>
            <div className="h-px shrink-0 bg-surface-border" />
            <div className="flex shrink-0 flex-wrap items-center gap-2 px-5 py-3">{footer}</div>
          </>
        )}
      </div>
    </div>
  );
}

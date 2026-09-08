'use client';

import { useEffect } from 'react';

import { CloseIcon } from './Icons';
import { IconButton } from './IconButton';

/**
 * A detail view, over the screen that opened it.
 *
 * Every list in this app opens one of these rather than navigating away: what
 * you clicked stays visible behind it, and closing puts you back exactly where
 * you were with your scroll position intact. Escape closes, the backdrop
 * closes, and the body cannot scroll behind it.
 */
export function Modal({
  open,
  title,
  onClose,
  footer,
  children,
}: {
  open: boolean;
  title: string;
  onClose: () => void;
  footer?: React.ReactNode;
  children?: React.ReactNode;
}) {
  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open, onClose]);

  if (!open) return null;
  return (
    <div className="fixed inset-0 z-[60] flex items-center justify-center p-5">
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

'use client';

import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';

import { isTopmost, popOverlay, pushOverlay } from './overlay-stack';

/**
 * A panel anchored to the control that opened it.
 *
 * Positioned `fixed` from the trigger's measured rect rather than absolutely
 * inside it, for one concrete reason: this app's pickers live inside modals
 * whose body scrolls, and an absolutely-positioned panel is clipped by that
 * scroll container the moment it is taller than what is left below the trigger.
 *
 * Flips above the trigger when there is not room below, and never runs off the
 * right edge. Escape and an outside click close it — but only when it is the
 * topmost overlay, so a popover opened inside a modal does not take the modal
 * with it (`overlay-stack.ts`).
 */
const GAP = 6;
const EDGE = 12;

export function Popover({
  open,
  anchorRef,
  onClose,
  width = 300,
  align = 'start',
  children,
}: {
  open: boolean;
  anchorRef: React.RefObject<HTMLElement>;
  onClose: () => void;
  width?: number;
  /** `end`: the panel's right edge lines up with the trigger's — for a trigger
   *  at the right of a row, whose panel would otherwise hang past the column. */
  align?: 'start' | 'end';
  children?: React.ReactNode;
}) {
  const panelRef = useRef<HTMLDivElement>(null);
  const [style, setStyle] = useState<React.CSSProperties>({ visibility: 'hidden' });

  // Measured after paint, before the browser shows it: reading the panel's own
  // height is what decides whether it opens downwards or flips up.
  useLayoutEffect(() => {
    if (!open) return;
    const place = () => {
      const anchor = anchorRef.current?.getBoundingClientRect();
      const panel = panelRef.current?.getBoundingClientRect();
      if (!anchor) return;
      const height = panel?.height ?? 0;
      const below = window.innerHeight - anchor.bottom - GAP;
      const flip = below < height && anchor.top > below;
      const next: React.CSSProperties = {
        position: 'fixed',
        width,
        left: Math.max(EDGE, Math.min(align === 'end' ? anchor.right - width : anchor.left,
                                      window.innerWidth - width - EDGE)),
        ...(flip
          ? { bottom: window.innerHeight - anchor.top + GAP }
          : { top: anchor.bottom + GAP }),
        maxHeight: Math.max(160, (flip ? anchor.top : below) - EDGE),
      };
      // The same answer is not a new state: without this, watching the panel's size
      // (below) and re-placing it would be a loop.
      setStyle((prev) => (JSON.stringify(prev) === JSON.stringify(next) ? prev : next));
    };
    place();
    window.addEventListener('resize', place);
    window.addEventListener('scroll', place, true);
    // Placement depends on how tall the panel is, and the panel changes height while
    // open — a list that finishes loading, a section that appears when a model is
    // chosen. Worked out only at the moment of opening, it stayed where it had fit
    // when it was smaller and ran off the bottom of the screen until something else
    // happened to move it.
    const panelEl = panelRef.current;
    const watcher = panelEl && typeof ResizeObserver !== 'undefined' ? new ResizeObserver(place) : null;
    if (panelEl && watcher) watcher.observe(panelEl);
    return () => {
      window.removeEventListener('resize', place);
      window.removeEventListener('scroll', place, true);
      watcher?.disconnect();
    };
  }, [open, anchorRef, width, align]);

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
    const onDown = (event: MouseEvent) => {
      const target = event.target as Node;
      if (panelRef.current?.contains(target) || anchorRef.current?.contains(target)) return;
      if (isTopmost(token)) closeRef.current();
    };
    window.addEventListener('keydown', onKey, true);
    window.addEventListener('mousedown', onDown, true);
    return () => {
      window.removeEventListener('keydown', onKey, true);
      window.removeEventListener('mousedown', onDown, true);
      popOverlay(token);
    };
  }, [open, anchorRef]);

  if (!open) return null;
  // Rendered into `document.body`, not where it is declared. `position: fixed` means
  // "relative to the viewport" only when no ancestor has a `transform`, `filter` or
  // `backdrop-filter` — and the conversation panel has the last of those and its rail
  // the first. Declared inside it, this was placed relative to the panel instead
  // (1075px became 2116px on a 1440px screen) and clipped by its overflow: open, but
  // nowhere a person could see it. Nothing here is ever clipped by where it came from.
  return createPortal(
    <div
      ref={panelRef}
      role="dialog"
      data-testid="popover"
      style={style}
      className="animate-fade-up z-[80] flex flex-col overflow-hidden rounded-lg border
                 border-surface-border bg-surface-raised shadow-panel"
    >
      {children}
    </div>,
    document.body,
  );
}

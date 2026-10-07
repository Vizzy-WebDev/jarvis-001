'use client';

import { useEffect, useRef, useState } from 'react';

import { CloseIcon, MicIcon } from '@/components/ui/Icons';
import { api } from '@/lib/api';
import { PAGES, type PageId } from '@/lib/nav';

/**
 * The menu (design 9a): eleven rows, sliding in from the left, and the way
 * every page is reached.
 *
 * Built from the PAGES registry, so a page added later appears here, in the
 * router and in voice navigation from one entry. Badges say what needs the
 * person without opening anything: unread notifications (red), content waiting
 * for review (blue), connectors that stopped working (amber). They are read
 * when the menu opens rather than polled — counts nobody is looking at are not
 * worth a request.
 */
export function Drawer({
  open,
  current,
  unread,
  onClose,
  onNavigate,
}: {
  open: boolean;
  current: PageId;
  unread: number;
  onClose: () => void;
  onNavigate: (id: string) => void;
}) {
  const [counts, setCounts] = useState<{ review: number; issues: number }>({ review: 0, issues: 0 });
  const closeRef = useRef(onClose);
  closeRef.current = onClose;

  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') closeRef.current();
    };
    window.addEventListener('keydown', onKey);
    let cancelled = false;
    void Promise.all([
      api.content.summary().then((s) => s.attention.toReview).catch(() => null),
      api.connectors.list()
        .then((r) => r.connectors.filter((c) => c.enabled && c.status?.state === 'error').length)
        .catch(() => null),
    ]).then(([review, issues]) => {
      if (cancelled) return;
      setCounts((was) => ({ review: review ?? was.review, issues: issues ?? was.issues }));
    });
    return () => {
      cancelled = true;
      window.removeEventListener('keydown', onKey);
    };
  }, [open]);

  const badge = (id: PageId): { text: string; tone: 'red' | 'blue' | 'amber' } | null => {
    if (id === 'notifications' && unread > 0) return { text: unread > 99 ? '99+' : String(unread), tone: 'red' };
    if (id === 'content' && counts.review > 0) return { text: `${counts.review} to review`, tone: 'blue' };
    if (id === 'capabilities' && counts.issues > 0) {
      return { text: `${counts.issues} ${counts.issues === 1 ? 'issue' : 'issues'}`, tone: 'amber' };
    }
    return null;
  };

  return (
    <>
      <div
        className={[
          'fixed inset-0 z-40 bg-surface/50 transition-opacity duration-[250ms]',
          open ? 'opacity-100' : 'pointer-events-none opacity-0',
        ].join(' ')}
        onClick={onClose}
        aria-hidden
      />
      <nav
        aria-label="Pages"
        aria-hidden={!open}
        data-testid="drawer"
        data-open={open ? 'true' : 'false'}
        className={[
          'fixed inset-y-0 left-0 z-50 flex w-[340px] max-w-[86%] flex-col border-r border-line/25',
          'bg-gradient-to-b from-surface-raised/[0.97] to-surface/[0.98]',
          'shadow-[24px_0_60px_-20px_rgb(0_0_0/0.85)]',
          'transition-transform duration-[280ms] ease-out',
          open ? 'translate-x-0' : '-translate-x-[104%]',
        ].join(' ')}
      >
        <div className="flex items-center gap-3 pb-3.5 pl-5 pr-4 pt-[18px]">
          <div className="flex flex-col gap-[3px]">
            <span className="flex items-center gap-2.5 text-[15px] tracking-[0.34em] text-ink-strong">
              <span className="h-2.5 w-2.5 rounded-full bg-[radial-gradient(circle,#fff_0,rgb(var(--orb-standby))_50%,transparent_80%)] shadow-[0_0_12px_rgb(var(--orb-standby))]" />
              JARVIS
            </span>
            <span className="text-[12.5px] text-ink-muted">Everything it can do.</span>
          </div>
          <button
            type="button"
            aria-label="Close menu"
            title="Close menu"
            tabIndex={open ? 0 : -1}
            onClick={onClose}
            className="ml-auto inline-flex h-10 w-10 items-center justify-center rounded-[10px] text-ink-soft
                       hover:bg-line/[0.08] hover:text-white"
          >
            <CloseIcon className="h-[18px] w-[18px]" />
          </button>
        </div>
        <div className="mx-4 h-px bg-line/[0.14]" />

        <div className="scroll-quiet flex min-h-0 flex-1 flex-col gap-px overflow-y-auto overflow-x-hidden pb-2 pl-3 pr-2.5 pt-2.5">
          {PAGES.map((page) => {
            const active = page.id === current;
            const b = badge(page.id);
            return (
              <button
                key={page.id}
                type="button"
                tabIndex={open ? 0 : -1}
                data-testid={`nav-${page.id}`}
                aria-current={active ? 'page' : undefined}
                onClick={() => {
                  onNavigate(page.id);
                  onClose();
                }}
                className={[
                  'relative flex min-h-[38px] items-center gap-[11px] rounded-[10px] pl-2 pr-2.5 text-left',
                  'transition-colors duration-150 hover:bg-line/[0.08] hover:text-ink-strong',
                  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/60',
                  active ? 'bg-line/10 text-ink-strong' : 'text-ink-soft',
                ].join(' ')}
              >
                <span
                  aria-hidden
                  className={[
                    'absolute -left-2.5 top-1/2 -mt-2.5 h-5 w-[3px] rounded-r-[3px]',
                    active ? 'bg-orb-standby shadow-[0_0_10px_rgb(var(--orb-standby))]' : 'bg-transparent',
                  ].join(' ')}
                />
                <span
                  className={[
                    'inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-lg border transition-all duration-150',
                    active ? 'border-orb-standby/45 bg-orb-standby/[0.12]' : 'border-line/[0.16] bg-line/[0.04]',
                  ].join(' ')}
                >
                  <svg viewBox="0 0 24 24" aria-hidden
                       className={`h-4 w-4 fill-none stroke-[1.7] ${active ? 'stroke-orb-standby' : 'stroke-[#9fb2c6]'}`}
                       strokeLinecap="round" strokeLinejoin="round">
                    <path d={page.icon} />
                  </svg>
                </span>
                <span className="min-w-0 flex-1 truncate text-[14px]">{page.label}</span>
                {b && (
                  <span
                    data-testid={`nav-badge-${page.id}`}
                    className={[
                      'shrink-0 rounded-full px-2 py-0.5 text-[11px]',
                      b.tone === 'red' ? 'bg-[rgb(255_59_48/0.16)] text-badge-red'
                        : b.tone === 'amber' ? 'bg-state-warn/[0.14] text-badge-amber'
                          : 'bg-accent/[0.14] text-badge-blue',
                    ].join(' ')}
                  >
                    {b.text}
                  </span>
                )}
              </button>
            );
          })}
        </div>

        <div className="flex items-center gap-2.5 border-t border-line/[0.12] px-[18px] pb-3.5 pt-3 text-[12.5px] text-ink-muted">
          <MicIcon className="h-3.5 w-3.5" />
          <span>Or say “Jarvis, open Memory”</span>
          <span className="ml-auto rounded-[5px] border border-line/25 px-1.5 py-0.5 font-mono text-[10.5px] text-ink-soft">
            ESC
          </span>
        </div>
      </nav>
    </>
  );
}

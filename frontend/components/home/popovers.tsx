'use client';

import { useEffect, useRef, useState } from 'react';

import { isTopmost, popOverlay, pushOverlay } from '@/components/ui/overlay-stack';
import { api } from '@/lib/api';
import type { Notification } from '@/lib/api-types';

/**
 * The frame every Home popover shares: a glass card hanging under the header
 * with a lit top edge, a click-catcher behind it, and Escape closing only the
 * topmost overlay (the shared overlay stack — a list opened inside it closes
 * first).
 */
export function HomePopover({ open, onClose, width, right = 20, testId, children, className = '' }: {
  open: boolean;
  onClose: () => void;
  width: number | string;
  right?: number;
  testId: string;
  children: React.ReactNode;
  className?: string;
}) {
  const closeRef = useRef(onClose);
  closeRef.current = onClose;
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
  return (
    <>
      <div aria-hidden className="absolute inset-0 z-[11]" onClick={onClose} />
      <div
        data-testid={testId}
        role="dialog"
        className={[
          'absolute top-[66px] z-[12] flex max-w-[calc(100vw-24px)] flex-col overflow-hidden rounded-[14px] border border-line/[0.28]',
          'bg-gradient-to-b from-[rgb(15_22_31/0.97)] to-[rgb(8_11_16/0.97)] shadow-[0_24px_60px_-16px_rgba(0,0,0,0.85)]',
          'animate-fade-up',
          className,
        ].join(' ')}
        style={{ right, width }}
      >
        <span aria-hidden className="absolute left-[22px] right-[22px] top-0 h-px bg-[linear-gradient(90deg,transparent,rgba(170,210,240,0.55),transparent)]" />
        {children}
      </div>
    </>
  );
}

const LEVEL_COLOUR: Record<Notification['level'], string> = {
  error: '#ff3b30',
  warning: '#f5a524',
  success: '#6be3a3',
  info: '#7fb8e6',
};

/** When something happened, the way the bell says it: a time today, a weekday
 *  this week, a date before that. */
export function when(ts: string): string {
  const d = new Date(ts);
  const now = new Date();
  if (d.toDateString() === now.toDateString()) {
    return d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
  }
  if (now.getTime() - d.getTime() < 6 * 86400_000) return d.toLocaleDateString(undefined, { weekday: 'short' });
  return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}

/**
 * The bell (design 1i): the recent notifications in a 340px scrolling list,
 * unread ones lit; tapping one opens it inside the popover (← Recent) with its
 * own action and "Open on page"; Mark all read; View all → the full page.
 * Opening one marks it read for real, and the count on the bell follows.
 */
export function BellPopover({ open, onClose, onNavigate, onUnread }: {
  open: boolean;
  onClose: () => void;
  onNavigate: (section: string) => void;
  /** The unread count after a change made here, for the badge. */
  onUnread: (count: number) => void;
}) {
  const [items, setItems] = useState<Notification[] | null>(null);
  const [detail, setDetail] = useState<Notification | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) {
      setDetail(null);
      return;
    }
    api.notifications.list(30)
      .then((found) => {
        setItems(found.notifications);
        onUnread(found.notifications.filter((n) => !n.read).length);
        setError(null);
      })
      .catch(() => setError('Could not read your notifications.'));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  const unread = (items ?? []).filter((n) => !n.read).length;

  const apply = (next: Notification[]) => {
    setItems(next);
    onUnread(next.filter((n) => !n.read).length);
  };

  const openOne = (n: Notification) => {
    setDetail(n);
    if (n.read) return;
    apply((items ?? []).map((x) => (x.id === n.id ? { ...x, read: true } : x)));
    api.notifications.markRead([n.id]).catch(() => undefined);
  };

  const markAll = () => {
    apply((items ?? []).map((x) => ({ ...x, read: true })));
    api.notifications.markAllRead().catch(() => undefined);
  };

  const go = (section: string) => {
    onClose();
    onNavigate(section);
  };

  return (
    <HomePopover open={open} onClose={onClose} width={380} testId="bell-popover">
      <div className="flex items-center gap-2 px-3.5 pb-2.5 pt-3">
        <span className="text-[14px] font-medium text-ink-strong">Notifications</span>
        {unread > 0 && (
          <span className="rounded-full bg-[rgb(255_59_48/0.14)] px-2 py-0.5 text-[11px] text-badge-red">{unread} new</span>
        )}
        <button type="button" data-testid="bell-mark-all" onClick={markAll}
                className="ml-auto px-0.5 py-1 text-[12.5px] text-ink-muted hover:text-ink-strong">
          Mark all read
        </button>
      </div>
      <div className="h-px bg-line/[0.14]" />

      {detail ? (
        <div data-testid="bell-detail" className="flex min-h-[340px] flex-col px-3.5 pb-3.5 pt-3">
          <button type="button" onClick={() => setDetail(null)} data-testid="bell-back"
                  className="flex items-center gap-1.5 self-start pb-2 pt-0.5 text-[12.5px] text-[#9fd0f2] hover:text-white">
            <svg viewBox="0 0 24 24" className="h-3.5 w-3.5 fill-none stroke-current stroke-2" strokeLinecap="round" aria-hidden>
              <path d="m15 6-6 6 6 6" />
            </svg>
            Recent
          </button>
          <div className="flex items-start gap-2.5">
            <span className="mt-1.5 h-2 w-2 shrink-0 rounded-full"
                  style={{ background: LEVEL_COLOUR[detail.level], boxShadow: `0 0 8px ${LEVEL_COLOUR[detail.level]}` }} />
            <span className="flex-1 text-[14.5px] font-medium leading-snug text-ink-strong">{detail.title}</span>
            <span className="mt-0.5 text-[11.5px] text-ink-muted">{when(detail.ts)}</span>
          </div>
          <p className="whitespace-pre-wrap pb-3 pl-[18px] pt-2 text-[13px] leading-relaxed text-ink-soft">{detail.body}</p>
          <div className="flex gap-2 pl-[18px]">
            {detail.action && (
              <button type="button" data-testid="bell-action" onClick={() => go(detail.action!.section)}
                      className="h-8 rounded-[9px] border border-line/40 bg-[rgb(63_127_174/0.35)] px-3.5 text-[12.5px] text-ink-strong hover:bg-[rgb(63_127_174/0.6)]">
                {detail.action.label}
              </button>
            )}
            <button type="button" onClick={() => go('notifications')}
                    className="h-8 rounded-[9px] px-3 text-[12.5px] text-ink-muted hover:text-white">
              Open on page
            </button>
          </div>
        </div>
      ) : (
        <div className="flex max-h-[340px] flex-col overflow-y-auto overscroll-contain p-1.5" data-testid="bell-list">
          {error && <p className="px-2.5 py-3 text-[13px] text-badge-red">{error}</p>}
          {items === null && !error && <p className="px-2.5 py-3 text-[13px] text-ink-muted">Loading…</p>}
          {items?.length === 0 && <p className="px-2.5 py-3 text-[13px] text-ink-muted">Nothing yet.</p>}
          {(items ?? []).map((n) => (
            <button key={n.id} type="button" data-testid="bell-row" onClick={() => openOne(n)}
                    className="flex w-full items-start gap-[11px] rounded-[10px] p-2.5 text-left hover:bg-line/[0.09]"
                    style={{ background: n.read ? undefined : 'rgb(var(--line) / 0.07)' }}>
              <span className="mt-[5px] h-2 w-2 shrink-0 rounded-full"
                    style={{ background: LEVEL_COLOUR[n.level], boxShadow: `0 0 8px ${LEVEL_COLOUR[n.level]}` }} />
              <span className="flex min-w-0 flex-1 flex-col gap-0.5">
                <span className="flex items-baseline gap-2">
                  <span className={`truncate text-[13.5px] ${n.read ? 'text-ink-soft' : 'font-medium text-ink-strong'}`}>
                    {n.title}{n.count > 1 ? ` ×${n.count}` : ''}
                  </span>
                  <span className="ml-auto shrink-0 text-[11.5px] text-ink-muted">{when(n.ts)}</span>
                </span>
                <span className="truncate text-[12.5px] text-ink-muted">{n.body}</span>
              </span>
            </button>
          ))}
        </div>
      )}

      <div className="border-t border-line/[0.14] p-2">
        <button type="button" data-testid="bell-view-all" onClick={() => go('notifications')}
                className="min-h-[38px] w-full rounded-[10px] border border-line/[0.28] bg-line/[0.06] text-[13.5px] text-ink-strong hover:bg-line/[0.14]">
          View all →
        </button>
      </div>
    </HomePopover>
  );
}

/** "Show on Home": which panels are showing, Rest now, and Just Jarvis (F). */
export function ShowOnHome({ open, onClose, show, onShow, onRest, onPresenceOnly }: {
  open: boolean;
  onClose: () => void;
  show: { chat: boolean; sys: boolean; jar: boolean };
  onShow: (next: { chat: boolean; sys: boolean; jar: boolean }) => void;
  onRest: () => void;
  onPresenceOnly: () => void;
}) {
  const any = show.chat || show.sys || show.jar;
  const rows: [keyof typeof show, string][] = [['chat', 'Conversation'], ['sys', 'System Health'], ['jar', 'Jarvis Health']];
  return (
    <HomePopover open={open} onClose={onClose} width={272} testId="show-on-home" className="gap-3 p-4">
      <span className="font-mono text-[11px] tracking-[0.18em] text-ink-muted">SHOW ON HOME</span>
      {rows.map(([key, label]) => (
        <button key={key} type="button" role="switch" aria-checked={show[key]} data-testid={`show-${key}`}
                onClick={() => onShow({ ...show, [key]: !show[key] })}
                className="flex items-center justify-between py-1 text-[14px] text-ink">
          <span>{label}</span>
          <span className="relative h-5 w-[34px] rounded-full transition-colors duration-200"
                style={{ background: show[key] ? '#5e93bd' : 'rgba(255,255,255,0.14)' }}>
            <span className="absolute top-0.5 h-4 w-4 rounded-full bg-white transition-[left] duration-200"
                  style={{ left: show[key] ? 16 : 2 }} />
          </span>
        </button>
      ))}
      <div className="h-px bg-line/[0.14]" />
      <button type="button" data-testid="rest-now" onClick={onRest}
              className="rounded-[10px] border border-line/25 p-2.5 text-[13.5px] text-ink-soft hover:bg-line/10">
        Rest now
      </button>
      <button type="button" data-testid="presence-only" onClick={onPresenceOnly}
              className="rounded-[10px] border border-line/25 bg-line/[0.06] p-2.5 text-[13.5px] text-ink hover:bg-line/[0.12]">
        {any ? 'Just Jarvis' : 'Show everything'}
      </button>
      <span className="text-[12px] leading-normal text-ink-muted">Press F to toggle.</span>
    </HomePopover>
  );
}

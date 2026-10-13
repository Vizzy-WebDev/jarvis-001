'use client';

import { useCallback, useEffect, useRef, useState } from 'react';

import { isTopmost, popOverlay, pushOverlay } from '@/components/ui/overlay-stack';
import { api, ApiRequestError } from '@/lib/api';
import type { Conversation } from '@/lib/api-types';

/**
 * Chat history, sliding in over the conversation it belongs to (design Home v6,
 * "Latest additions"): a search box, Pinned and Recent groups that fold, and
 * under "Show archived" an Archived group. Every row has Archive (Restore when
 * archived) and Pin beside it; the open chat is marked in Jarvis's colour.
 * "View all →" opens the full Chat History page, which keeps the recycle bin.
 *
 * It lives INSIDE the conversation panel, positioned against it — not fixed to
 * the window — so it always opens over the panel it was asked from.
 */
export function ChatHistoryDrawer({
  open,
  onClose,
  onViewAll,
  onResume,
  currentId,
  accent,
}: {
  open: boolean;
  onClose: () => void;
  onViewAll: () => void;
  onResume: (id: string) => void;
  /** The conversation on screen now. */
  currentId: string | null;
  /** Jarvis's state colour, for the open chat's mark. */
  accent: string;
}) {
  const [rows, setRows] = useState<Conversation[] | null>(null);
  const [archived, setArchived] = useState<Conversation[]>([]);
  const [showArchived, setShowArchived] = useState(false);
  const [query, setQuery] = useState('');
  const [error, setError] = useState<string | null>(null);
  // Both default open — collapsing is for a list that's grown long enough to
  // want to save the scroll space, not the drawer's starting state.
  const [pinnedExpanded, setPinnedExpanded] = useState(true);
  const [recentExpanded, setRecentExpanded] = useState(true);
  const closeRef = useRef(onClose);
  closeRef.current = onClose;

  const load = useCallback(async () => {
    const q = query.trim() || undefined;
    try {
      const [live, gone] = await Promise.all([
        api.conversations.list({ query: q }),
        showArchived ? api.conversations.list({ query: q, includeArchived: true }) : Promise.resolve(null),
      ]);
      setRows(live.conversations);
      setArchived(gone?.conversations ?? []);
      setError(null);
    } catch (err) {
      setError(err instanceof ApiRequestError ? err.message : 'Could not read your chats.');
      setRows([]);
    }
  }, [query, showArchived]);

  // A search reads as you type, without asking the server on every key.
  useEffect(() => {
    if (!open) return;
    const timer = setTimeout(() => void load(), query ? 200 : 0);
    return () => clearTimeout(timer);
  }, [open, load, query]);

  useEffect(() => {
    if (!open) {
      setQuery('');
      return;
    }
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

  async function update(conversation: Conversation, patch: { pinned?: boolean; archived?: boolean }) {
    try {
      await api.conversations.update(conversation.id, patch);
    } catch {
      setError("Couldn't save that — try again.");
    }
    await load(); // re-sorts the list; a fresh read is the real order
  }

  const pinned = (rows ?? []).filter((c) => c.pinned);
  const recent = (rows ?? []).filter((c) => !c.pinned);
  const nothing = rows !== null && !rows.length && !(showArchived && archived.length);
  const tab = open ? 0 : -1;
  const row = (conversation: Conversation, isArchived = false) => (
    <Row key={conversation.id} conversation={conversation} archived={isArchived} tab={tab}
         current={conversation.id === currentId} accent={accent}
         onResume={onResume}
         onPin={() => void update(conversation, { pinned: !conversation.pinned })}
         onArchive={() => void update(conversation, { archived: !isArchived })} />
  );

  return (
    <>
      <div
        aria-hidden
        onClick={onClose}
        className="absolute inset-x-0 bottom-0 top-[51px] z-[3] bg-[rgb(3_5_8/0.6)] transition-opacity duration-200"
        style={{ opacity: open ? 1 : 0, pointerEvents: open ? 'auto' : 'none' }}
      />
      <aside
        aria-label="Chat history"
        aria-hidden={!open}
        data-testid="chat-history-drawer"
        data-open={open ? 'true' : 'false'}
        className="absolute bottom-0 left-0 top-[51px] z-[4] flex w-[84%] flex-col rounded-br-[14px] border-r border-line/[0.26]
                   bg-gradient-to-b from-[rgb(12_18_26/0.99)] to-[rgb(7_10_15/0.99)] shadow-[18px_0_40px_-18px_rgba(0,0,0,0.9)]
                   transition-transform duration-[260ms] ease-out"
        style={{ transform: open ? 'translateX(0)' : 'translateX(-104%)', pointerEvents: open ? 'auto' : 'none' }}
      >
        <div className="flex items-center gap-2 pb-1.5 pl-4 pr-2 pt-3">
          <span className="text-[14px] font-medium text-ink-strong">Chat History</span>
          <button type="button" title="Close" aria-label="Close" onClick={onClose} tabIndex={tab}
                  className="ml-auto inline-flex h-[34px] w-[34px] items-center justify-center rounded-[9px] text-ink-soft hover:bg-line/[0.08] hover:text-white">
            <svg viewBox="0 0 24 24" aria-hidden className="h-4 w-4 fill-none stroke-current stroke-[1.8]" strokeLinecap="round"><path d="M6 6l12 12M18 6 6 18" /></svg>
          </button>
        </div>

        <div className="px-3 pb-2.5 pt-1">
          <div className="flex h-[38px] items-center gap-2 rounded-[10px] border border-line/[0.22] bg-[rgb(8_12_18/0.85)] px-3">
            <svg viewBox="0 0 24 24" aria-hidden className="h-[15px] w-[15px] shrink-0 fill-none stroke-ink-muted stroke-[1.8]" strokeLinecap="round">
              <circle cx="11" cy="11" r="7" /><path d="m20 20-3.5-3.5" />
            </svg>
            <input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Search chats" tabIndex={tab}
                   data-testid="drawer-search"
                   className="min-w-0 flex-1 bg-transparent text-[13.5px] text-ink-strong outline-none placeholder:text-ink-muted" />
            {query && (
              <button type="button" title="Clear search" aria-label="Clear search" onClick={() => setQuery('')} tabIndex={tab}
                      className="inline-flex h-6 w-6 items-center justify-center rounded-md text-ink-muted hover:bg-line/[0.12] hover:text-white">
                <svg viewBox="0 0 24 24" aria-hidden className="h-3 w-3 fill-none stroke-current stroke-2" strokeLinecap="round"><path d="M6 6l12 12M18 6 6 18" /></svg>
              </button>
            )}
          </div>
        </div>

        <div className="px-4 pb-2.5">
          <button type="button" data-testid="drawer-toggle-archived" tabIndex={tab}
                  onClick={() => setShowArchived((was) => !was)}
                  className="text-[12.5px] text-ink-muted hover:text-ink-strong">
            {showArchived ? 'Hide archived' : 'Show archived'}
          </button>
        </div>

        {error && <p className="px-4 pb-2 text-[12px] text-badge-red">{error}</p>}

        <div className="scroll-quiet flex min-h-0 flex-1 flex-col gap-3 overflow-auto px-2 pb-2.5">
          {nothing && (
            <p className="px-3 py-2 text-[13px] leading-normal text-ink-muted">
              {query ? `No chats match “${query}”.` : 'Nothing here yet.'}
            </p>
          )}
          {pinned.length > 0 && (
            <section className="flex flex-col gap-px">
              <GroupHead label="PINNED" expanded={pinnedExpanded} onToggle={() => setPinnedExpanded((was) => !was)}
                         testId="drawer-pinned-toggle" tab={tab} />
              {pinnedExpanded && (
                <ul className="flex flex-col gap-px" data-testid="drawer-pinned-list">{pinned.map((c) => row(c))}</ul>
              )}
            </section>
          )}
          {recent.length > 0 && (
            <section className="flex flex-col gap-px">
              {pinned.length > 0 && (
                <GroupHead label="RECENT" expanded={recentExpanded} onToggle={() => setRecentExpanded((was) => !was)}
                           testId="drawer-recent-toggle" tab={tab} />
              )}
              {(pinned.length === 0 || recentExpanded) && (
                <ul className="flex flex-col gap-px" data-testid="drawer-recent-list">{recent.map((c) => row(c))}</ul>
              )}
            </section>
          )}
          {showArchived && archived.length > 0 && (
            <section className="flex flex-col gap-px">
              <span className="self-start px-2.5 pb-1.5 pt-0.5 font-mono text-[10.5px] tracking-[0.18em] text-ink-muted">ARCHIVED</span>
              <ul className="flex flex-col gap-px" data-testid="drawer-archived-list">{archived.map((c) => row(c, true))}</ul>
            </section>
          )}
        </div>

        <div className="border-t border-line/[0.14] p-2.5">
          <button type="button" data-testid="drawer-view-all" onClick={onViewAll} tabIndex={tab}
                  className="min-h-[38px] w-full rounded-[10px] border border-line/[0.28] bg-line/[0.06] text-[13.5px] text-ink-strong hover:bg-line/[0.14]">
            View all →
          </button>
        </div>
      </aside>
    </>
  );
}

/** A group label that is also its own fold toggle. */
function GroupHead({ label, expanded, onToggle, testId, tab }: {
  label: string;
  expanded: boolean;
  onToggle: () => void;
  testId: string;
  tab: number;
}) {
  return (
    <button type="button" data-testid={testId} aria-expanded={expanded} onClick={onToggle} tabIndex={tab}
            className="flex items-center gap-1.5 self-start px-2.5 pb-1.5 pt-0.5 font-mono text-[10.5px] tracking-[0.18em] text-ink-muted hover:text-ink-strong">
      {label}
      <svg viewBox="0 0 24 24" aria-hidden className={`h-3 w-3 fill-none stroke-current stroke-2 transition-transform ${expanded ? '' : '-rotate-90'}`} strokeLinecap="round">
        <path d="m6 9 6 6 6-6" />
      </svg>
    </button>
  );
}

function Row({ conversation, archived, current, accent, tab, onResume, onPin, onArchive }: {
  conversation: Conversation;
  archived: boolean;
  current: boolean;
  accent: string;
  tab: number;
  onResume: (id: string) => void;
  onPin: () => void;
  onArchive: () => void;
}) {
  const pinned = conversation.pinned && !archived;
  return (
    <li>
      <div
        data-testid="drawer-chat-row"
        className="group relative flex min-h-[38px] items-center gap-1 rounded-[9px] pl-3 pr-1 transition-colors hover:bg-line/[0.09]"
        style={{ background: current ? 'rgb(var(--line) / 0.12)' : pinned ? 'rgb(var(--line) / 0.05)' : undefined }}
      >
        <span aria-hidden className="absolute left-0 top-1/2 -mt-[9px] h-[18px] w-0.5 rounded-sm"
              style={{ background: current ? accent : 'transparent', boxShadow: current ? `0 0 8px ${accent}` : undefined }} />
        <button
          type="button"
          data-testid="drawer-resume"
          onClick={() => onResume(conversation.id)}
          tabIndex={tab}
          className="min-w-0 flex-1 truncate py-[9px] text-left text-[13.5px]"
          style={{ color: archived ? 'rgb(var(--ink-muted))' : current ? '#fff' : '#dce4ee' }}
        >
          {conversation.title || 'Untitled conversation'}
        </button>
        <button
          type="button"
          data-testid="drawer-toggle-archive"
          aria-label={archived ? 'Restore this chat' : 'Archive this chat'}
          title={archived ? 'Restore this chat' : 'Archive this chat'}
          onClick={onArchive}
          tabIndex={tab}
          className="inline-flex h-[30px] w-[30px] shrink-0 items-center justify-center rounded-lg text-ink-muted opacity-35 transition-opacity hover:bg-line/[0.12] hover:opacity-100"
        >
          <svg viewBox="0 0 24 24" aria-hidden className="h-[15px] w-[15px] fill-none stroke-current stroke-[1.8]" strokeLinecap="round" strokeLinejoin="round">
            <path d={archived ? 'M3 12a9 9 0 1 0 3-6.7L3 8M3 3v5h5' : 'M3 4h18v4H3zM5 8v12h14V8M10 12h4'} />
          </svg>
        </button>
        {!archived && (
          <button
            type="button"
            data-testid="drawer-toggle-pin"
            aria-label={pinned ? 'Unpin this chat' : 'Pin this chat'}
            title={pinned ? 'Unpin this chat' : 'Pin this chat'}
            aria-pressed={pinned}
            onClick={onPin}
            tabIndex={tab}
            className="inline-flex h-[30px] w-[30px] shrink-0 items-center justify-center rounded-lg transition-opacity hover:bg-line/[0.12] hover:opacity-100"
            style={{ color: pinned ? '#9fc3e0' : 'rgb(var(--ink-muted))', opacity: pinned ? 1 : 0.35 }}
          >
            <svg viewBox="0 0 24 24" aria-hidden className="h-[15px] w-[15px] stroke-current stroke-[1.8]" strokeLinecap="round" strokeLinejoin="round"
                 style={{ fill: pinned ? 'currentColor' : 'none' }}>
              <path d="M9 3h6l-1 7 3.5 3.5V15h-11v-1.5L10 10zM12 15v6" />
            </svg>
          </button>
        )}
      </div>
    </li>
  );
}

'use client';

import { useCallback, useEffect, useState } from 'react';

import { Button } from '@/components/ui/Button';
import { CloseIcon, PinIcon } from '@/components/ui/Icons';
import { IconButton } from '@/components/ui/IconButton';
import { api, ApiRequestError } from '@/lib/api';
import type { Conversation } from '@/lib/api-types';

/**
 * Quick access to chat history from the conversation panel itself — a slide-
 * out, not a full page. Pinned chats sort first (the backend already does
 * this) and get their own labelled group with a filled pin mark, so pinned
 * and unpinned are never just "the same row, in a slightly different order."
 *
 * Deliberately thin: search, the recycle bin's own Restore/Delete-forever
 * controls, and the full pin/archive management UI live on the full Chat
 * History page ("View all" below) where there is room for them. This is the
 * quick list you glance at without leaving the conversation.
 */
export function ChatHistoryDrawer({
  open,
  onClose,
  onViewAll,
  onResume,
}: {
  open: boolean;
  onClose: () => void;
  onViewAll: () => void;
  onResume: (id: string) => void;
}) {
  const [rows, setRows] = useState<Conversation[] | null>(null);
  const [showArchived, setShowArchived] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const found = await api.conversations.list({ includeArchived: showArchived });
      setRows(found.conversations);
      setError(null);
    } catch (err) {
      setError(err instanceof ApiRequestError ? err.message : 'Could not read your chats.');
      setRows([]);
    }
  }, [showArchived]);

  useEffect(() => {
    if (open) void load();
  }, [open, load]);

  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open, onClose]);

  async function togglePinned(conversation: Conversation) {
    try {
      await api.conversations.update(conversation.id, { pinned: !conversation.pinned });
    } catch {
      setError("Couldn't save that — try again.");
    }
    await load();  // pinning re-sorts the list; a fresh read is the real order
  }

  const pinned = (rows ?? []).filter((c) => c.pinned);
  const recent = (rows ?? []).filter((c) => !c.pinned);

  return (
    <>
      <div
        className={[
          'fixed inset-0 z-40 bg-surface-overlay backdrop-blur-[1px] transition-opacity duration-200',
          open ? 'opacity-100' : 'pointer-events-none opacity-0',
        ].join(' ')}
        onClick={onClose}
        aria-hidden
      />
      {/* Anchored to the RIGHT, at the same offset as the conversation panel
          it's opened from (`right: var(--rail-gutter)`, app/page.tsx) — this
          used to copy the main app Drawer's left-edge positioning verbatim,
          which is correct for that global menu but put this one sliding out
          from the opposite side of the screen from its own trigger button.
          Still rendered here at the page's top level rather than nested
          inside the conversation panel, for the reason given where this is
          used: that panel's own backdrop-blur-xl traps position:fixed
          descendants. */}
      <aside
        aria-label="Chat history"
        aria-hidden={!open}
        data-testid="chat-history-drawer"
        data-open={open ? 'true' : 'false'}
        className={[
          'fixed inset-y-0 z-50 flex w-[300px] max-w-[86vw] flex-col',
          'border-l border-surface-border bg-surface-raised/95 backdrop-blur-xl shadow-panel',
          'transition-transform duration-200 ease-out',
          open ? 'translate-x-0' : 'translate-x-full',
        ].join(' ')}
        style={{ right: 'var(--rail-gutter)' }}
      >
        <div className="flex items-center gap-2 px-4 pb-3 pt-5">
          <p className="text-[13px] font-semibold text-ink">Chat History</p>
          <IconButton label="Close" className="ml-auto -mr-1 h-8 w-8" onClick={onClose}
                      tabIndex={open ? 0 : -1}>
            <CloseIcon className="h-[16px] w-[16px]" />
          </IconButton>
        </div>

        <div className="px-4 pb-2">
          <button
            type="button"
            data-testid="drawer-toggle-archived"
            onClick={() => setShowArchived((was) => !was)}
            className="text-[12px] text-ink-faint hover:text-ink"
            tabIndex={open ? 0 : -1}
          >
            {showArchived ? 'Hide archived' : 'Show archived'}
          </button>
        </div>

        {error && <p className="px-4 pb-2 text-[12px] text-state-danger">{error}</p>}

        <div className="scroll-quiet flex-1 overflow-y-auto px-2 pb-2">
          {rows === null ? null : rows.length === 0 ? (
            <p className="px-2 py-4 text-[12px] text-ink-faint">Nothing here yet.</p>
          ) : (
            <>
              {pinned.length > 0 && (
                <section className="mb-3">
                  <h3 className="px-2 pb-1 text-[10px] font-semibold uppercase tracking-[0.16em] text-ink-faint">
                    Pinned
                  </h3>
                  <ul className="space-y-0.5" data-testid="drawer-pinned-list">
                    {pinned.map((conversation) => (
                      <Row key={conversation.id} conversation={conversation} open={open}
                          onResume={onResume} onTogglePinned={togglePinned} />
                    ))}
                  </ul>
                </section>
              )}
              <section>
                {pinned.length > 0 && (
                  <h3 className="px-2 pb-1 text-[10px] font-semibold uppercase tracking-[0.16em] text-ink-faint">
                    Recent
                  </h3>
                )}
                <ul className="space-y-0.5" data-testid="drawer-recent-list">
                  {recent.map((conversation) => (
                    <Row key={conversation.id} conversation={conversation} open={open}
                        onResume={onResume} onTogglePinned={togglePinned} />
                  ))}
                </ul>
              </section>
            </>
          )}
        </div>

        <div className="border-t border-surface-border p-3">
          <Button data-testid="drawer-view-all" className="w-full justify-center"
                  onClick={onViewAll} tabIndex={open ? 0 : -1}>
            View all →
          </Button>
        </div>
      </aside>
    </>
  );
}

function Row({ conversation, open, onResume, onTogglePinned }: {
  conversation: Conversation;
  open: boolean;
  onResume: (id: string) => void;
  onTogglePinned: (conversation: Conversation) => void;
}) {
  return (
    <li>
      <div
        data-testid="drawer-chat-row"
        className={[
          'group flex items-center gap-1.5 rounded px-2 py-1.5 transition',
          conversation.pinned ? 'bg-accent/[0.06] hover:bg-accent/[0.10]' : 'hover:bg-white/[0.05]',
        ].join(' ')}
      >
        <button
          type="button"
          data-testid="drawer-resume"
          onClick={() => onResume(conversation.id)}
          tabIndex={open ? 0 : -1}
          className="min-w-0 flex-1 truncate text-left text-[13px] text-ink"
        >
          {conversation.title || 'Untitled conversation'}
        </button>
        <button
          type="button"
          data-testid="drawer-toggle-pin"
          aria-label={conversation.pinned ? 'Unpin this chat' : 'Pin this chat'}
          aria-pressed={conversation.pinned}
          onClick={() => onTogglePinned(conversation)}
          tabIndex={open ? 0 : -1}
          className={[
            'shrink-0 rounded p-1 transition',
            conversation.pinned
              ? 'text-accent opacity-100'
              : 'text-ink-faint opacity-0 hover:text-ink group-hover:opacity-100',
          ].join(' ')}
        >
          <PinIcon className="h-3.5 w-3.5" />
        </button>
      </div>
    </li>
  );
}

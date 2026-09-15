'use client';

import { useCallback, useEffect, useState } from 'react';

import { Message as MessageView, type Turn } from '@/components/conversation/Message';
import { Button } from '@/components/ui/Button';
import { Card } from '@/components/ui/Card';
import { EmptyState } from '@/components/ui/EmptyState';
import { Field, inputClass } from '@/components/ui/Field';
import { PinIcon } from '@/components/ui/Icons';
import { Modal } from '@/components/ui/Modal';
import { api, ApiRequestError } from '@/lib/api';
import type { Conversation, Message } from '@/lib/api-types';

/**
 * Every past conversation, searchable.
 *
 * **Search runs on the server, over the full transcript**, not over the titles
 * on this page — conversations are kept whole and indexed for exactly this, and
 * filtering a list of titles in the browser would quietly answer a different,
 * much worse question.
 *
 * Opening one shows it; picking it up makes it the live conversation, so the
 * next thing said continues it. Those are deliberately two different actions:
 * reading an old thread is the common one, and resuming it by accident is not
 * something anyone wants.
 */
export function ChatHistoryScreen({ onNavigate, onResumeConversation }: {
  onNavigate?: (id: string) => void;
  /** Actually loads the picked-up conversation's transcript into the live
   *  panel — `onNavigate('home')` alone only changes which SCREEN is shown. */
  onResumeConversation?: (id: string) => void;
}) {
  const [rows, setRows] = useState<Conversation[] | null>(null);
  const [activeId, setActiveId] = useState('');
  const [query, setQuery] = useState('');
  const [archived, setArchived] = useState(false);
  const [openId, setOpenId] = useState<string | null>(null);
  const [binOpen, setBinOpen] = useState(false);
  const [trashCount, setTrashCount] = useState(0);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const [found, trashed] = await Promise.all([
        api.conversations.list({ query, includeArchived: archived }),
        api.conversations.trash(),
      ]);
      setRows(found.conversations);
      setActiveId(found.activeId);
      setTrashCount(trashed.conversations.length);
      setError(null);
    } catch (err) {
      setError(err instanceof ApiRequestError ? err.message : 'Could not read your conversations.');
      setRows([]);
    }
  }, [query, archived]);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <>

      <div className="mb-4 flex flex-wrap items-center gap-2">
        <input
          className={`${inputClass} max-w-xs`}
          data-testid="history-search"
          placeholder="Search everything said"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
        />
        <Button data-testid="toggle-archived" onClick={() => setArchived((on) => !on)}>
          {archived ? 'Hide archived' : 'Show archived'}
        </Button>
        <Button data-testid="open-chat-recycle-bin" className="ml-auto" onClick={() => setBinOpen(true)}>
          Recycle Bin{trashCount ? ` (${trashCount})` : ''}
        </Button>
      </div>

      {error && <p className="mb-4 text-[13px] text-state-danger">{error}</p>}

      {rows === null ? null : rows.length === 0 ? (
        <EmptyState
          title={query ? 'Nothing matches that' : 'No conversations yet'}
          body={query
            ? 'Search looks through everything that was actually said, not just the titles.'
            : 'Everything you say to Jarvis is kept here and stays searchable.'}
        />
      ) : (
        <div className="space-y-2" data-testid="history-list">
          {rows.map((conversation) => (
            <Card key={conversation.id} interactive data-testid="history-row"
                  className={conversation.pinned ? 'border-accent/25 bg-accent/[0.04]' : undefined}
                  onClick={() => setOpenId(conversation.id)}>
              <div className="flex items-start justify-between gap-4">
                <div className="min-w-0">
                  <p className="flex items-center gap-1.5 truncate text-[14px] text-ink">
                    {conversation.pinned && (
                      <PinIcon title="Pinned" className="h-3.5 w-3.5 shrink-0 text-accent" />
                    )}
                    <span className="truncate">{conversation.title || 'Untitled conversation'}</span>
                  </p>
                  <p className="mt-1 text-[12px] text-ink-faint">
                    {conversation.updatedAt.slice(0, 10)}
                    {typeof conversation.messageCount === 'number'
                      && ` · ${conversation.messageCount} message${conversation.messageCount === 1 ? '' : 's'}`}
                    {conversation.archived && ' · archived'}
                  </p>
                </div>
                {conversation.id === activeId && (
                  <span className="shrink-0 text-[12px] text-accent" data-testid="active-flag">
                    Open now
                  </span>
                )}
              </div>
            </Card>
          ))}
        </div>
      )}

      {openId && (
        <ConversationView
          id={openId}
          isActive={openId === activeId}
          onClose={() => setOpenId(null)}
          onChanged={async () => {
            setOpenId(null);
            await load();
          }}
          onResumed={(id) => {
            setOpenId(null);
            onResumeConversation?.(id);
            onNavigate?.('home');
          }}
        />
      )}

      {binOpen && (
        <RecycleBin
          onClose={() => setBinOpen(false)}
          onChanged={(count) => {
            setTrashCount(count);
            void load();
          }}
        />
      )}
    </>
  );
}

function ConversationView({ id, isActive, onClose, onChanged, onResumed }: {
  id: string;
  isActive: boolean;
  onClose: () => void;
  onChanged: () => Promise<void>;
  onResumed: (id: string) => void;
}) {
  const [conversation, setConversation] = useState<Conversation | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [title, setTitle] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api.conversations.open(id)
      .then((found) => {
        setConversation(found.conversation);
        setMessages(found.messages);
        setTitle(found.conversation.title);
      })
      .catch((err) => setError(err instanceof ApiRequestError ? err.message : 'Could not read it.'));
  }, [id]);

  async function run(action: () => Promise<unknown>, after: () => void | Promise<void>) {
    setBusy(true);
    setError(null);
    try {
      await action();
      await after();
    } catch (err) {
      setError(err instanceof ApiRequestError ? err.message : 'That did not work.');
      setBusy(false);
    }
  }

  return (
    <Modal
      open
      title={conversation?.title || 'Conversation'}
      onClose={onClose}
      footer={
        <>
          <Button tone="danger" data-testid="delete-conversation" disabled={busy}
                  onClick={() => run(() => api.conversations.remove(id), onChanged)}>
            Delete
          </Button>
          <Button data-testid="pin-conversation" disabled={busy}
                  onClick={() => run(
                    () => api.conversations.update(id, { pinned: !conversation?.pinned }),
                    onChanged)}>
            {conversation?.pinned ? 'Unpin' : 'Pin'}
          </Button>
          <Button data-testid="archive-conversation" disabled={busy}
                  onClick={() => run(
                    () => api.conversations.update(id, { archived: !conversation?.archived }),
                    onChanged)}>
            {conversation?.archived ? 'Unarchive' : 'Archive'}
          </Button>
          {/* Reading an old thread is the common thing; picking it back up is
              not, so it is its own action rather than a side effect of opening.
              No local run()/busy here — the actual activate-and-reload-the-
              transcript work happens in the caller (app/page.tsx's
              resumeConversation()), after this modal is already closing. */}
          {!isActive && (
            <Button tone="primary" data-testid="resume-conversation" disabled={busy}
                    onClick={() => onResumed(id)}>
              Pick up where this left off
            </Button>
          )}
        </>
      }
    >
      <Field label="Name" hint="Yours to change — Jarvis named it from what was said.">
        <input
          className={inputClass}
          data-testid="conversation-title"
          value={title}
          onChange={(event) => setTitle(event.target.value)}
          onBlur={() => {
            if (conversation && title.trim() && title !== conversation.title) {
              void api.conversations.update(id, { title: title.trim() }).catch(() => undefined);
            }
          }}
        />
      </Field>

      <div className="mt-3 max-h-[46vh] space-y-3 overflow-y-auto" data-testid="conversation-messages">
        {messages.length === 0 ? (
          <p className="text-[13px] text-ink-faint">Nothing was said in this one.</p>
        ) : (
          messages
            // Tool rows are the working-out, not the conversation. They are kept
            // in the record and read by the model; showing them here would bury
            // what was actually said.
            .filter((message) => message.role !== 'tool')
            .map((message, index) => (
              <MessageView
                key={index}
                turn={{
                  id: String(index),
                  role: message.role === 'assistant' ? 'assistant' : 'user',
                  text: message.text ?? '',
                } as Turn}
              />
            ))
        )}
      </div>

      {error && <p className="mt-2 text-[13px] text-state-danger">{error}</p>}
    </Modal>
  );
}

function RecycleBin({ onClose, onChanged }: {
  onClose: () => void;
  onChanged: (trashCount: number) => void;
}) {
  const [rows, setRows] = useState<Conversation[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const found = (await api.conversations.trash()).conversations;
      setRows(found);
      onChanged(found.length);
    } catch (err) {
      setError(err instanceof ApiRequestError ? err.message : 'Could not read the recycle bin.');
      setRows([]);
    }
    // onChanged is a fresh arrow from the caller every render; only re-run
    // this on a real reload, not because the parent re-rendered.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function restore(id: string) {
    setRows((current) => (current ?? []).filter((c) => c.id !== id));
    await api.conversations.restore(id).catch(() => void load());
    onChanged((rows ?? []).length - 1);
  }

  async function deleteForever(id: string) {
    setRows((current) => (current ?? []).filter((c) => c.id !== id));
    await api.conversations.purge(id).catch(() => void load());
    onChanged((rows ?? []).length - 1);
  }

  async function emptyBin() {
    setRows([]);
    await api.conversations.emptyTrash().catch(() => void load());
    onChanged(0);
  }

  return (
    <Modal
      open
      title="Recycle Bin"
      onClose={onClose}
      footer={
        <Button
          tone="danger"
          data-testid="empty-chat-recycle-bin"
          disabled={!(rows ?? []).length}
          onClick={() => void emptyBin()}
        >
          Empty recycle bin
        </Button>
      }
    >
      {error && <p className="mb-3 text-[13px] text-state-danger">{error}</p>}
      {rows === null ? (
        <p className="py-6 text-center text-[13px] text-ink-faint">Reading…</p>
      ) : rows.length === 0 ? (
        <p className="py-6 text-center text-[13px] text-ink-faint">
          The recycle bin is empty.
        </p>
      ) : (
        <ul className="space-y-2" data-testid="chat-recycle-bin-list">
          {rows.map((conversation) => (
            <li key={conversation.id}>
              <Card data-testid="chat-recycle-bin-row" className="flex items-center gap-3">
                <div className="min-w-0 flex-1">
                  <p className="truncate text-[14px] text-ink-muted">
                    {conversation.title || 'Untitled conversation'}
                  </p>
                  <p className="mt-0.5 truncate text-[12px] text-ink-faint">
                    {daysLeft(conversation.deletedAt)}
                  </p>
                </div>
                <Button data-testid="restore-conversation" onClick={() => void restore(conversation.id)}>
                  Restore
                </Button>
                <Button tone="danger" data-testid="delete-conversation-forever"
                        onClick={() => void deleteForever(conversation.id)}>
                  Delete forever
                </Button>
              </Card>
            </li>
          ))}
        </ul>
      )}
    </Modal>
  );
}

/** How long until the automatic 30-day sweep would take it anyway. */
function daysLeft(deletedAt: string | undefined): string {
  if (!deletedAt) return '';
  const deletedMs = new Date(deletedAt).getTime();
  if (Number.isNaN(deletedMs)) return '';
  const remaining = 30 - Math.floor((Date.now() - deletedMs) / 86_400_000);
  if (remaining <= 0) return 'Gone very soon';
  return `${remaining} day${remaining === 1 ? '' : 's'} left`;
}

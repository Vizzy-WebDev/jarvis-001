'use client';

import { useCallback, useEffect, useState } from 'react';

import { Message as MessageView, type Turn } from '@/components/conversation/Message';
import { Button } from '@/components/ui/Button';
import { Card } from '@/components/ui/Card';
import { EmptyState } from '@/components/ui/EmptyState';
import { Field, inputClass } from '@/components/ui/Field';
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
export function ChatHistoryScreen({ onNavigate }: { onNavigate?: (id: string) => void }) {
  const [rows, setRows] = useState<Conversation[] | null>(null);
  const [activeId, setActiveId] = useState('');
  const [query, setQuery] = useState('');
  const [archived, setArchived] = useState(false);
  const [openId, setOpenId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const found = await api.conversations.list({ query, includeArchived: archived });
      setRows(found.conversations);
      setActiveId(found.activeId);
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
                  onClick={() => setOpenId(conversation.id)}>
              <div className="flex items-start justify-between gap-4">
                <div className="min-w-0">
                  <p className="truncate text-[14px] text-ink">
                    {conversation.title || 'Untitled conversation'}
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
          onResumed={() => {
            setOpenId(null);
            onNavigate?.('home');
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
  onResumed: () => void;
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
          <Button data-testid="archive-conversation" disabled={busy}
                  onClick={() => run(
                    () => api.conversations.update(id, { archived: !conversation?.archived }),
                    onChanged)}>
            {conversation?.archived ? 'Unarchive' : 'Archive'}
          </Button>
          {/* Reading an old thread is the common thing; picking it back up is
              not, so it is its own action rather than a side effect of opening. */}
          {!isActive && (
            <Button tone="primary" data-testid="resume-conversation" disabled={busy}
                    onClick={() => run(() => api.conversations.activate(id), onResumed)}>
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

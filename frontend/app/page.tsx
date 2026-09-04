'use client';

import { useEffect, useState } from 'react';

import { api, ApiRequestError } from '@/lib/api';
import type { Conversation, Prefs } from '@/lib/api-types';

/**
 * A scaffold screen, not the real UI.
 *
 * Its job is to prove the whole front-end path works end to end — build, static
 * export, Tailwind, the typed client, and a real call against the backend — so
 * the screen-by-screen port of public/screens/ starts from something known to
 * work rather than from a blank page. The actual shell (transcript, orb, drawer,
 * voice engines) lands in F1/F2.
 */
export default function Home() {
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [prefs, setPrefs] = useState<Prefs | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;

    (async () => {
      try {
        const [list, loadedPrefs] = await Promise.all([api.conversations.list(), api.prefs.get()]);
        if (cancelled) return;
        setConversations(list.conversations);
        setPrefs(loadedPrefs);
      } catch (err) {
        if (cancelled) return;
        // The backend writes its error strings for the user to read directly,
        // so show what it said rather than a generic message.
        setError(err instanceof ApiRequestError ? err.message : 'Could not reach Jarvis.');
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();

    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <main className="mx-auto flex min-h-screen max-w-3xl flex-col gap-8 px-6 py-12">
      <header>
        <h1 className="text-2xl font-semibold tracking-tight">Jarvis</h1>
        <p className="mt-1 text-sm text-ink-muted">
          Next.js front end — scaffold. The real shell lands with the voice engines.
        </p>
      </header>

      {loading && <p className="text-sm text-ink-faint">Loading…</p>}

      {error && (
        <p className="rounded-md border border-surface-border bg-surface-raised px-4 py-3 text-sm text-ink">
          {error}
        </p>
      )}

      {!loading && !error && (
        <>
          <section>
            <h2 className="mb-3 text-sm font-medium uppercase tracking-wide text-ink-muted">
              Conversations
            </h2>
            {conversations.length === 0 ? (
              <p className="text-sm text-ink-faint">No conversations yet.</p>
            ) : (
              <ul className="divide-y divide-surface-border rounded-md border border-surface-border">
                {conversations.map((conversation) => (
                  <li key={conversation.id} className="flex items-center justify-between px-4 py-3">
                    <span className="truncate text-sm">{conversation.title}</span>
                    <span className="ml-4 shrink-0 text-xs text-ink-faint">
                      {conversation.messageCount ?? 0} messages
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </section>

          {prefs && (
            <section>
              <h2 className="mb-3 text-sm font-medium uppercase tracking-wide text-ink-muted">
                Preferences
              </h2>
              <dl className="grid grid-cols-2 gap-x-6 gap-y-2 text-sm">
                <dt className="text-ink-muted">Model selection</dt>
                <dd>{prefs.autoSelect ? 'Automatic' : 'Manual'}</dd>
                <dt className="text-ink-muted">Balance</dt>
                <dd>{prefs.balance}</dd>
                <dt className="text-ink-muted">Memory trust</dt>
                <dd>{prefs.memoryTrust}</dd>
                <dt className="text-ink-muted">Quiet hours</dt>
                <dd>
                  {prefs.quietHours.enabled
                    ? `${prefs.quietHours.start}–${prefs.quietHours.end}`
                    : 'Off'}
                </dd>
              </dl>
            </section>
          )}
        </>
      )}
    </main>
  );
}

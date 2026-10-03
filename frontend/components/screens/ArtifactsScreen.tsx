'use client';

/**
 * Artifacts — every file Jarvis has made, in one place.
 *
 * A list (newest first, searchable, filterable by kind, loaded a page at a time)
 * beside the selected file: shown with the same viewer the chat's cards open,
 * with Open in Chat, Download, Copy and Delete. The selection lives in the hash
 * (`#/artifacts/<id>`), so a refresh or Back keeps your place, and the chat's
 * "Show in Artifacts" link lands here on that file.
 *
 * Open in Chat goes to the conversation that MADE the file (recorded when it was
 * made), not the newest one, and lands on its card. When there is no such chat
 * any more — a background job made it, or the chat was deleted — the button says
 * why instead of doing nothing.
 */

import { useCallback, useEffect, useRef, useState } from 'react';

import { ArtifactActions, ArtifactFacts } from '@/components/artifacts/ArtifactPanel';
import { KindBadge } from '@/components/artifacts/ArtifactCard';
import { ArtifactViewer } from '@/components/artifacts/ArtifactViewer';
import { Button } from '@/components/ui/Button';
import { EmptyState } from '@/components/ui/EmptyState';
import { inputClass } from '@/components/ui/Field';
import { Modal } from '@/components/ui/Modal';
import { api, ApiRequestError } from '@/lib/api';
import type { Artifact, ArtifactKind } from '@/lib/api-types';
import { formatSize, KIND_LABEL } from '@/lib/artifacts';

const PAGE = 30;

function selectedFromHash(): string | null {
  const rest = window.location.hash.replace(/^#\/?/, '').split('/');
  return rest[0] === 'artifacts' && rest[1] ? decodeURIComponent(rest[1]) : null;
}

function when(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  const today = new Date();
  return date.toDateString() === today.toDateString()
    ? date.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })
    : date.toLocaleDateString([], { day: 'numeric', month: 'short', year: date.getFullYear() === today.getFullYear() ? undefined : 'numeric' });
}

/** Why Open in Chat cannot go anywhere, or null when it can. */
function whyNoChat(artifact: Artifact): string | null {
  const chat = artifact.conversation;
  if (!chat) return 'Made by a background job, not in a chat.';
  if (chat.state === 'trashed') return 'The chat it came from is in the recycle bin (Chat History).';
  if (chat.state === 'gone') return 'The chat it came from was deleted.';
  return null;
}

export function ArtifactsScreen({ onNavigate, onOpenInChat }: {
  onNavigate?: (id: string) => void;
  /** Reopen that conversation and land on this file's card in it. */
  onOpenInChat?: (conversationId: string, artifactId: string) => void;
}) {
  const [items, setItems] = useState<Artifact[] | null>(null);
  const [next, setNext] = useState<string | undefined>(undefined);
  const [loadingMore, setLoadingMore] = useState(false);
  const [query, setQuery] = useState('');
  const [kind, setKind] = useState<ArtifactKind | ''>('');
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [selected, setSelected] = useState<Artifact | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [confirming, setConfirming] = useState<Artifact | null>(null);
  const [deleting, setDeleting] = useState(false);
  const request = useRef(0);

  const load = useCallback(async (q: string, k: string) => {
    const mine = ++request.current;
    try {
      const page = await api.artifacts.list({ q, kind: k, limit: PAGE });
      if (mine !== request.current) return;
      setItems(page.artifacts);
      setNext(page.nextBefore);
      setProblem(null);
    } catch (err) {
      if (mine === request.current) setProblem(err instanceof Error ? err.message : 'The list could not be loaded.');
    }
  }, []);

  // Search waits for a pause in typing; the kind filter applies at once.
  useEffect(() => {
    const timer = setTimeout(() => void load(query.trim(), kind), query ? 250 : 0);
    return () => clearTimeout(timer);
  }, [query, kind, load]);

  useEffect(() => {
    const read = () => setSelectedId(selectedFromHash());
    read();
    window.addEventListener('hashchange', read);
    return () => window.removeEventListener('hashchange', read);
  }, []);

  // The selected file's full details — from the list when it is there, else
  // fetched (a link straight to one not on the first page, or filtered out).
  useEffect(() => {
    if (!selectedId) { setSelected(null); return; }
    const listed = items?.find((a) => a.id === selectedId);
    if (listed) { setSelected(listed); return; }
    let cancelled = false;
    api.artifacts.info(selectedId)
      .then((found) => { if (!cancelled) setSelected(found.artifact); })
      .catch((err) => {
        if (cancelled) return;
        setSelected(null);
        if (err instanceof ApiRequestError && err.status === 404) {
          setProblem('That file no longer exists — it may have been deleted.');
        }
      });
    return () => { cancelled = true; };
  }, [selectedId, items]);

  const select = (id: string | null) => {
    window.location.hash = id ? `#/artifacts/${encodeURIComponent(id)}` : '#/artifacts';
  };

  const loadMore = async () => {
    if (!next) return;
    setLoadingMore(true);
    try {
      const page = await api.artifacts.list({ q: query.trim(), kind, before: next, limit: PAGE });
      setItems((current) => [...(current ?? []), ...page.artifacts]);
      setNext(page.nextBefore);
    } catch (err) {
      setProblem(err instanceof Error ? err.message : 'More could not be loaded.');
    } finally {
      setLoadingMore(false);
    }
  };

  const remove = async (artifact: Artifact) => {
    setDeleting(true);
    try {
      await api.artifacts.remove(artifact.id);
      setItems((current) => (current ?? []).filter((a) => a.id !== artifact.id));
      setConfirming(null);
      if (selectedId === artifact.id) select(null);
    } catch (err) {
      setProblem(err instanceof Error ? err.message : 'It could not be deleted.');
      setConfirming(null);
    } finally {
      setDeleting(false);
    }
  };

  const filtering = Boolean(query.trim() || kind);
  const empty = items !== null && items.length === 0;

  if (empty && !filtering && !selectedId) {
    return (
      <EmptyState
        title="Nothing here yet."
        body="When you ask Jarvis to make a document, spreadsheet, presentation, PDF, web page, diagram, code or any other file, it is kept here — ready to open, download, or take you back to the chat it came from."
        action={<Button tone="primary" onClick={() => onNavigate?.('home')}>Ask Jarvis for one</Button>}
      />
    );
  }

  return (
    <div className="flex flex-col gap-4 lg:flex-row lg:items-start" data-testid="artifacts-screen">
      {/* The list. On a narrow screen it gives way to the open file. */}
      <aside className={`${selected ? 'hidden lg:flex' : 'flex'} w-full shrink-0 flex-col gap-2.5 lg:sticky lg:top-0 lg:w-[340px]`}>
        <input type="search" value={query} onChange={(e) => setQuery(e.target.value)}
               placeholder="Search by name or title" aria-label="Search artifacts"
               data-testid="artifacts-search" className={inputClass} />
        <select value={kind} onChange={(e) => setKind(e.target.value as ArtifactKind | '')}
                aria-label="Kind of file" data-testid="artifacts-kind" className={inputClass}>
          <option value="">All kinds</option>
          {(Object.keys(KIND_LABEL) as ArtifactKind[]).map((k) => (
            <option key={k} value={k}>{KIND_LABEL[k]}</option>
          ))}
        </select>
        {problem && <p className="text-[13px] text-state-warn" role="alert">{problem}</p>}
        {items === null && <p className="py-6 text-center text-[13px] text-ink-faint">Loading…</p>}
        {empty && filtering && (
          <p className="py-6 text-center text-[13px] text-ink-muted" data-testid="artifacts-none-match">
            Nothing matches that. Try another word or kind.
          </p>
        )}
        <ul className="flex flex-col gap-1.5" data-testid="artifacts-list">
          {(items ?? []).map((artifact) => (
            <li key={artifact.id}>
              <button type="button" onClick={() => select(artifact.id)}
                      data-testid="artifact-row" data-artifact-id={artifact.id}
                      aria-current={artifact.id === selectedId ? 'true' : undefined}
                      className={`flex w-full items-center gap-3 rounded-lg border px-3 py-2.5 text-left transition-colors
                        ${artifact.id === selectedId
                          ? 'border-accent/40 bg-accent/[0.08]'
                          : 'border-surface-border bg-surface-raised/60 hover:border-surface-border-strong'}`}>
                <KindBadge kind={artifact.kind} />
                <span className="min-w-0 flex-1">
                  <span className="block truncate text-[13.5px] font-medium text-ink">{artifact.title}</span>
                  <span className="block truncate text-[11.5px] text-ink-muted">
                    {KIND_LABEL[artifact.kind]} · {formatSize(artifact.size)} · {when(artifact.createdAt)}
                  </span>
                  <span className="block truncate text-[11.5px] text-ink-faint">
                    {artifact.conversation?.title
                      ? `From “${artifact.conversation.title}”`
                      : artifact.conversation ? 'From a deleted chat' : 'From a background job'}
                  </span>
                </span>
              </button>
            </li>
          ))}
        </ul>
        {next && (
          <Button onClick={loadMore} disabled={loadingMore} data-testid="artifacts-more" className="self-center">
            {loadingMore ? 'Loading…' : 'Load more'}
          </Button>
        )}
      </aside>

      {/* The open file. */}
      <section className={`${selected ? 'flex' : 'hidden lg:flex'} min-w-0 flex-1 flex-col rounded-xl border border-surface-border bg-surface-raised/60 p-4`}
               data-testid="artifact-detail">
        {selected ? (
          <>
            <button type="button" onClick={() => select(null)}
                    className="mb-2 self-start text-[12.5px] text-ink-muted hover:text-ink lg:hidden">
              ← All artifacts
            </button>
            <h2 className="text-[17px] font-medium text-ink" data-testid="artifact-title">{selected.title}</h2>
            <div className="mt-1"><ArtifactFacts artifact={selected} /></div>
            <div className="mb-4 flex flex-wrap items-center gap-2">
              <OpenInChat artifact={selected} onOpen={onOpenInChat} />
              <ArtifactActions artifact={selected} showInArtifacts={false} quietDownload />
              <Button tone="danger" data-testid="artifact-delete" onClick={() => setConfirming(selected)}
                      className="ml-auto">
                Delete
              </Button>
            </div>
            <ArtifactViewer key={selected.id} artifact={selected} />
          </>
        ) : (
          <p className="py-16 text-center text-[13px] text-ink-faint">Pick a file to open it here.</p>
        )}
      </section>

      <Modal open={confirming !== null} title="Delete this file?" onClose={() => !deleting && setConfirming(null)}
             footer={confirming && (
               <>
                 <Button tone="danger" data-testid="artifact-delete-confirm" disabled={deleting}
                         onClick={() => void remove(confirming)}>
                   {deleting ? 'Deleting…' : 'Delete it'}
                 </Button>
                 <Button onClick={() => setConfirming(null)} disabled={deleting}>Keep it</Button>
               </>
             )}>
        {confirming && (
          <p className="text-[14px] leading-relaxed text-ink/90">
            “{confirming.title}” will be removed for good. It can&apos;t be brought back. The chat it came
            from stays, and its card there will say the file was deleted.
          </p>
        )}
      </Modal>
    </div>
  );
}

function OpenInChat({ artifact, onOpen }: {
  artifact: Artifact;
  onOpen?: (conversationId: string, artifactId: string) => void;
}) {
  const why = whyNoChat(artifact);
  if (why || !artifact.conversation) {
    return (
      <span className="flex items-center gap-2">
        <Button disabled data-testid="artifact-open-chat">Open in Chat</Button>
        <span className="text-[12px] text-ink-faint" data-testid="artifact-no-chat">{why}</span>
      </span>
    );
  }
  const chat = artifact.conversation;
  return (
    <Button tone="primary" data-testid="artifact-open-chat" title={chat.title ? `Open “${chat.title}”` : undefined}
            onClick={() => onOpen?.(chat.id, artifact.id)}>
      Open in Chat
    </Button>
  );
}

'use client';

/**
 * A saved artifact as it appears in the chat: what it is, and a way to open it.
 *
 * Clicking the card opens the viewer over the page (like the side panel Claude
 * uses for its own artifacts); Download is always there beside it. The card
 * checks, once, that the file still exists — so a card in an old conversation
 * whose file was deleted on the Artifacts page says so instead of failing when
 * clicked.
 */

import { useEffect, useState } from 'react';

import { api, ApiRequestError } from '@/lib/api';
import { formatSize, KIND_LABEL, type FileCard } from '@/lib/artifacts';
import type { ArtifactKind } from '@/lib/api-types';

import { ArtifactPanel } from './ArtifactPanel';

export function ArtifactCard({ card }: { card: FileCard & { artifactId: string } }) {
  const [open, setOpen] = useState(false);
  const [gone, setGone] = useState(false);
  const [title, setTitle] = useState(card.title || card.name || 'File');

  useEffect(() => {
    let cancelled = false;
    api.artifacts.info(card.artifactId)
      .then((found) => {
        if (cancelled) return;
        setGone(!found.exists);
        setTitle(found.artifact.title);
      })
      .catch((err) => {
        if (!cancelled && err instanceof ApiRequestError && err.status === 404) setGone(true);
      });
    return () => { cancelled = true; };
  }, [card.artifactId]);

  const kind = (card.artifactKind as ArtifactKind | undefined) ?? 'other';
  const label = KIND_LABEL[kind] ?? 'File';
  const facts = [label, typeof card.size === 'number' ? formatSize(card.size) : null].filter(Boolean).join(' · ');

  if (gone) {
    return (
      <div data-testid="file-tile" data-artifact-id={card.artifactId} data-state="deleted"
           className="mb-2 w-[290px] max-w-full rounded-lg border border-dashed border-surface-border px-3 py-2.5 text-[12.5px] text-ink-faint">
        <span className="line-through">{title}</span> — this file was deleted.
      </div>
    );
  }
  return (
    <div data-testid="file-tile" data-artifact-id={card.artifactId}
         className="artifact-card mb-2 flex w-[290px] max-w-full items-center gap-3 rounded-lg border border-surface-border bg-white/[0.03] px-3 py-2.5 transition-colors hover:border-accent/30">
      <button type="button" onClick={() => setOpen(true)} data-testid="artifact-open" aria-label={`Open ${title}`}
              className="shrink-0">
        <KindBadge kind={kind} />
      </button>
      <span className="min-w-0 flex-1">
        <button type="button" onClick={() => setOpen(true)} title={card.name}
                className="block w-full truncate text-left text-[13.5px] font-medium text-accent hover:underline">
          {title}
        </button>
        <span className="flex items-center gap-2 text-[11.5px] text-ink-muted">
          <span className="min-w-0 truncate">{facts}</span>
          <a href={card.url} download={card.name || true} data-testid="artifact-card-download"
             className="ml-auto shrink-0 text-ink-muted underline-offset-2 hover:text-ink hover:underline">
            Download
          </a>
        </span>
      </span>
      <ArtifactPanel artifactId={card.artifactId} open={open} onClose={() => setOpen(false)} />
    </div>
  );
}

const BADGE: Partial<Record<ArtifactKind, string>> = {
  document: 'DOC', spreadsheet: 'XLS', presentation: 'PPT', pdf: 'PDF', markdown: 'MD',
  web: 'WEB', image: 'IMG', audio: 'AUD', data: 'DATA', code: '</>', text: 'TXT',
};

export function KindBadge({ kind }: { kind: ArtifactKind }) {
  return (
    <span aria-hidden
          className="flex h-9 w-9 shrink-0 items-center justify-center rounded-md border border-accent/25 bg-accent/10 text-[10px] font-semibold tracking-wide text-accent">
      {BADGE[kind] ?? 'FILE'}
    </span>
  );
}

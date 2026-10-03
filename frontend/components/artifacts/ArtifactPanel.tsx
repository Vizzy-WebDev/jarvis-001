'use client';

/**
 * An artifact opened over whatever screen asked for it — the chat's file card
 * uses this; the Artifacts page shows the same viewer in its own detail pane.
 *
 * **Rendered into `document.body` through a portal, and that is not optional.**
 * The conversation panel has a `backdrop-filter`, and under such an ancestor
 * `position: fixed` stops meaning "the viewport" — opened from a card inside the
 * panel, the overlay would be placed and clipped inside the panel instead
 * (the same trap `ui/Popover.tsx` documents).
 */

import { useEffect, useState } from 'react';
import { createPortal } from 'react-dom';

import { Button } from '@/components/ui/Button';
import { Modal } from '@/components/ui/Modal';
import { api, ApiRequestError } from '@/lib/api';
import type { Artifact } from '@/lib/api-types';
import { formatSize, KIND_LABEL } from '@/lib/artifacts';

import { ArtifactViewer } from './ArtifactViewer';

export function ArtifactPanel({ artifactId, open, onClose }: {
  artifactId: string;
  open: boolean;
  onClose: () => void;
}) {
  const [artifact, setArtifact] = useState<Artifact | null>(null);
  const [problem, setProblem] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setProblem(null);
    api.artifacts.info(artifactId)
      .then((found) => {
        if (cancelled) return;
        if (!found.exists) setProblem('This file was deleted, so there is nothing to show.');
        setArtifact(found.artifact);
      })
      .catch((err) => {
        if (cancelled) return;
        setProblem(err instanceof ApiRequestError && err.status === 404
          ? 'This file was deleted, so there is nothing to show.'
          : err instanceof Error ? err.message : 'It could not be opened.');
      });
    return () => { cancelled = true; };
  }, [artifactId, open]);

  if (!open || typeof document === 'undefined') return null;
  return createPortal(
    <Modal open={open} title={artifact?.title ?? 'File'} onClose={onClose} size="wide"
           footer={artifact && !problem ? <ArtifactActions artifact={artifact} onLeave={onClose} /> : undefined}>
      <div data-testid="artifact-panel" className="flex min-h-[40vh] flex-col">
        {artifact && <ArtifactFacts artifact={artifact} />}
        {problem
          ? <p className="text-[13px] text-ink-muted" data-testid="artifact-missing">{problem}</p>
          : artifact ? <ArtifactViewer artifact={artifact} /> : <p className="text-[13px] text-ink-muted">Opening…</p>}
      </div>
    </Modal>,
    document.body,
  );
}

export function ArtifactFacts({ artifact }: { artifact: Artifact }) {
  const when = new Date(artifact.createdAt);
  return (
    <p className="mb-3 text-[12.5px] text-ink-muted" data-testid="artifact-facts">
      {KIND_LABEL[artifact.kind]} · {artifact.name} · {formatSize(artifact.size)} ·{' '}
      {Number.isNaN(when.getTime()) ? artifact.createdAt : when.toLocaleString()}
    </p>
  );
}

/** Download, Copy (when it is text), and a way to the Artifacts page. */
export function ArtifactActions({ artifact, onLeave, showInArtifacts = true, quietDownload = false }: {
  artifact: Artifact;
  onLeave?: () => void;
  showInArtifacts?: boolean;
  /** When something else on screen is the primary action (Open in Chat). */
  quietDownload?: boolean;
}) {
  const [copied, setCopied] = useState<'idle' | 'done' | 'failed'>('idle');
  const textual = ['markdown', 'code', 'text', 'data', 'web'].includes(artifact.kind)
    || artifact.name.toLowerCase().endsWith('.svg');

  const copy = async () => {
    try {
      const text = await (await fetch(api.artifacts.fileUrl(artifact.id))).text();
      await navigator.clipboard.writeText(text);
      setCopied('done');
    } catch {
      setCopied('failed');
    }
    setTimeout(() => setCopied('idle'), 1800);
  };

  return (
    <>
      <a href={api.artifacts.fileUrl(artifact.id)} download={artifact.name} data-testid="artifact-download"
         className={`inline-flex items-center gap-2 rounded-pill border px-3.5 py-1.5 text-[13px] font-medium ${quietDownload
           ? 'border-surface-border bg-white/[0.04] text-ink-muted hover:bg-white/[0.08] hover:text-ink'
           : 'border-accent/25 bg-accent/15 text-accent hover:bg-accent/25'}`}>
        Download
      </a>
      {textual && (
        <Button data-testid="artifact-copy" onClick={copy}>
          {copied === 'done' ? 'Copied' : copied === 'failed' ? 'Couldn’t copy' : 'Copy'}
        </Button>
      )}
      {showInArtifacts && (
        <a href={`#/artifacts/${encodeURIComponent(artifact.id)}`} onClick={() => onLeave?.()}
           data-testid="artifact-show-in-page"
           className="ml-auto text-[13px] text-ink-muted underline-offset-2 hover:text-ink hover:underline">
          Show in Artifacts
        </a>
      )}
    </>
  );
}

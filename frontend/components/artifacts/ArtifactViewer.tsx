'use client';

/**
 * Shows one artifact inside the app, whatever it is — the one viewer the chat's
 * file card and the Artifacts page both use.
 *
 * **Nothing is ever served inline.** The file route always answers as a download
 * (routes/artifacts.py, and root CLAUDE.md's rule about content a model wrote), so
 * this component FETCHES the bytes and shows them itself:
 *
 * - text, code, data, Markdown: as text nodes (Markdown through `markdown.tsx`,
 *   which never produces raw HTML), so `<script>` in a file is just characters;
 * - an image, including an SVG: through `<img>` from a blob URL — a browser never
 *   runs script inside an `<img>`;
 * - a web page: in `<iframe sandbox="allow-scripts">` with no `allow-same-origin`,
 *   so it gets an opaque origin, can't read the app or its storage, can't submit
 *   forms, open windows or navigate the app; a strict content policy is put
 *   FIRST in its document so it cannot load or send anything; and
 *   `request_guard.py` refuses anything it still manages to aim at Jarvis's API
 *   (a frame can always navigate itself);
 * - a PDF: the browser's own PDF viewer, from a blob URL;
 * - Word, Excel, PowerPoint: their text and cells from `/preview` (JSON);
 * - audio: `<audio>`.
 */

import { useEffect, useMemo, useState } from 'react';

import { api, ApiRequestError } from '@/lib/api';
import type { Artifact, ArtifactPreview } from '@/lib/api-types';

import { formatSize, isTextKind, KIND_LABEL, lockedDocument, parseDelimited } from '@/lib/artifacts';

import { Markdown } from './markdown';

export { formatSize, KIND_LABEL };

/** Beyond this, only the start of a text file is shown, and the viewer says so. */
const MAX_TEXT_BYTES = 2_000_000;
const MAX_TABLE_ROWS = 1000;

type Loaded =
  | { state: 'loading' }
  | { state: 'missing' }
  | { state: 'error'; message: string }
  | { state: 'text'; text: string; cut: boolean }
  | { state: 'blob'; url: string }
  | { state: 'preview'; preview: ArtifactPreview }
  | { state: 'none' };

export function ArtifactViewer({ artifact, onText }: {
  artifact: Artifact;
  /** Handed the file's text once loaded, for a Copy button beside the viewer. */
  onText?: (text: string | null) => void;
}) {
  const [loaded, setLoaded] = useState<Loaded>({ state: 'loading' });
  const [showSource, setShowSource] = useState(false);
  const { id, kind, name } = artifact;

  useEffect(() => {
    let cancelled = false;
    let objectUrl: string | null = null;
    setLoaded({ state: 'loading' });
    setShowSource(false);
    onText?.(null);

    const fail = (err: unknown) => {
      if (cancelled) return;
      if (err instanceof ApiRequestError && err.status === 404) setLoaded({ state: 'missing' });
      else setLoaded({ state: 'error', message: err instanceof Error ? err.message : 'It could not be opened.' });
    };

    (async () => {
      if (kind === 'document' || kind === 'spreadsheet' || kind === 'presentation') {
        const preview = await api.artifacts.preview(id);
        if (!cancelled) setLoaded({ state: 'preview', preview });
        return;
      }
      if (kind === 'audio') { setLoaded({ state: 'none' }); return; }
      const binary = kind === 'pdf' || (kind === 'image' && !name.toLowerCase().endsWith('.svg'));
      if (!binary && !isTextKind(kind, name)) { setLoaded({ state: 'none' }); return; }

      const response = await fetch(api.artifacts.fileUrl(id));
      if (response.status === 404) throw new ApiRequestError(404, 'Not found.');
      if (!response.ok) throw new ApiRequestError(response.status, `It could not be opened (${response.status}).`);
      const blob = await response.blob();
      if (cancelled) return;
      if (binary) {
        objectUrl = URL.createObjectURL(new Blob([blob], { type: kind === 'pdf' ? 'application/pdf' : artifact.mimeType }));
        setLoaded({ state: 'blob', url: objectUrl });
        return;
      }
      const cut = blob.size > MAX_TEXT_BYTES;
      const text = await blob.slice(0, MAX_TEXT_BYTES).text();
      if (cancelled) return;
      if (kind === 'image') {
        // An SVG: shown as an image (no script can run in an <img>), its text kept for Copy.
        objectUrl = URL.createObjectURL(new Blob([text], { type: 'image/svg+xml' }));
        setLoaded({ state: 'blob', url: objectUrl });
      } else {
        setLoaded({ state: 'text', text, cut });
      }
      onText?.(text);
    })().catch(fail);

    return () => {
      cancelled = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id, kind, name]);

  const lower = name.toLowerCase();
  const sourceToggle = loaded.state === 'text' && (kind === 'markdown' || kind === 'web' || lower.endsWith('.csv') || lower.endsWith('.tsv'));

  return (
    <div className="flex min-h-0 flex-1 flex-col" data-testid="artifact-viewer" data-kind={kind}>
      {sourceToggle && (
        <div className="mb-2 flex justify-end">
          <button type="button" data-testid="artifact-source-toggle"
                  onClick={() => setShowSource((v) => !v)}
                  className="rounded-pill border border-surface-border px-2.5 py-1 text-[12px] text-ink-muted hover:text-ink">
            {showSource ? 'Show it rendered' : 'Show the source'}
          </button>
        </div>
      )}
      <Body artifact={artifact} loaded={loaded} showSource={showSource} />
    </div>
  );
}

function Body({ artifact, loaded, showSource }: { artifact: Artifact; loaded: Loaded; showSource: boolean }) {
  const { kind, name } = artifact;
  const lower = name.toLowerCase();

  if (loaded.state === 'loading') return <Note>Opening {name}…</Note>;
  if (loaded.state === 'missing') {
    return <Note testId="artifact-missing">This file was deleted, so there is nothing to show.</Note>;
  }
  if (loaded.state === 'error') return <Note>{loaded.message}</Note>;

  if (kind === 'audio') {
    return <audio controls preload="metadata" src={api.artifacts.fileUrl(artifact.id)} className="w-full" data-testid="artifact-audio" />;
  }
  if (loaded.state === 'none') {
    return (
      <Note testId="artifact-no-preview">
        There is no preview for this kind of file inside Jarvis. Download it to open it with the app that
        handles {lower.includes('.') ? lower.slice(lower.lastIndexOf('.')) : 'it'} files.
      </Note>
    );
  }
  if (loaded.state === 'blob') {
    if (kind === 'pdf') {
      return <iframe title={name} src={loaded.url} data-testid="artifact-pdf"
                     className="min-h-[70vh] w-full flex-1 rounded-lg border border-surface-border bg-white" />;
    }
    return (
      <div className="flex justify-center rounded-lg border border-surface-border bg-[#f7f7f5] p-4">
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img src={loaded.url} alt={artifact.title} data-testid="artifact-image" className="max-h-[70vh] max-w-full object-contain" />
      </div>
    );
  }
  if (loaded.state === 'preview') return <PreviewBody preview={loaded.preview} />;

  const { text, cut } = loaded;
  const cutNote = cut ? <Note>This file is large; the first 2 MB are shown. Download it for the rest.</Note> : null;
  if (kind === 'web' && !showSource) {
    return (
      <>
        <iframe title={artifact.title} sandbox="allow-scripts" srcDoc={lockedDocument(text)}
                referrerPolicy="no-referrer" data-testid="artifact-frame"
                className="min-h-[70vh] w-full flex-1 rounded-lg border border-surface-border bg-white" />
        <p className="mt-2 text-[12px] text-ink-faint">
          Running in a sealed frame: it can&apos;t reach the internet, your files or the rest of Jarvis.
        </p>
      </>
    );
  }
  if (kind === 'markdown' && !showSource) {
    return <div className="rounded-lg border border-surface-border bg-white/[0.02] px-5 py-4">{cutNote}<Markdown source={text} /></div>;
  }
  if ((lower.endsWith('.csv') || lower.endsWith('.tsv')) && !showSource) {
    return <>{cutNote}<CsvTable text={text} tab={lower.endsWith('.tsv')} /></>;
  }
  return <>{cutNote}<CodeBlock text={prettyJson(text, lower)} /></>;
}

function prettyJson(text: string, lower: string): string {
  if (!lower.endsWith('.json')) return text;
  try {
    return JSON.stringify(JSON.parse(text), null, 2);
  } catch {
    return text;
  }
}

function CodeBlock({ text }: { text: string }) {
  const lines = text.split('\n');
  return (
    <pre data-testid="artifact-text"
         className="scroll-quiet overflow-auto rounded-lg border border-surface-border bg-black/30 py-3 font-mono text-[12.5px] leading-[1.6] text-ink">
      <code className="grid" style={{ gridTemplateColumns: 'auto 1fr' }}>
        {lines.map((line, index) => (
          <span key={index} className="contents">
            <span className="select-none pl-3 pr-4 text-right text-ink-faint/70">{index + 1}</span>
            <span className="whitespace-pre-wrap break-words pr-4">{line || ' '}</span>
          </span>
        ))}
      </code>
    </pre>
  );
}

function CsvTable({ text, tab }: { text: string; tab: boolean }) {
  const rows = useMemo(() => parseDelimited(text, tab ? '\t' : ','), [text, tab]);
  return <Grid rows={rows} testId="artifact-table" />;
}

function Grid({ rows, testId }: { rows: string[][]; testId: string }) {
  if (!rows.length) return <Note>It&apos;s empty.</Note>;
  const [head, ...body] = rows;
  const width = Math.max(...rows.map((r) => r.length));
  const shown = body.slice(0, MAX_TABLE_ROWS);
  return (
    <div className="scroll-quiet overflow-auto rounded-lg border border-surface-border" data-testid={testId}>
      <table className="w-full border-collapse text-[13px]">
        <thead className="sticky top-0 bg-surface-raised">
          <tr>{Array.from({ length: width }, (_, j) => (
            <th key={j} className="border-b border-surface-border px-3 py-2 text-left font-semibold text-ink">{head?.[j] ?? ''}</th>
          ))}</tr>
        </thead>
        <tbody>
          {shown.map((row, r) => (
            <tr key={r} className="odd:bg-white/[0.02]">
              {Array.from({ length: width }, (_, j) => (
                <td key={j} className="border-b border-surface-border/60 px-3 py-1.5 align-top text-ink/90">{row[j] ?? ''}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      {body.length > MAX_TABLE_ROWS && (
        <p className="px-3 py-2 text-[12px] text-ink-faint">
          Showing the first {MAX_TABLE_ROWS} of {body.length} rows. Download it for all of them.
        </p>
      )}
    </div>
  );
}

function PreviewBody({ preview }: { preview: ArtifactPreview }) {
  const [sheet, setSheet] = useState(0);
  if (preview.format === 'markdown') {
    return (
      <div className="rounded-lg border border-surface-border bg-white/[0.02] px-5 py-4" data-testid="artifact-preview">
        {preview.note && <Note>{preview.note}</Note>}
        <Markdown source={preview.markdown || '_This document has no text._'} />
      </div>
    );
  }
  const current = preview.sheets[sheet] ?? preview.sheets[0];
  return (
    <div data-testid="artifact-preview">
      {preview.note && <Note>{preview.note}</Note>}
      {preview.sheets.length > 1 && (
        <div className="mb-2 flex flex-wrap gap-1.5">
          {preview.sheets.map((s, index) => (
            <button key={s.name} type="button" onClick={() => setSheet(index)}
                    className={`rounded-pill border px-2.5 py-1 text-[12px] ${index === sheet ? 'border-accent/40 bg-accent/15 text-accent' : 'border-surface-border text-ink-muted'}`}>
              {s.name}
            </button>
          ))}
        </div>
      )}
      {current ? <Grid rows={current.rows} testId="artifact-table" /> : <Note>This spreadsheet has no sheets.</Note>}
    </div>
  );
}

function Note({ children, testId }: { children: React.ReactNode; testId?: string }) {
  return <p className="mb-2 text-[13px] leading-relaxed text-ink-muted" data-testid={testId}>{children}</p>;
}

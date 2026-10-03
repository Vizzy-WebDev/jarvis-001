'use client';

import { useEffect, useMemo, useRef, useState } from 'react';

import { Button } from '@/components/ui/Button';
import { Field, inputClass } from '@/components/ui/Field';
import { Modal } from '@/components/ui/Modal';
import { api, ApiRequestError } from '@/lib/api';
import type { ContentMeta } from '@/lib/api-types';

/** Types whose content is ONE file: one file makes one item. A carousel is many
 *  slides making one item, and text types have no file, so they use the single form. */
export function oneFileTypes(meta: ContentMeta): string[] {
  return Object.entries(meta.types)
    .filter(([, t]) => t.media === 'primary' && ['video', 'image', 'audio'].includes(t.render))
    .map(([id]) => id);
}

const ACCEPT: Record<string, string> = { video: 'video/*', image: 'image/*', audio: 'audio/*' };

type Row = {
  key: string;
  file: File;
  name: string;
  state: 'waiting' | 'adding' | 'added' | 'failed';
  error?: string;
  /** Not the kind of file this type is made of: never sent. */
  wrong?: boolean;
};

let counter = 0;

/** "rain_on_a_tin-roof.final.mp4" → "rain on a tin roof.final": the name only the person sees. */
function nameFrom(file: File): string {
  const bare = file.name.replace(/\.[^.]+$/, '').replace(/[_-]+/g, ' ').replace(/\s+/g, ' ').trim();
  return (bare || file.name).slice(0, 200);
}

/**
 * Several files at once, each becoming its OWN item — ten videos are ten
 * videos, not one thing. Every one goes through the same door as anything
 * else handed in (`POST /api/content-items`), one after another, so a file the
 * server refuses is reported by name while the others are kept.
 */
export function AddSeveralDialog({
  meta, niche: startNiche, onAdded, onClose,
}: {
  meta: ContentMeta;
  niche: string;
  onAdded: () => void;
  onClose: () => void;
}) {
  const types = useMemo(() => oneFileTypes(meta), [meta]);
  const [type, setType] = useState(types[0] ?? 'video');
  const [niche, setNiche] = useState(startNiche);
  const [rows, setRows] = useState<Row[]>([]);
  const [platforms, setPlatforms] = useState<string[]>([]);
  const [running, setRunning] = useState(false);
  const [finished, setFinished] = useState(false);
  const [over, setOver] = useState(false);
  const picker = useRef<HTMLInputElement>(null);
  const list = useRef<HTMLUListElement>(null);
  const info = meta.types[type];
  const accepts = useMemo(() => Object.entries(meta.platforms).filter(([, p]) => p.accepts.includes(type)),
    [meta, type]);
  const done = rows.filter((r) => r.state === 'added').length;
  const failed = rows.filter((r) => r.state === 'failed');
  const todo = rows.filter((r) => r.state !== 'added');

  function pick(files: FileList | File[] | null) {
    if (!files) return;
    const wanted = ACCEPT[info?.render ?? ''] ?? '';
    const kind = wanted.split('/')[0];
    const fresh = [...files].map((file) => ({
      key: `several${(counter += 1)}`, file, name: nameFrom(file), state: 'waiting' as const,
      ...(kind && file.type && !file.type.startsWith(`${kind}/`)
        ? { state: 'failed' as const, error: notA(kind), wrong: true }
        : {}),
    }));
    setRows((current) => [...current, ...fresh]);
    setFinished(false);
  }

  function changeType(next: string) {
    setType(next);
    setPlatforms((current) => current.filter((p) => meta.platforms[p]?.accepts.includes(next)));
    // A file of the wrong kind for the new type is marked, not silently dropped.
    const kind = (ACCEPT[meta.types[next]?.render ?? ''] ?? '').split('/')[0];
    setRows((current) => current.map((r) => {
      if (r.state === 'added') return r;
      const wrong = kind && r.file.type && !r.file.type.startsWith(`${kind}/`);
      return wrong ? { ...r, state: 'failed', error: notA(kind), wrong: true }
        : { ...r, state: 'waiting', error: undefined, wrong: false };
    }));
  }

  async function addAll(readyToPost: boolean) {
    setRunning(true);
    for (const row of rows) {
      if (row.state === 'added') continue;
      if (row.wrong) continue;
      if (!row.name.trim()) {
        setRows((current) => current.map((r) => r.key === row.key
          ? { ...r, state: 'failed', error: 'Give it a name.' } : r));
        continue;
      }
      setRows((current) => current.map((r) => r.key === row.key ? { ...r, state: 'adding', error: undefined } : r));
      try {
        await api.content.create({
          name: row.name.trim(), contentType: type, niche: niche.trim(), producer: 'you',
          media: [{ file: row.key, role: 'primary', order: 0 }],
          platforms: platforms.map((platform) => ({ platform })), readyToPost,
        }, [{ key: row.key, file: row.file }]);
        setRows((current) => current.map((r) => r.key === row.key ? { ...r, state: 'added' } : r));
      } catch (err) {
        const why = err instanceof ApiRequestError ? err.message : 'Could not add it.';
        setRows((current) => current.map((r) => r.key === row.key ? { ...r, state: 'failed', error: why } : r));
      }
    }
    setRunning(false);
    setFinished(true);
    onAdded();
  }

  // Everything added: nothing left to decide here.
  const allIn = finished && rows.length > 0 && failed.length === 0 && done === rows.length;
  useEffect(() => {
    if (allIn) onClose();
  }, [allIn, onClose]);
  // Some were refused: show the first one and why, not just a count.
  useEffect(() => {
    if (finished && !running) {
      list.current?.querySelector('[data-state=failed]')?.scrollIntoView({ block: 'nearest' });
    }
  }, [finished, running]);

  const sendable = todo.filter((r) => !r.wrong).length;
  const title = niche.trim() ? `Add several to “${niche.trim()}”` : 'Add several';

  return (
    <Modal open size="wide" title={title} onClose={() => { if (!running) onClose(); }} footer={
      <>
        <Button tone="primary" data-testid="several-review" disabled={running || !sendable}
                onClick={() => void addAll(false)}>
          {!sendable ? 'Send to Review' : finished && failed.length ? `Try again: send ${sendable} to Review`
            : `Send ${sendable} to Review`}
        </Button>
        <Button data-testid="several-ready" disabled={running || !sendable} onClick={() => void addAll(true)}>
          {sendable ? `Add ${sendable} as Ready to Post` : 'Add as Ready to Post'}</Button>
        <Button onClick={onClose} disabled={running}>{done ? 'Close' : 'Cancel'}</Button>
        <span className="text-[12px] text-ink-faint" data-testid="several-progress">
          {running ? `Adding ${Math.min(done + 1, rows.length)} of ${rows.length}…`
            : finished ? `${done} added${failed.length ? ` · ${failed.length} not added` : ''}` : ''}
        </span>
      </>
    }>
      <div className="grid grid-cols-1 gap-6 lg:grid-cols-[18rem_1fr]" data-testid="several">
        <section>
          <Field label="Type" hint="Each file becomes its own item of this type.">
            <select data-testid="several-type" className={inputClass} value={type} disabled={running || done > 0}
                    onChange={(e) => changeType(e.target.value)}>
              {types.map((id) => <option key={id} value={id}>{meta.types[id]!.label}</option>)}
            </select>
          </Field>
          <Field label="Niche">
            <input data-testid="several-niche" className={inputClass} value={niche} list="several-niches"
                   disabled={running || done > 0} onChange={(e) => setNiche(e.target.value)} />
            <datalist id="several-niches">{meta.niches.map((n) => <option key={n} value={n} />)}</datalist>
          </Field>
          <h3 className="mb-2 mt-4 text-[11px] font-semibold uppercase tracking-[0.16em] text-ink-faint">
            Where they go</h3>
          <p className="mb-2 text-[12px] text-ink-faint">Optional — the same platforms for every one. Each can be
            changed on its own later.</p>
          <div className="flex flex-wrap gap-2" data-testid="several-platforms">
            {accepts.map(([id, p]) => {
              const on = platforms.includes(id);
              return (
                <button key={id} type="button" aria-pressed={on} data-testid={`several-platform-${id}`}
                        disabled={running || done > 0}
                        onClick={() => setPlatforms((current) => on ? current.filter((x) => x !== id) : [...current, id])}
                        className={`rounded-pill border px-3 py-1 text-[13px] ${on
                          ? 'border-accent/40 bg-accent/15 text-accent' : 'border-surface-border text-ink-muted hover:text-ink'}`}>
                  {p.label}
                </button>
              );
            })}
          </div>
          <p className="mt-4 text-[12px] leading-relaxed text-ink-faint">
            Titles, captions and thumbnails can be added to each one afterwards, from its own page.
          </p>
        </section>
        <section className="min-w-0">
          <div
            data-testid="several-drop"
            onDragOver={(e) => { e.preventDefault(); setOver(true); }}
            onDragLeave={() => setOver(false)}
            onDrop={(e) => { e.preventDefault(); setOver(false); if (!running) pick(e.dataTransfer.files); }}
            className={`mb-3 flex flex-col items-center justify-center gap-2 rounded-lg border border-dashed px-4 py-6
                        text-center text-[13px] ${over ? 'border-accent bg-accent/10 text-ink'
                          : 'border-surface-border text-ink-muted'}`}>
            <span>Drop {info?.label.toLowerCase() ?? 'files'} files here, or</span>
            <Button disabled={running} onClick={() => picker.current?.click()} data-testid="several-choose">
              Choose files…</Button>
            <input ref={picker} type="file" multiple className="sr-only" accept={ACCEPT[info?.render ?? ''] ?? ''}
                   data-testid="several-files"
                   onChange={(e) => { pick(e.target.files); e.target.value = ''; }} />
          </div>
          {rows.length > 0 && (
            <>
              <p className="mb-2 text-[12px] text-ink-faint">
                {rows.length} file{rows.length === 1 ? '' : 's'} — {rows.length} separate item{rows.length === 1 ? '' : 's'}.
                The name is only for you; change any of them here.
              </p>
              <ul ref={list} className="max-h-[46vh] space-y-1.5 overflow-y-auto pr-1" data-testid="several-list">
                {rows.map((row) => (
                  <li key={row.key} data-testid="several-row" data-state={row.state}
                      className="flex flex-wrap items-center gap-2 rounded border border-surface-border px-2 py-1.5">
                    <input className={`${inputClass} min-w-0 flex-1 py-1`} value={row.name}
                           data-testid="several-name" disabled={running || row.state === 'added'}
                           aria-label={`Name for ${row.file.name}`}
                           onChange={(e) => setRows((current) => current.map((r) => r.key === row.key
                             ? { ...r, name: e.target.value } : r))} />
                    <span className="hidden w-48 shrink-0 items-baseline gap-1 text-[11px] text-ink-faint sm:flex"
                          title={row.file.name}>
                      <span className="min-w-0 truncate">{row.file.name}</span>
                      <span className="shrink-0">· {size(row.file.size)}</span>
                    </span>
                    <span className={`w-24 shrink-0 text-right text-[12px] ${row.state === 'added' ? 'text-state-ok'
                      : row.state === 'failed' ? 'text-state-danger' : row.state === 'adding' ? 'text-accent'
                        : 'text-ink-faint'}`} data-testid="several-state">
                      {row.state === 'added' ? 'Added ✓' : row.state === 'failed' ? 'Not added'
                        : row.state === 'adding' ? 'Adding…' : 'Waiting'}
                    </span>
                    <button type="button" aria-label={`Remove ${row.file.name}`} data-testid="several-remove"
                            disabled={running || row.state === 'added'}
                            className="shrink-0 px-1 text-ink-faint hover:text-state-danger disabled:opacity-30"
                            onClick={() => setRows((current) => current.filter((r) => r.key !== row.key))}>×</button>
                    {row.error && (
                      <span className="basis-full text-[12px] text-state-danger" data-testid="several-error">
                        {row.error}</span>
                    )}
                  </li>
                ))}
              </ul>
            </>
          )}
        </section>
      </div>
    </Modal>
  );
}

/** Said the way a person would: "a video", "an image", "an audio file". */
function notA(kind: string): string {
  return `This isn't ${kind === 'video' ? 'a video' : kind === 'image' ? 'an image' : 'an audio'} file.`;
}

function size(bytes: number): string {
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

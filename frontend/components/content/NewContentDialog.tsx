'use client';

import { useMemo, useState } from 'react';

import { Button } from '@/components/ui/Button';
import { Field, inputClass } from '@/components/ui/Field';
import { Modal } from '@/components/ui/Modal';
import { api, ApiRequestError } from '@/lib/api';
import type { ContentItemDetail, ContentMeta } from '@/lib/api-types';

import { entriesFrom, type FileEntry, FilesEditor, toRequest } from './FilesEditor';
import { fromDraft, toDraft } from './format';

/** The supporting text a content type carries, as form fields. */
export function SupportingFields({
  meta, fields, draft, onChange, testid = 'new-field',
}: {
  meta: ContentMeta;
  fields: string[];
  draft: Record<string, string>;
  onChange: (draft: Record<string, string>) => void;
  testid?: string;
}) {
  return (
    <>
      {fields.map((field) => {
        const spec = meta.fields[field];
        const common = {
          'data-testid': `${testid}-${field}`, className: inputClass, value: draft[field] ?? '',
          onChange: (e: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement>) =>
            onChange({ ...draft, [field]: e.target.value }),
        };
        return (
          <Field key={field} label={spec?.label ?? field}
                 hint={spec?.kind === 'list' ? (field === 'hashtags' ? 'Separated by spaces.' : 'Separated by commas.')
                   : undefined}>
            {spec?.kind === 'longtext'
              ? <textarea {...common} className={`${inputClass} ${field === 'body' ? 'min-h-[120px]' : 'min-h-[64px]'}`} />
              : <input {...common} />}
          </Field>
        );
      })}
    </>
  );
}

/**
 * Adding content yourself — a video you made, an image, a carousel, a post.
 * It goes through the SAME door agents and Jarvis use (`POST /api/content-items`),
 * into the same pipeline, wherever that pipeline currently is. "Ready to Post"
 * is the ordinary approve step, done as it is added.
 */
export function NewContentDialog({
  meta, onCreated, onClose,
}: { meta: ContentMeta; onCreated: (itemId: string) => void; onClose: () => void }) {
  const typeIds = Object.keys(meta.types);
  const [type, setType] = useState(typeIds[0] ?? 'video');
  const [name, setName] = useState('');
  const [niche, setNiche] = useState('');
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [files, setFiles] = useState<FileEntry[]>([]);
  const [platforms, setPlatforms] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const info = meta.types[type];
  const accepts = useMemo(() => Object.entries(meta.platforms).filter(([, p]) => p.accepts.includes(type)), [meta, type]);

  function changeType(next: string) {
    setType(next);
    const roles = new Set([meta.types[next]?.media, ...(meta.types[next]?.assets ?? [])]);
    setFiles((current) => current.filter((f) => roles.has(f.role)));
    setPlatforms((current) => current.filter((p) => meta.platforms[p]?.accepts.includes(next)));
  }

  async function create(readyToPost: boolean) {
    setBusy(true);
    setError(null);
    try {
      const { media, uploads } = toRequest(files);
      const { item } = await api.content.create({
        name: name.trim(), contentType: type, niche: niche.trim(), producer: 'you',
        fields: fromDraft(draft, {}, meta, info?.fields ?? []), media,
        platforms: platforms.map((platform) => ({ platform })), readyToPost,
      }, uploads);
      onCreated(item.id);
      onClose();
    } catch (err) {
      setError(err instanceof ApiRequestError ? err.message : 'Could not add it.');
      setBusy(false);
    }
  }

  return (
    <Modal open size="wide" title="New content" onClose={onClose} footer={
      <>
        <Button tone="primary" data-testid="new-send-review" disabled={busy || !name.trim()}
                onClick={() => void create(false)}>Send to Review</Button>
        <Button data-testid="new-ready" disabled={busy || !name.trim()} onClick={() => void create(true)}>
          Add as Ready to Post</Button>
        <Button onClick={onClose} disabled={busy}>Cancel</Button>
        {busy && <span className="text-[12px] text-ink-faint">Uploading…</span>}
      </>
    }>
      <div className="grid grid-cols-1 gap-6 lg:grid-cols-2" data-testid="new-content">
        <section>
          <div className="grid grid-cols-2 gap-x-3">
            <Field label="Name (only you see this)">
              <input data-testid="new-name" className={inputClass} value={name} autoFocus
                     placeholder="e.g. Nature Sounds #001" onChange={(e) => setName(e.target.value)} />
            </Field>
            <Field label="Type">
              <select data-testid="new-type" className={inputClass} value={type}
                      onChange={(e) => changeType(e.target.value)}>
                {typeIds.map((id) => <option key={id} value={id}>{meta.types[id]!.label}</option>)}
              </select>
            </Field>
          </div>
          <Field label="Niche" hint="Optional — any label you filter by.">
            <input data-testid="new-niche" className={inputClass} value={niche} list="new-niches"
                   onChange={(e) => setNiche(e.target.value)} />
            <datalist id="new-niches">{meta.niches.map((n) => <option key={n} value={n} />)}</datalist>
          </Field>
          <SupportingFields meta={meta} fields={info?.fields ?? []} draft={draft} onChange={setDraft} />
        </section>
        <section className="space-y-4">
          <div>
            <h3 className="mb-2 text-[11px] font-semibold uppercase tracking-[0.16em] text-ink-faint">Files</h3>
            <FilesEditor info={info} meta={meta} entries={files} onChange={setFiles} testid="new-files" />
          </div>
          <div>
            <h3 className="mb-2 text-[11px] font-semibold uppercase tracking-[0.16em] text-ink-faint">Where it goes</h3>
            <p className="mb-2 text-[12px] text-ink-faint">Optional — platforms can be added or scheduled later.</p>
            <div className="flex flex-wrap gap-2" data-testid="new-platforms">
              {accepts.map(([id, p]) => {
                const on = platforms.includes(id);
                return (
                  <button key={id} type="button" aria-pressed={on} data-testid={`new-platform-${id}`}
                          onClick={() => setPlatforms((current) => on ? current.filter((x) => x !== id) : [...current, id])}
                          className={`rounded-pill border px-3 py-1 text-[13px] ${on
                            ? 'border-accent/40 bg-accent/15 text-accent' : 'border-surface-border text-ink-muted hover:text-ink'}`}>
                    {p.label}
                  </button>
                );
              })}
            </div>
          </div>
          {error && <p data-testid="new-error" className="text-[13px] text-state-danger">{error}</p>}
        </section>
      </div>
    </Modal>
  );
}

/**
 * Answering a change request yourself: change the text and/or the files and hand
 * the new version in — the same revision door an agent or Jarvis uses. Back to
 * Review, as always.
 */
export function HandInRevisionDialog({
  item, meta, onDone, onClose,
}: { item: ContentItemDetail; meta: ContentMeta; onDone: () => void; onClose: () => void }) {
  const info = meta.types[item.contentType];
  const [draft, setDraft] = useState<Record<string, string>>(() => toDraft(item.fields, meta));
  const [files, setFiles] = useState<FileEntry[]>(() => entriesFrom(item.media));
  const [note, setNote] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const filesChanged = JSON.stringify(files.map((f) => f.key)) !== JSON.stringify(entriesFrom(item.media).map((f) => f.key));

  async function submit() {
    setBusy(true);
    setError(null);
    try {
      const fields = fromDraft(draft, item.fields, meta, info?.fields ?? []);
      const { media, uploads } = toRequest(files);
      await api.content.revise(item.id, {
        fields, ...(filesChanged ? { media } : {}), note: note.trim(), by: 'you',
      }, filesChanged ? uploads : []);
      onDone();
      onClose();
    } catch (err) {
      setError(err instanceof ApiRequestError ? err.message : 'Could not hand that in.');
      setBusy(false);
    }
  }

  return (
    <Modal open nested size="wide" title="Hand in a revision" onClose={onClose} footer={
      <>
        <Button tone="primary" data-testid="revise-save" disabled={busy} onClick={() => void submit()}>
          Hand in revision {item.revision + 1}</Button>
        <Button onClick={onClose} disabled={busy}>Cancel</Button>
      </>
    }>
      <p className="mb-4 text-[13px] text-ink-muted">
        Asked for: <span className="text-ink">“{item.openRequest?.what}”</span>. It goes back to Review as
        revision {item.revision + 1}.
      </p>
      <div className="grid grid-cols-1 gap-6 lg:grid-cols-2" data-testid="revise">
        <section>
          <SupportingFields meta={meta} fields={info?.fields ?? []} draft={draft} onChange={setDraft}
                            testid="revise-field" />
          <Field label="What you changed" hint="One line, for the history.">
            <input data-testid="revise-note" className={inputClass} value={note} onChange={(e) => setNote(e.target.value)} />
          </Field>
        </section>
        <section>
          <FilesEditor info={info} meta={meta} entries={files} onChange={setFiles} testid="revise-files" />
        </section>
      </div>
      {error && <p className="mt-3 text-[13px] text-state-danger">{error}</p>}
    </Modal>
  );
}

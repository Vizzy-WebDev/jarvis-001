'use client';

import { useState } from 'react';

import { Button } from '@/components/ui/Button';
import { Field, inputClass } from '@/components/ui/Field';
import { Modal } from '@/components/ui/Modal';
import { api, ApiRequestError } from '@/lib/api';
import type { ContentFolder } from '@/lib/api-types';

function message(err: unknown, fallback: string): string {
  return err instanceof ApiRequestError ? err.message : fallback;
}

/** Naming a niche: a new one, or a new name for one (everything in it moves along). */
export function NicheNameDialog({
  current, onDone, onClose,
}: { current?: string; onDone: (name: string) => void; onClose: () => void }) {
  const [name, setName] = useState(current ?? '');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const unchanged = current !== undefined && name.trim() === current;

  async function save() {
    setBusy(true);
    setError(null);
    try {
      const done = current === undefined
        ? await api.content.createNiche(name.trim())
        : await api.content.renameNiche(current, name.trim());
      onDone(done.name);
      onClose();
    } catch (err) {
      setError(message(err, 'Could not save the niche.'));
      setBusy(false);
    }
  }

  return (
    <Modal open title={current === undefined ? 'New niche' : `Rename “${current}”`} onClose={onClose} footer={
      <>
        <Button tone="primary" data-testid="niche-save" disabled={busy || !name.trim() || unchanged}
                onClick={() => void save()}>{current === undefined ? 'Create niche' : 'Rename'}</Button>
        <Button onClick={onClose} disabled={busy}>Cancel</Button>
      </>
    }>
      <form onSubmit={(e) => { e.preventDefault(); if (name.trim() && !unchanged) void save(); }}>
        <Field label="Name" hint={current === undefined
          ? 'A topic you make content about — Psychology, Fitness, Cooking.'
          : 'Everything in this niche moves with it, including the archive and the Recycle Bin.'}>
          <input data-testid="niche-name" className={inputClass} value={name} autoFocus maxLength={80}
                 placeholder="e.g. Psychology" onChange={(e) => setName(e.target.value)} />
        </Field>
      </form>
      {error && <p data-testid="niche-error" className="mt-3 text-[13px] text-state-danger">{error}</p>}
    </Modal>
  );
}

/** Deleting a niche: only an empty one. Deleting a folder never deletes content,
 *  so a niche that still holds anything says what, and offers no delete. */
export function NicheDeleteDialog({
  name, folder, onDone, onClose,
}: { name: string; folder: ContentFolder; onDone: () => void; onClose: () => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const held = folder.total + folder.archived + folder.binned;
  const parts = [folder.total ? `${folder.total} active` : '', folder.archived ? `${folder.archived} archived` : '',
                 folder.binned ? `${folder.binned} in the Recycle Bin` : ''].filter(Boolean);

  return (
    <Modal open title={held ? `“${name}” isn't empty` : `Delete “${name}”?`} onClose={onClose} footer={
      held ? <Button onClick={onClose} data-testid="niche-delete-ok">OK</Button> : (
        <>
          <Button tone="danger" data-testid="niche-delete-yes" disabled={busy} onClick={async () => {
            setBusy(true);
            try {
              await api.content.deleteNiche(name);
              onDone();
              onClose();
            } catch (err) {
              setError(message(err, 'Could not delete the niche.'));
              setBusy(false);
            }
          }}>Delete niche</Button>
          <Button onClick={onClose} disabled={busy}>Cancel</Button>
        </>
      )
    }>
      <p className="text-[14px] leading-relaxed text-ink" data-testid="niche-delete-body">
        {held ? (
          <>It still holds {held === 1 ? 'one item' : `${held} items`} ({parts.join(', ')}).
            {' '}{held === 1 ? 'Move it to another niche, or delete it forever from the Recycle Bin, first'
              : 'Move them to another niche, or delete them forever from the Recycle Bin, first'} — deleting a
            niche never deletes content.</>
        ) : 'The niche is empty, so nothing else is affected.'}
      </p>
      {error && <p className="mt-3 text-[13px] text-state-danger">{error}</p>}
    </Modal>
  );
}

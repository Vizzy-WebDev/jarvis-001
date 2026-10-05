'use client';

import { useCallback, useEffect, useState } from 'react';

import { Button } from '@/components/ui/Button';
import { Card } from '@/components/ui/Card';
import { EmptyState } from '@/components/ui/EmptyState';
import { Field, inputClass } from '@/components/ui/Field';
import { Modal } from '@/components/ui/Modal';
import { api, ApiRequestError } from '@/lib/api';
import type { MemoryVersion, ProfileEntry } from '@/lib/api-types';

/**
 * Notes about what the person is working on and working towards.
 *
 * **Not a second store.** These are memories in one category, and the same rows
 * appear on the Memory screen. They have their own screen because the question
 * is different: Memory is "what does it know about me", this is "what have I
 * told it to keep in mind" — and the morning briefing reads exactly these.
 *
 * Oldest first, which is the order they were written in. The browse view sorts
 * newest first, and that is right there and wrong here: a list of goals reads as
 * a story, not as a feed.
 */
export function ProfileScreen() {
  const [entries, setEntries] = useState<ProfileEntry[] | null>(null);
  const [draft, setDraft] = useState('');
  const [open, setOpen] = useState<ProfileEntry | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      setEntries((await api.profile.list()).entries);
      setError(null);
    } catch (err) {
      setError(err instanceof ApiRequestError ? err.message : 'Could not read your notes.');
      setEntries([]);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function add() {
    const text = draft.trim();
    if (!text) return;
    // Cleared BEFORE the round trip, not after it. Clearing afterwards wipes
    // whatever was typed while the save was in flight, which is the whole
    // window someone is most likely to keep typing in.
    setDraft('');
    setBusy(true);
    try {
      await api.profile.add(text);
      await load();
    } catch (err) {
      setError(err instanceof ApiRequestError ? err.message : 'Could not save that note.');
      setDraft((current) => current || text);  // it did not save; give it back
    } finally {
      setBusy(false);
    }
  }

  return (
    <>

      <Card className="mb-4">
        <Field label="Add a note" hint="A goal, a preference, something about how you work.">
          <textarea
            className={inputClass}
            rows={2}
            data-testid="note-input"
            placeholder="Shipping the new site by March"
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={(event) => {
              // Enter sends; Shift+Enter is a new line, as everywhere else here.
              if (event.key === 'Enter' && !event.shiftKey) {
                event.preventDefault();
                void add();
              }
            }}
          />
        </Field>
        <div className="flex justify-end">
          <Button tone="primary" data-testid="note-add" disabled={busy || !draft.trim()}
                  onClick={() => void add()}>
            Add
          </Button>
        </div>
      </Card>

      {error && <p className="mb-4 text-[13px] text-state-danger">{error}</p>}

      {entries === null ? null : entries.length === 0 ? (
        <EmptyState
          title="Nothing here yet"
          body="Add what you are working on, or what you want Jarvis to keep in mind. It reads these when it puts your briefing together."
        />
      ) : (
        <div className="space-y-2" data-testid="note-list">
          {entries.map((entry) => (
            <Card key={entry.id} interactive data-testid="note-row" onClick={() => setOpen(entry)}>
              <p className="text-[14px] text-ink">{entry.text}</p>
              <p className="mt-1 text-[12px] text-ink-faint">
                Added {entry.addedAt.slice(0, 10)}
              </p>
            </Card>
          ))}
        </div>
      )}

      {open && (
        <NoteDetail
          entry={open}
          onClose={() => setOpen(null)}
          onChanged={async () => {
            setOpen(null);
            await load();
          }}
        />
      )}
    </>
  );
}

function NoteDetail({ entry, onClose, onChanged }: {
  entry: ProfileEntry;
  onClose: () => void;
  onChanged: () => Promise<void>;
}) {
  const [text, setText] = useState(entry.text);
  const [versions, setVersions] = useState<MemoryVersion[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api.profile.versions(entry.id)
      .then((found) => setVersions(found.versions))
      .catch(() => setVersions([]));
  }, [entry.id]);

  async function run(action: () => Promise<unknown>) {
    setBusy(true);
    setError(null);
    try {
      await action();
      await onChanged();
    } catch (err) {
      setError(err instanceof ApiRequestError ? err.message : 'That did not work.');
      setBusy(false);
    }
  }

  return (
    <Modal
      open
      title="This note"
      onClose={onClose}
      footer={
        <>
          <Button tone="danger" data-testid="note-delete" disabled={busy}
                  onClick={() => run(() => api.profile.remove(entry.id))}>
            Delete
          </Button>
          <Button tone="primary" data-testid="note-save"
                  disabled={busy || !text.trim() || text.trim() === entry.text}
                  onClick={() => run(() => api.profile.update(entry.id, text.trim()))}>
            Save
          </Button>
        </>
      }
    >
      <Field label="The note">
        <textarea className={inputClass} rows={3} data-testid="note-text"
                  value={text} onChange={(event) => setText(event.target.value)} />
      </Field>

      {versions.length > 0 && (
        <Field label="What it used to say">
          <ul className="space-y-1.5" data-testid="note-versions">
            {versions.map((version, index) => (
              <li key={version.id ?? index} className="text-[12px] text-ink-faint">
                <span className="text-ink-muted">{version.text}</span>
                {version.reason && <span> — {version.reason}</span>}
              </li>
            ))}
          </ul>
        </Field>
      )}

      {error && <p className="mt-2 text-[13px] text-state-danger">{error}</p>}
    </Modal>
  );
}

'use client';

import { useCallback, useEffect, useMemo, useState } from 'react';

import { Button } from '@/components/ui/Button';
import { Card } from '@/components/ui/Card';
import { EmptyState } from '@/components/ui/EmptyState';
import { Field, inputClass } from '@/components/ui/Field';
import { Modal } from '@/components/ui/Modal';
import { PageHeader } from '@/components/ui/PageHeader';
import { api, ApiRequestError } from '@/lib/api';
import type {
  Memory, MemoryCandidate, MemoryCategory, MemoryVersion, Prefs,
} from '@/lib/api-types';

/**
 * Every durable fact Jarvis has saved about the person using it — and the queue
 * of things it would like to save.
 *
 * **The queue comes first whenever anything is in it**, because it is the only
 * part of this screen that is actually asking for something. Everything else is
 * here to be browsed.
 *
 * **The trust dial lives here rather than in Settings.** Once a save can happen
 * without being asked, seeing and undoing it stops being optional — so the dial
 * and the badge that says how each memory got here are readable in one place.
 *
 * **A conflict always needs a person, at every level of that dial, with no
 * override.** Resolving one changes or duplicates something that already exists,
 * and while it waits the older memory stops being asserted to the model as
 * settled fact — which this screen says out loud rather than showing the row as
 * though nothing were wrong.
 */

const TRUST: { id: Prefs['memoryTrust']; label: string; blurb: string }[] = [
  { id: 'ask', label: 'Ask me every time',
    blurb: 'Nothing is saved until you have read it. The safe default.' },
  { id: 'balanced', label: 'Save what it is sure of',
    blurb: 'Only a fact it is confident about saves itself. The rest still asks.' },
  { id: 'auto', label: 'Save without asking',
    blurb: 'Anything it works out gets saved. You can still edit or remove it here.' },
];

const ORIGIN_LABEL: Record<string, string> = {
  explicit: 'You said so',
  approved: 'You approved it',
  auto: 'Saved on its own',
  legacy: 'From before',
};

export function MemoryScreen() {
  const [memories, setMemories] = useState<Memory[] | null>(null);
  const [conflicted, setConflicted] = useState<string[]>([]);
  const [candidates, setCandidates] = useState<MemoryCandidate[]>([]);
  const [categories, setCategories] = useState<MemoryCategory[]>([]);
  const [trust, setTrust] = useState<Prefs['memoryTrust']>('ask');

  const [query, setQuery] = useState('');
  const [category, setCategory] = useState('');
  const [archived, setArchived] = useState(false);
  const [open, setOpen] = useState<Memory | null>(null);
  const [selected, setSelected] = useState<string[]>([]);
  const [merging, setMerging] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const [listed, waiting, groups] = await Promise.all([
        api.memories.list({ query, category, includeArchived: archived }),
        api.memories.candidates(),
        api.memories.categories(),
      ]);
      setMemories(listed.memories);
      setConflicted(listed.conflicted);
      setCandidates(waiting.candidates);
      setCategories(groups.categories);
      setError(null);
    } catch (err) {
      setError(err instanceof ApiRequestError ? err.message : 'Could not read your memories.');
      setMemories([]);
    }
  }, [query, category, archived]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    api.prefs.get().then((prefs) => setTrust(prefs.memoryTrust)).catch(() => undefined);
  }, []);

  async function setTrustLevel(level: Prefs['memoryTrust']) {
    setTrust(level);  // the dial should move under the finger, not after a round trip
    try {
      await api.prefs.update({ memoryTrust: level });
    } catch {
      const prefs = await api.prefs.get().catch(() => null);
      if (prefs) setTrust(prefs.memoryTrust);  // it did not take; do not claim it did
    }
  }

  const chosen = useMemo(
    () => (memories ?? []).filter((memory) => selected.includes(memory.id)),
    [memories, selected]);

  function toggleSelected(id: string) {
    setSelected((current) =>
      current.includes(id) ? current.filter((other) => other !== id) : [...current, id]);
  }

  return (
    <div className="mx-auto max-w-3xl px-6 py-10">
      <PageHeader title="Memory" blurb="Everything Jarvis has saved about you — editable, and undoable.">
        {selected.length > 1 && (
          <Button tone="primary" data-testid="merge-start" onClick={() => setMerging(true)}>
            Merge {selected.length}
          </Button>
        )}
        {selected.length > 0 && (
          <Button onClick={() => setSelected([])}>Clear selection</Button>
        )}
      </PageHeader>

      {error && <p className="mb-4 text-[13px] text-state-danger">{error}</p>}

      {candidates.length > 0 && (
        <ReviewQueue candidates={candidates} memories={memories ?? []} onChanged={load} />
      )}

      <TrustDial value={trust} onChange={setTrustLevel} />

      <div className="mb-4 flex flex-wrap items-center gap-2">
        <input
          className={`${inputClass} max-w-xs`}
          placeholder="Search what it knows"
          data-testid="memory-search"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
        />
        <select
          className={`${inputClass} max-w-[190px]`}
          data-testid="memory-category"
          value={category}
          onChange={(event) => setCategory(event.target.value)}
        >
          <option value="">Every category</option>
          {categories.map((group) => (
            <option key={group.name} value={group.name}>{group.name}</option>
          ))}
        </select>
        <Button data-testid="toggle-archived" onClick={() => setArchived((on) => !on)}>
          {archived ? 'Hide archived' : 'Show archived'}
        </Button>
      </div>

      {memories === null ? null : memories.length === 0 ? (
        <EmptyState
          title="Nothing saved yet"
          body="Tell Jarvis something worth remembering, or say “remember that…” in a conversation. What it works out on its own shows up here to be approved first."
        />
      ) : (
        <div className="space-y-2" data-testid="memory-list">
          {memories.map((memory) => (
            <Card
              key={memory.id}
              interactive
              data-testid="memory-row"
              onClick={() => setOpen(memory)}
            >
              <div className="flex items-start gap-3">
                <input
                  type="checkbox"
                  aria-label={`Select “${memory.text}”`}
                  data-testid="memory-select"
                  className="mt-1 accent-accent"
                  checked={selected.includes(memory.id)}
                  onClick={(event) => event.stopPropagation()}
                  onChange={() => toggleSelected(memory.id)}
                />
                <div className="min-w-0 flex-1">
                  <p className="text-[14px] text-ink">{memory.text}</p>
                  <p className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-[12px] text-ink-faint">
                    <span>{memory.category}</span>
                    <span aria-hidden>·</span>
                    <span>{ORIGIN_LABEL[memory.origin] ?? memory.origin}</span>
                    {memory.archived && <span className="text-ink-muted">· Archived</span>}
                    {conflicted.includes(memory.id) && (
                      <span className="text-state-warn" data-testid="conflicted-flag">
                        · Something disagrees with this — it is not being used until you decide
                      </span>
                    )}
                  </p>
                </div>
              </div>
            </Card>
          ))}
        </div>
      )}

      {open && (
        <MemoryDetail
          memory={open}
          categories={categories}
          onClose={() => setOpen(null)}
          onChanged={async () => {
            setOpen(null);
            await load();
          }}
        />
      )}

      {merging && (
        <MergeDialog
          chosen={chosen}
          onClose={() => setMerging(false)}
          onMerged={async () => {
            setMerging(false);
            setSelected([]);
            await load();
          }}
        />
      )}
    </div>
  );
}

/** The dial, and what each setting actually means in a sentence. */
function TrustDial({ value, onChange }: {
  value: Prefs['memoryTrust'];
  onChange: (level: Prefs['memoryTrust']) => void;
}) {
  return (
    <Card className="mb-4">
      <p className="text-[13px] font-medium text-ink">When Jarvis works something out about you</p>
      <div className="mt-3 space-y-1" data-testid="trust-dial">
        {TRUST.map((option) => (
          <button
            key={option.id}
            type="button"
            data-testid={`trust-${option.id}`}
            aria-pressed={value === option.id}
            onClick={() => onChange(option.id)}
            className={[
              'block w-full rounded border px-3 py-2 text-left transition duration-150 ease-out',
              value === option.id
                ? 'border-accent/40 bg-accent/10'
                : 'border-surface-border hover:border-surface-border-strong',
            ].join(' ')}
          >
            <span className={`block text-[13px] ${value === option.id ? 'text-ink' : 'text-ink-muted'}`}>
              {option.label}
            </span>
            <span className="mt-0.5 block text-[12px] text-ink-faint">{option.blurb}</span>
          </button>
        ))}
      </div>
    </Card>
  );
}

/**
 * What Jarvis would like to save.
 *
 * A conflict is a different question from an ordinary candidate — it is asking
 * which of two things is true, not whether to keep one — so it gets its own
 * three answers rather than the same approve/reject pair.
 */
function ReviewQueue({ candidates, memories, onChanged }: {
  candidates: MemoryCandidate[];
  memories: Memory[];
  onChanged: () => Promise<void>;
}) {
  const [busy, setBusy] = useState<string | null>(null);

  async function act(id: string, action: () => Promise<unknown>) {
    setBusy(id);
    try {
      await action();
      await onChanged();
    } finally {
      setBusy(null);
    }
  }

  return (
    <Card className="mb-4 border-accent/25 bg-accent/[0.04]" data-testid="review-queue">
      <p className="text-[13px] font-medium text-ink">
        {candidates.length === 1 ? 'One thing to review' : `${candidates.length} things to review`}
      </p>
      <div className="mt-3 space-y-3">
        {candidates.map((candidate) => {
          const contradicts = candidate.conflictWith
            ? memories.find((memory) => memory.id === candidate.conflictWith)
            : null;
          return (
            <div key={candidate.id} className="rounded border border-surface-border p-3"
                 data-testid="candidate">
              <p className="text-[14px] text-ink">{candidate.text}</p>
              <p className="mt-1 text-[12px] text-ink-faint">{candidate.category}</p>
              {contradicts && (
                <p className="mt-2 rounded bg-state-warn/10 px-2 py-1.5 text-[12px] text-state-warn">
                  This disagrees with something already saved: “{contradicts.text}”
                </p>
              )}
              <div className="mt-3 flex flex-wrap gap-2">
                {contradicts ? (
                  <>
                    <Button tone="primary" disabled={busy === candidate.id}
                            data-testid="use-new"
                            onClick={() => act(candidate.id,
                              () => api.memories.resolveConflict(candidate.id, 'use-new'))}>
                      Use the new one
                    </Button>
                    <Button disabled={busy === candidate.id}
                            onClick={() => act(candidate.id,
                              () => api.memories.resolveConflict(candidate.id, 'keep-old'))}>
                      Keep what I had
                    </Button>
                    <Button disabled={busy === candidate.id}
                            onClick={() => act(candidate.id,
                              () => api.memories.resolveConflict(candidate.id, 'keep-both'))}>
                      Both are true
                    </Button>
                  </>
                ) : (
                  <>
                    <Button tone="primary" disabled={busy === candidate.id}
                            data-testid="approve-candidate"
                            onClick={() => act(candidate.id, () => api.memories.approve(candidate.id))}>
                      Save it
                    </Button>
                    <Button disabled={busy === candidate.id}
                            data-testid="reject-candidate"
                            onClick={() => act(candidate.id, () => api.memories.reject(candidate.id))}>
                      No thanks
                    </Button>
                  </>
                )}
              </div>
            </div>
          );
        })}
      </div>
    </Card>
  );
}

/** One memory, everything about it, and everything that can be done to it. */
function MemoryDetail({ memory, categories, onClose, onChanged }: {
  memory: Memory;
  categories: MemoryCategory[];
  onClose: () => void;
  onChanged: () => Promise<void>;
}) {
  const [text, setText] = useState(memory.text);
  const [category, setCategory] = useState(memory.category);
  const [versions, setVersions] = useState<MemoryVersion[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api.memories.versions(memory.id)
      .then((found) => setVersions(found.versions))
      .catch(() => setVersions([]));
  }, [memory.id]);

  const changed = text.trim() !== memory.text || category !== memory.category;

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
      title="This memory"
      onClose={onClose}
      footer={
        <>
          {memory.archived ? (
            <>
              <Button data-testid="restore-memory" disabled={busy}
                      onClick={() => run(() => api.memories.restore(memory.id))}>
                Restore
              </Button>
              {/* Only ever from here: archiving first is what leaves a row for an
                  undo elsewhere to point at. */}
              <Button tone="danger" data-testid="delete-memory" disabled={busy}
                      onClick={() => run(() => api.memories.remove(memory.id))}>
                Delete for good
              </Button>
            </>
          ) : (
            <Button data-testid="archive-memory" disabled={busy}
                    onClick={() => run(() => api.memories.archive(memory.id))}>
              Archive
            </Button>
          )}
          <Button tone="primary" data-testid="save-memory" disabled={busy || !changed}
                  onClick={() => run(() => api.memories.update(memory.id, { text: text.trim(), category }))}>
            Save
          </Button>
        </>
      }
    >
      <Field label="What it knows">
        <textarea className={inputClass} rows={3} data-testid="memory-text"
                  value={text} onChange={(event) => setText(event.target.value)} />
      </Field>
      <Field label="Category">
        <select className={inputClass} value={category}
                onChange={(event) => setCategory(event.target.value)}>
          {categories.map((group) => (
            <option key={group.name} value={group.name}>{group.name}</option>
          ))}
        </select>
      </Field>
      <Field label="How it got here" hint={`Saved ${memory.createdAt.slice(0, 10)}`}>
        <p className="text-[13px] text-ink-muted">{ORIGIN_LABEL[memory.origin] ?? memory.origin}</p>
      </Field>

      {versions.length > 0 && (
        <Field label="What it used to say">
          <ul className="space-y-1.5" data-testid="memory-versions">
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

/**
 * Folding duplicates into one.
 *
 * The others are ARCHIVED rather than deleted: a merge that turns out to have
 * been wrong should be recoverable, and the primary keeps a version row saying
 * what it became.
 */
function MergeDialog({ chosen, onClose, onMerged }: {
  chosen: Memory[];
  onClose: () => void;
  onMerged: () => Promise<void>;
}) {
  const primary = chosen[0];
  const [text, setText] = useState(chosen.map((memory) => memory.text).join(' '));
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  if (!primary) return null;

  async function merge() {
    setBusy(true);
    setError(null);
    try {
      await api.memories.merge(primary!.id, chosen.slice(1).map((memory) => memory.id),
                               text.trim(), primary!.category);
      await onMerged();
    } catch (err) {
      setError(err instanceof ApiRequestError ? err.message : 'Could not merge those.');
      setBusy(false);
    }
  }

  return (
    <Modal
      open
      title={`Merge ${chosen.length} memories`}
      onClose={onClose}
      footer={
        <Button tone="primary" data-testid="merge-confirm" disabled={busy || !text.trim()}
                onClick={merge}>
          Merge
        </Button>
      }
    >
      <p className="mb-3 text-[13px] text-ink-muted">
        These become one. The others are archived rather than deleted, so this can be undone.
      </p>
      <ul className="mb-3 space-y-1">
        {chosen.map((memory) => (
          <li key={memory.id} className="text-[12px] text-ink-faint">· {memory.text}</li>
        ))}
      </ul>
      <Field label="What the single memory should say">
        <textarea className={inputClass} rows={3} data-testid="merge-text"
                  value={text} onChange={(event) => setText(event.target.value)} />
      </Field>
      {error && <p className="text-[13px] text-state-danger">{error}</p>}
    </Modal>
  );
}

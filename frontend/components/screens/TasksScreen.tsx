'use client';

import { useCallback, useEffect, useState } from 'react';

import { Button } from '@/components/ui/Button';
import { Card } from '@/components/ui/Card';
import { EmptyState } from '@/components/ui/EmptyState';
import { Field, inputClass } from '@/components/ui/Field';
import { Modal } from '@/components/ui/Modal';
import { Toggle } from '@/components/ui/Toggle';
import { api, ApiRequestError } from '@/lib/api';
import type { Recurrence, Task, TaskRun } from '@/lib/api-types';

/**
 * Work that runs on a clock.
 *
 * Every row opens into a real editor, and the on/off switch works from the list
 * without opening anything — pausing a task is the thing people come here to do
 * most often, and making that a two-step trip through a detail view would be
 * the wrong default.
 *
 * The schedule sentence under each title is the BACKEND's own
 * `recurrence.describe()`, not a second implementation in TypeScript. The
 * vocabulary of "every weekday at 07:00" exists once, where the maths that
 * produces the next run also lives.
 */
const REPEATS: { value: string; label: string }[] = [
  { value: 'daily', label: 'Every day' },
  { value: 'weekdays', label: 'Weekdays' },
  { value: 'weekly', label: 'Certain days' },
  { value: 'once', label: 'Once' },
  { value: 'interval', label: 'Every so often' },
];

const DAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];

/** What a brand-new task starts as: something valid, that does nothing yet. */
function blank(): Task {
  return {
    id: '', title: '', recurrence: { type: 'daily', time: '08:00' },
    action: { type: 'prompt', prompt: '' }, enabled: true, notify: 'on_error',
    nextRunAt: null, createdAt: '', lastRunAt: null,
  };
}

export function TasksScreen() {
  const [tasks, setTasks] = useState<Task[] | null>(null);
  const [descriptions, setDescriptions] = useState<Record<string, string>>({});
  const [draft, setDraft] = useState<Task | null>(null);
  const [runs, setRuns] = useState<TaskRun[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const load = useCallback(async () => {
    try {
      const answer = await api.tasks.list();
      setTasks(answer.tasks);
      setDescriptions(answer.descriptions);
    } catch (err) {
      setError(err instanceof ApiRequestError ? err.message : 'Could not read your tasks.');
      setTasks([]);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function edit(task: Task) {
    setDraft(task);
    setRuns([]);
    setError(null);
    try {
      setRuns((await api.tasks.open(task.id)).runs);
    } catch {
      /* the editor is still usable without its history */
    }
  }

  async function setEnabled(task: Task, enabled: boolean) {
    // Answer the click first, then confirm with the server: a switch that waits
    // on a round trip feels broken even when it is working.
    setTasks((current) => (current ?? []).map((t) => (t.id === task.id ? { ...t, enabled } : t)));
    try {
      const saved = await api.tasks.update(task.id, { enabled });
      setTasks((current) => (current ?? []).map((t) => (t.id === task.id ? saved.task : t)));
    } catch (err) {
      setError(err instanceof ApiRequestError ? err.message : 'That change did not save.');
      void load();
    }
  }

  async function save() {
    if (!draft) return;
    setSaving(true);
    setError(null);
    const patch = {
      title: draft.title, recurrence: draft.recurrence, action: draft.action,
      enabled: draft.enabled, notify: draft.notify,
    };
    try {
      if (draft.id) await api.tasks.update(draft.id, patch);
      else await api.tasks.create(patch);
      setDraft(null);
      await load();
    } catch (err) {
      setError(err instanceof ApiRequestError ? err.message : 'That task could not be saved.');
    } finally {
      setSaving(false);
    }
  }

  async function remove() {
    if (!draft?.id) return;
    await api.tasks.remove(draft.id).catch(() => undefined);
    setDraft(null);
    await load();
  }

  async function runNow() {
    if (!draft?.id) return;
    setSaving(true);
    setError(null);
    try {
      await api.tasks.runNow(draft.id);
      setRuns((await api.tasks.open(draft.id)).runs);
      await load();
    } catch (err) {
      setError(err instanceof ApiRequestError ? err.message : 'That task could not be run.');
    } finally {
      setSaving(false);
    }
  }

  const patch = (change: Partial<Task>) => setDraft((current) => (current ? { ...current, ...change } : current));
  const patchRecurrence = (change: Partial<Recurrence>) =>
    patch({ recurrence: { ...(draft?.recurrence ?? { type: 'daily' }), ...change } as Recurrence });

  return (
    <>
      <div className="mb-4 flex items-center gap-2">
        <Button tone="primary" data-testid="new-task" onClick={() => void edit(blank())}>
          New task
        </Button>
      </div>

      {error && !draft && <p className="mb-4 text-[13px] text-state-danger">{error}</p>}

      {tasks === null ? (
        <p className="py-10 text-center text-[13px] text-ink-faint">Reading…</p>
      ) : tasks.length === 0 ? (
        <EmptyState
          title="Nothing is scheduled."
          body="A task runs on a clock whether you are here or not — a morning summary, a weekly tidy-up, a nudge on a Friday. You can also just ask Jarvis to set one up."
          action={<Button tone="primary" onClick={() => void edit(blank())}>New task</Button>}
        />
      ) : (
        <ul className="space-y-2" data-testid="task-list">
          {tasks.map((task) => (
            <li key={task.id}>
              <Card interactive data-testid="task-row" className="flex items-center gap-4">
                <button
                  type="button"
                  data-testid="task-open"
                  onClick={() => void edit(task)}
                  className="min-w-0 flex-1 text-left focus-visible:outline-none"
                >
                  <p className={`truncate text-[14px] ${task.enabled ? 'text-ink' : 'text-ink-faint'}`}>
                    {task.title || descriptions[task.id] || 'Untitled task'}
                  </p>
                  <p className="mt-0.5 truncate text-[12px] text-ink-faint">
                    {descriptions[task.id]}
                    {task.enabled && task.nextRunAt
                      ? ` · next ${new Date(task.nextRunAt).toLocaleString()}`
                      : ' · paused'}
                  </p>
                </button>
                <Toggle
                  label={`${task.enabled ? 'Pause' : 'Resume'} ${task.title || 'this task'}`}
                  checked={task.enabled}
                  onChange={(next) => void setEnabled(task, next)}
                />
              </Card>
            </li>
          ))}
        </ul>
      )}

      <Modal
        open={draft !== null}
        title={draft?.id ? 'Edit task' : 'New task'}
        onClose={() => setDraft(null)}
        footer={
          draft && (
            <>
              <Button tone="primary" disabled={saving} onClick={() => void save()}>
                {saving ? 'Saving…' : 'Save'}
              </Button>
              {draft.id && (
                <>
                  <Button disabled={saving} onClick={() => void runNow()}>Run now</Button>
                  <Button tone="danger" className="ml-auto" onClick={() => void remove()}>
                    Delete
                  </Button>
                </>
              )}
            </>
          )
        }
      >
        {draft && (
          <>
            {error && <p className="mb-3 text-[13px] text-state-danger">{error}</p>}

            <Field label="Name" hint="Leave it empty and Jarvis names it after its schedule.">
              <input
                className={inputClass}
                value={draft.title}
                data-testid="task-title"
                placeholder="Morning summary"
                onChange={(event) => patch({ title: event.target.value })}
              />
            </Field>

            <Field label="Repeat">
              <select
                className={inputClass}
                value={String(draft.recurrence.type)}
                data-testid="task-repeat"
                onChange={(event) => patchRecurrence(defaultsFor(event.target.value))}
              >
                {REPEATS.map((option) => (
                  <option key={option.value} value={option.value}>{option.label}</option>
                ))}
              </select>
            </Field>

            {['daily', 'weekdays', 'weekly'].includes(String(draft.recurrence.type)) && (
              <Field label="Time">
                <input
                  type="time"
                  className={inputClass}
                  value={String(draft.recurrence.time ?? '08:00')}
                  onChange={(event) => patchRecurrence({ time: event.target.value })}
                />
              </Field>
            )}

            {draft.recurrence.type === 'weekly' && (
              <Field label="Days">
                <div className="flex flex-wrap gap-1.5">
                  {DAYS.map((day, index) => {
                    const chosen = (draft.recurrence.days as number[] | undefined) ?? [];
                    const on = chosen.includes(index);
                    return (
                      <button
                        key={day}
                        type="button"
                        aria-pressed={on}
                        onClick={() => patchRecurrence({
                          days: on ? chosen.filter((d) => d !== index) : [...chosen, index].sort(),
                        })}
                        className={[
                          'rounded-pill border px-3 py-1 text-[12px] transition duration-150',
                          on
                            ? 'border-accent/40 bg-accent/15 text-accent'
                            : 'border-surface-border text-ink-muted hover:text-ink',
                        ].join(' ')}
                      >
                        {day}
                      </button>
                    );
                  })}
                </div>
              </Field>
            )}

            {draft.recurrence.type === 'once' && (
              <Field label="When">
                <input
                  type="datetime-local"
                  className={inputClass}
                  value={forInput(String(draft.recurrence.at ?? ''))}
                  onChange={(event) =>
                    patchRecurrence({ at: new Date(event.target.value).toISOString() })}
                />
              </Field>
            )}

            {draft.recurrence.type === 'interval' && (
              <Field label="Every (minutes)">
                <input
                  type="number"
                  min={1}
                  className={inputClass}
                  value={Math.max(1, Math.round(Number(draft.recurrence.everyMs ?? 3600000) / 60000))}
                  onChange={(event) => patchRecurrence({
                    everyMs: Math.max(1, Number(event.target.value) || 1) * 60000,
                  })}
                />
              </Field>
            )}

            <Field
              label="What it does"
              hint="Jarvis handles this the way it would if you asked in the moment — anything risky still asks first."
            >
              <textarea
                rows={3}
                className={`${inputClass} resize-none`}
                data-testid="task-prompt"
                placeholder="Give me a short summary of what's due today."
                value={String(draft.action.prompt ?? '')}
                onChange={(event) =>
                  patch({ action: { ...draft.action, type: 'prompt', prompt: event.target.value } })}
              />
            </Field>

            <Field label="Tell me about it">
              <select
                className={inputClass}
                value={draft.notify}
                onChange={(event) => patch({ notify: event.target.value })}
              >
                <option value="on_error">Only when something goes wrong</option>
                <option value="always">Every time it runs</option>
                <option value="never">Never</option>
              </select>
            </Field>

            <div className="flex items-center justify-between gap-4 border-t border-surface-border py-3">
              <span className="text-[14px] text-ink">Active</span>
              <Toggle
                label="Active"
                checked={draft.enabled}
                onChange={(enabled) => patch({ enabled })}
              />
            </div>

            {draft.id && (
              <div className="border-t border-surface-border pt-3">
                <p className="mb-2 text-[12px] font-medium text-ink-muted">Recent runs</p>
                {runs.length === 0 ? (
                  <p className="text-[12px] text-ink-faint">It has not run yet.</p>
                ) : (
                  <ul className="space-y-1.5">
                    {runs.slice(0, 8).map((run) => (
                      <li key={run.id} className="flex items-start gap-2 text-[12px]">
                        <span className={run.ok ? 'text-state-ok' : 'text-state-danger'}>
                          {run.ok ? '✓' : '✕'}
                        </span>
                        <span className="min-w-0 flex-1 text-ink-muted">
                          {run.summary || run.error || (run.ok ? 'Ran.' : 'Failed.')}
                        </span>
                        {run.ranAt && (
                          <span className="shrink-0 text-ink-faint">
                            {new Date(String(run.ranAt)).toLocaleString()}
                          </span>
                        )}
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            )}
          </>
        )}
      </Modal>
    </>
  );
}

/** Switching repeat type replaces the whole shape: `days` left behind on a
 *  daily task would be silently ignored by the backend and confusing here. */
function defaultsFor(type: string): Recurrence {
  if (type === 'weekly') return { type, time: '08:00', days: [1] };
  if (type === 'once') return { type, at: new Date(Date.now() + 3600_000).toISOString() };
  if (type === 'interval') return { type, everyMs: 3_600_000, anchor: new Date().toISOString() };
  return { type, time: '08:00' };
}

/** `datetime-local` wants local wall-clock time with no zone, not an ISO Z. */
function forInput(iso: string): string {
  const when = new Date(iso);
  if (Number.isNaN(when.getTime())) return '';
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${when.getFullYear()}-${pad(when.getMonth() + 1)}-${pad(when.getDate())}T${pad(when.getHours())}:${pad(when.getMinutes())}`;
}

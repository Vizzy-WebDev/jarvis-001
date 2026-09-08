'use client';

import { useCallback, useEffect, useState } from 'react';

import { Button } from '@/components/ui/Button';
import { Card } from '@/components/ui/Card';
import { EmptyState } from '@/components/ui/EmptyState';
import { Field, inputClass } from '@/components/ui/Field';
import { Modal } from '@/components/ui/Modal';
import { api, ApiRequestError } from '@/lib/api';
import type { Job, OutboxRow, TraceRow } from '@/lib/api-types';

/**
 * Work happening in the background while the conversation is about something
 * else — a different thing from a scheduled task, which runs on a clock nobody
 * judged. A job is work somebody decided to put in the background right now.
 *
 * **The trace is the point of this screen.** An intent row is written before an
 * action runs and an outcome row after it, so a crash between the two still
 * leaves the intent on record. Reading it is the only way to tell "it says it
 * did this" apart from "it did this", and it is what decides whether picking a
 * crashed job back up is safe at all.
 *
 * **A job that operates the real machine never starts on its own.** It is
 * created already parked, and the ordinary "keep going" is the only thing that
 * starts it — there is no separate confirm here, because there is no separate
 * mechanism.
 */

const STATUS_LABEL: Record<Job['status'], string> = {
  queued: 'Waiting to start',
  running: 'Working',
  awaiting_decision: 'Waiting on you',
  stalled: 'Stuck',
  done: 'Finished',
  cancelled: 'Stopped',
};

const STATUS_TONE: Record<Job['status'], string> = {
  queued: 'text-ink-faint',
  running: 'text-accent',
  awaiting_decision: 'text-state-warn',
  stalled: 'text-state-danger',
  done: 'text-state-ok',
  cancelled: 'text-ink-faint',
};

/** What the trace concluded about picking this up again, said plainly. */
const RECOVERY_LABEL: Record<string, string> = {
  resumable: 'It can carry on from where it stopped.',
  restartable: 'It has to start over, but nothing it did needs undoing.',
  needs_input: 'It needs something from you before it can go on.',
  unrecoverable: 'It already did something outside Jarvis that starting over could repeat.',
};

const FILTERS: [string, string][] = [
  ['', 'Everything'],
  ['queued,running,awaiting_decision,stalled', 'Still going'],
  ['done', 'Finished'],
  ['cancelled', 'Stopped'],
];

export function JobsScreen() {
  const [jobs, setJobs] = useState<Job[] | null>(null);
  const [filter, setFilter] = useState('');
  const [openId, setOpenId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setJobs((await api.jobs.list(filter || undefined)).jobs);
      setError(null);
    } catch (err) {
      setError(err instanceof ApiRequestError ? err.message : 'Could not read your jobs.');
      setJobs([]);
    }
  }, [filter]);

  useEffect(() => {
    void load();
  }, [load]);

  // A job is by definition happening while the user is elsewhere, so this is the
  // one screen where the data changes under it as a matter of course.
  useEffect(() => {
    const source = new EventSource('/api/events');
    source.onmessage = (raw) => {
      try {
        const type = (JSON.parse(raw.data) as { type?: string }).type ?? '';
        if (type.startsWith('job.')) void load();
      } catch {
        /* a frame we cannot read is not worth acting on */
      }
    };
    return () => source.close();
  }, [load]);

  return (
    <>

      <div className="mb-4 flex flex-wrap gap-1.5" data-testid="job-filters">
        {FILTERS.map(([value, label]) => (
          <Button key={label} aria-pressed={filter === value}
                  className={filter === value ? 'border-accent/40 bg-accent/10 text-accent' : ''}
                  onClick={() => setFilter(value)}>
            {label}
          </Button>
        ))}
      </div>

      {error && <p className="mb-4 text-[13px] text-state-danger">{error}</p>}

      {jobs === null ? null : jobs.length === 0 ? (
        <EmptyState
          title="Nothing running"
          body="Ask Jarvis to keep working on something in the background and it will show up here, with a record of what it actually did."
        />
      ) : (
        <div className="space-y-2" data-testid="job-list">
          {jobs.map((job) => (
            <Card key={job.id} interactive data-testid="job-row" onClick={() => setOpenId(job.id)}>
              <div className="flex items-start justify-between gap-4">
                <div className="min-w-0">
                  <p className="truncate text-[14px] text-ink">{job.title}</p>
                  <p className="mt-1 text-[12px] text-ink-faint">
                    {job.kind}
                    {job.currentStep && ` · ${job.currentStep}`}
                  </p>
                </div>
                <span className={`shrink-0 text-[12px] ${STATUS_TONE[job.status] ?? 'text-ink-faint'}`}
                      data-testid="job-status">
                  {STATUS_LABEL[job.status] ?? job.status}
                </span>
              </div>
            </Card>
          ))}
        </div>
      )}

      {openId && (
        <JobDetail
          jobId={openId}
          onClose={() => setOpenId(null)}
          onOpenJob={setOpenId}
          onChanged={load}
        />
      )}
    </>
  );
}

function JobDetail({ jobId, onClose, onOpenJob, onChanged }: {
  jobId: string;
  onClose: () => void;
  onOpenJob: (id: string) => void;
  onChanged: () => Promise<void>;
}) {
  const [detail, setDetail] = useState<{ job: Job; trace: TraceRow[]; outbox: OutboxRow[] } | null>(null);
  const [guidance, setGuidance] = useState('');
  const [confirmRestart, setConfirmRestart] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      setDetail(await api.jobs.open(jobId));
      setError(null);
    } catch (err) {
      setError(err instanceof ApiRequestError ? err.message : 'Could not read that job.');
    }
  }, [jobId]);

  useEffect(() => {
    void load();
  }, [load]);

  async function run(action: () => Promise<unknown>) {
    setBusy(true);
    setError(null);
    try {
      await action();
      await load();
      await onChanged();
    } catch (err) {
      setError(err instanceof ApiRequestError ? err.message : 'That did not work.');
    } finally {
      setBusy(false);
    }
  }

  async function restart(force = false) {
    setBusy(true);
    setError(null);
    try {
      const result = await api.jobs.restart(jobId, force);
      // Not an error: the trace says starting over could repeat something that
      // already left the machine, so it asks rather than doing it.
      if (result.ok === false) {
        setConfirmRestart(result.message ?? RECOVERY_LABEL.unrecoverable!);
        return;
      }
      setConfirmRestart(null);
      await load();
      await onChanged();
    } catch (err) {
      setError(err instanceof ApiRequestError ? err.message : 'Could not restart that.');
    } finally {
      setBusy(false);
    }
  }

  if (!detail) {
    return (
      <Modal open title="Loading" onClose={onClose}>
        {error ? <p className="text-[13px] text-state-danger">{error}</p> : null}
      </Modal>
    );
  }

  const { job, trace, outbox } = detail;
  const waiting = outbox.filter((row) => !row.deliveredAt);
  const finished = job.status === 'done' || job.status === 'cancelled';

  return (
    <Modal
      open
      title={job.title}
      onClose={onClose}
      footer={
        finished ? null : (
          <>
            <Button tone="danger" data-testid="job-discard" disabled={busy}
                    onClick={() => run(() => api.jobs.discard(job.id))}>
              Stop it
            </Button>
            {/* Offered per what the trace concluded, not per status: whether
                starting over is safe is a fact about what it already did. */}
            {job.recovery !== 'unrecoverable' && (
              <Button data-testid="job-restart" disabled={busy} onClick={() => void restart()}>
                Start over
              </Button>
            )}
            <Button tone="primary" data-testid="job-resume" disabled={busy}
                    onClick={() => run(() => api.jobs.resume(job.id, guidance.trim() || undefined))}>
              Keep going
            </Button>
          </>
        )
      }
    >
      <p className={`text-[13px] ${STATUS_TONE[job.status] ?? 'text-ink-faint'}`}>
        {STATUS_LABEL[job.status] ?? job.status}
      </p>
      <p className="mt-1 text-[12px] text-ink-faint">
        {job.kind} · {job.startedAt ? `started ${job.startedAt.slice(0, 16).replace('T', ' ')}` : 'not started yet'}
      </p>
      <p className="mt-3 text-[14px] text-ink">{job.goal}</p>

      {job.parentId && (
        <button type="button" data-testid="job-parent"
                className="mt-3 text-[12px] text-accent hover:underline"
                onClick={() => onOpenJob(job.parentId!)}>
          Part of a larger job — open that one
        </button>
      )}

      {job.result && <Field label="What it came back with">
        <p className="text-[13px] text-ink">{job.result}</p>
      </Field>}
      {job.error && <Field label="What went wrong">
        <p className="text-[13px] text-state-danger">{job.error}</p>
      </Field>}

      {waiting.length > 0 && (
        <div className="mt-4 rounded border border-state-warn/30 bg-state-warn/[0.06] p-3"
             data-testid="job-waiting">
          <p className="text-[13px] font-medium text-ink">It is waiting on you</p>
          {waiting.map((row) => (
            <p key={row.id} className="mt-1 text-[13px] text-ink-muted">{row.summary}</p>
          ))}
          {job.recovery && (
            <p className="mt-2 text-[12px] text-ink-faint">
              {RECOVERY_LABEL[job.recovery] ?? job.recovery}
            </p>
          )}
          <div className="mt-3">
            <Field label="Anything it should do differently"
                   hint="Optional. This goes into the work it is already doing, not a fresh start.">
              <input className={inputClass} data-testid="job-guidance"
                     placeholder="Try the archive instead"
                     value={guidance} onChange={(event) => setGuidance(event.target.value)} />
            </Field>
          </div>
        </div>
      )}

      {job.plan?.steps?.length ? (
        <Field label="What it planned to do">
          <ol className="space-y-1" data-testid="job-plan">
            {job.plan.steps.map((step, index) => (
              <li key={index} className="text-[12px] text-ink-faint">{index + 1}. {step}</li>
            ))}
          </ol>
        </Field>
      ) : null}

      <Field
        label="What it actually did"
        hint="Written down before each action and again after it, so a crash still leaves a record."
      >
        {trace.length === 0 ? (
          <p className="text-[12px] text-ink-faint">Nothing yet.</p>
        ) : (
          <ol className="space-y-1.5" data-testid="job-trace">
            {trace.map((row) => (
              <li key={row.id} className="text-[12px]" data-testid="trace-row">
                <span className={row.phase === 'intent' ? 'text-ink-faint' : 'text-ink-muted'}>
                  {row.phase === 'intent' ? 'About to' : 'Then'}: {row.summary}
                </span>
                {row.effect === 'external' && (
                  <span className="ml-1 text-state-warn">· reached outside Jarvis</span>
                )}
                {typeof row.detail === 'string' && row.detail && (
                  <span className="block pl-4 text-ink-faint">{row.detail}</span>
                )}
              </li>
            ))}
          </ol>
        )}
      </Field>

      {error && <p className="mt-2 text-[13px] text-state-danger">{error}</p>}

      {confirmRestart && (
        <Modal
          open
          nested
          title="Start it over anyway?"
          onClose={() => setConfirmRestart(null)}
          footer={
            <Button tone="danger" data-testid="job-restart-force" onClick={() => void restart(true)}>
              Start over anyway
            </Button>
          }
        >
          <p className="text-[13px] text-ink">{confirmRestart}</p>
          <p className="mt-3 text-[13px] text-ink-muted">
            Nothing has been restarted yet.
          </p>
        </Modal>
      )}
    </Modal>
  );
}

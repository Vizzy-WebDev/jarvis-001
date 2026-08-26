// Job-lifecycle actions safe for server/tools/ to import DIRECTLY — imports
// only job-store.js, job-events.js, job-policy.js, and prefs.js, all leaves
// (prefs.js itself imports only store.js). This module exists specifically
// because orchestrator.js is NOT leaf-safe: it imports worker.js, which
// imports models/runner.js, which imports capabilities.js, which imports
// tools/index.js — so a tool importing orchestrator.js directly would
// recreate the exact deadlock root CLAUDE.md's circular-import invariant
// exists to prevent (tools/index.js dynamically imports every file in
// server/tools/ at load time, including whichever tool made that import).
//
// The split this enforces: a tool may only ever CHANGE JOB STATE (create a
// row, cancel one, queue a resume) and emit on jobEvents — it can never
// itself start a worker. orchestrator.js's tick is the ONLY thing that ever
// calls worker.js's driveJob(), whether for a brand new `queued` row or one
// re-queued here with a `resumeNote` attached. A missed jobEvents emission
// only costs latency until the next tick, never correctness — same
// reasoning as server/monitor/engine.js's monitorEvents two-channel split.

import * as jobStore from './job-store.js';
import { jobEvents } from './job-events.js';
import { hasCapacity } from './job-policy.js';
import { getPrefs } from '../prefs.js';

function nowIso() {
  return new Date().toISOString();
}

function assertCapacityForResume() {
  const active = jobStore.listActiveJobs();
  const prefs = getPrefs();
  if (!hasCapacity(active, prefs.maxBackgroundJobs)) {
    throw new Error(
      `Already at capacity (${active.length} of ${prefs.maxBackgroundJobs} background jobs running) — stop one or wait for one to finish first.`
    );
  }
}

/** Whether a new background job could start right now, without spending anything (e.g. an admission model call) on one that won't be admitted. */
export function hasBackgroundCapacityNow() {
  return hasCapacity(jobStore.listActiveJobs(), getPrefs().maxBackgroundJobs);
}

/** The running list to show the owner when at capacity — used by both createJobIfCapacity's own atCapacity reply and a tool wanting to say what's already running. */
export function runningSummaries() {
  return jobStore.listActiveJobs().map((j) => ({ id: j.id, title: j.title, status: j.status }));
}

/**
 * The capacity-gated entry point for creating a job — used by /api/jobs and
 * by work_in_background.js, so the two can never diverge on what "at
 * capacity" means. Never silently queues past the limit: returns
 * `{ok:true, atCapacity:true, running}` instead of creating a row at all,
 * so the caller can tell the owner what's already running and ask how to
 * prioritize — an explicit requirement, not a nice-to-have.
 *
 * `kind: 'computer'` is special-cased: it never starts unattended. Root
 * CLAUDE.md's Jobs section is explicit that an outward-facing, hard-to-undo
 * action needs the owner, and autonomously operating the real desktop is
 * exactly that — so a fresh computer job is parked straight into
 * `awaiting_decision` with a Tier-1 outbox row asking to start, rather than
 * `queued`. resumeStuckJob() (the SAME "keep trying" mechanism every other
 * stuck-job resolution already uses) is what actually starts it once the
 * owner says go — no separate confirm-token machinery needed. `resource`
 * defaults to 'computer' for this kind so job-policy.js's resourceAvailable()
 * serializes it against any other computer-kind job, on top of
 * control/session.js's own independent activeSession singleton.
 *
 * `parentId` — set only by server/tools/request_job_split.js when a split is
 * approved. ALWAYS the ROOT ancestor's id, never an intermediate job's own
 * id: a level-2 job requesting a further split creates PEERS under the same
 * root, not a level-3 child (see request_job_split.js's own comment) — the
 * tree structurally cannot exceed depth 2, since parentId is never chained.
 */
export function createJobIfCapacity({ title, goal, kind = 'generic', resource = null, conversationId = null, plan = null, parentId = null }) {
  const active = jobStore.listActiveJobs();
  if (!hasCapacity(active, getPrefs().maxBackgroundJobs)) {
    return { ok: true, atCapacity: true, running: active.map((j) => ({ id: j.id, title: j.title, status: j.status })) };
  }
  const effectiveResource = resource ?? (kind === 'computer' ? 'computer' : null);
  const job = jobStore.createJob({ title, goal, kind, resource: effectiveResource, conversationId, plan, parentId });

  if (kind === 'computer') {
    jobStore.updateJob(job.id, { status: 'awaiting_decision' });
    jobStore.addOutboxEntry(job.id, {
      tier: 1,
      reason: 'permission',
      summary: `I'd like to control your computer to work on "${job.title}" in the background: ${plan?.summary || goal}. OK to start?`,
    });
  }

  const finalJob = jobStore.getJob(job.id);
  jobEvents.emit('status', { jobId: finalJob.id, status: finalJob.status, title: finalJob.title });
  return { ok: true, job: finalJob };
}

/**
 * Cancels a job outright — works regardless of current status (queued,
 * running, awaiting_decision, orphaned), always allowed regardless of
 * classification (the owner can always choose to walk away from something).
 * A `running` job's own worker loop notices the status changed out from
 * under it at its next step boundary (worker.js's own liveCheck) and stops
 * there rather than overwriting this. Any pending outbox entries are marked
 * delivered so a cancelled job's old decision never resurfaces on
 * prompt.js's jobsSection() again.
 */
export function cancelJob(jobId) {
  const job = jobStore.getJob(jobId);
  if (!job) throw new Error('Unknown job.');
  jobStore.updateJob(jobId, { status: 'cancelled', finishedAt: nowIso() });
  for (const entry of jobStore.getOutboxForJob(jobId)) {
    if (!entry.deliveredAt) jobStore.markOutboxDelivered(entry.id);
  }
  jobEvents.emit('status', { jobId, status: 'cancelled', title: job.title });
  return jobStore.getJob(jobId);
}

/**
 * Resumes a job parked `awaiting_decision` for `reason:'stuck'` (a stall or
 * hang that survived its one automatic retry, or the model's own
 * report_job_stuck) — as opposed to an `orphaned` job from a real crash,
 * which orchestrator.js's own resumeOrphan()/restartOrphan() still handle
 * directly (route-only, never called from a tool, so they're free to call
 * worker.js's driveJob() themselves without crossing this leaf boundary).
 *
 * This function only touches the database: sets status back to `queued`
 * with `resumeNote` carrying the owner's own guidance (or a generic
 * fallback), resets `retries` to 0 (a human just intervened — a fresh
 * automatic-recovery budget, not the one already spent reaching this
 * point), and marks this job's pending outbox entries delivered. The
 * ORCHESTRATOR's tick is what actually resumes the worker, reading
 * `resumeNote` back out and clearing it — see orchestrator.js's tick().
 */
export function resumeStuckJob(jobId, guidance) {
  const job = jobStore.getJob(jobId);
  if (!job || job.status !== 'awaiting_decision') {
    throw new Error('That job is not currently waiting on a decision.');
  }
  assertCapacityForResume();
  // A job that never actually started yet (kind:'computer' asking for its
  // initial go-ahead — see createJobIfCapacity) gets "approved, go ahead"
  // phrasing; one that already ran and got stuck gets "try something
  // different" phrasing. Both are the same mechanism, just worded honestly
  // for what's actually being resumed.
  const note = guidance
    ? `The owner says: ${guidance}`
    : job.startedAt
      ? 'The owner says to keep trying — try a genuinely different approach than before.'
      : 'The owner has approved this — go ahead and start.';
  jobStore.appendTrace(jobId, { phase: 'outcome', effect: 'read', kind: 'decision', summary: `Resuming after the owner's input: ${note}` });
  jobStore.updateJob(jobId, { retries: 0, status: 'queued', resumeNote: note });
  for (const entry of jobStore.getOutboxForJob(jobId)) {
    if (!entry.deliveredAt) jobStore.markOutboxDelivered(entry.id);
  }
  jobEvents.emit('status', { jobId, status: 'queued', title: job.title });
  return jobStore.getJob(jobId);
}

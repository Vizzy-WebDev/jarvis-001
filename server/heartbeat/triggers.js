// The event-driven half of Heartbeat — reacts to things HAPPENING (a job
// newly needing the owner's decision) rather than waiting for the next
// scheduled poll, while feeding the exact same pipeline (engine.js's
// routeFinding()) the tick uses. One pipeline, two entry points — never a
// second one.
//
// Subscribes to jobs/job-events.js's existing `jobEvents` EventEmitter — a
// SECOND listener alongside orchestrator.js's own and improvement/cycle.js's
// own; this emitter already supports multiple subscribers by design (see
// job-store.js's/orchestrator.js's own header comments on the two-channel
// pattern this mirrors).
//
// Deliberately does NOT react to every status transition — 'done'/'failed'/
// 'cancelled' already get a real addNotification() directly from
// worker.js/orchestrator.js (Jobs' own existing ambient channel); this only
// reacts to 'awaiting_decision', the one transition that means a fresh
// Tier 1 outbox row was very likely just created and is otherwise sitting
// silent until the user happens to start a conversation — exactly the gap
// jobs-source.js's own polling path exists to close, just without waiting
// up to that source's own check interval.

import { jobEvents } from '../jobs/job-events.js';
import { source as jobsSource } from './sources/jobs-source.js';
import { routeFinding } from './engine.js';
import * as scheduleStore from './schedule-store.js';

let subscribed = false;

async function onJobStatus({ jobId, status }) {
  if (status !== 'awaiting_decision') return;
  try {
    // Ensures a schedule row exists (harmless no-op if one already does)
    // and persists whatever checkState this check produces — without this,
    // a job's own dedup memory (see jobs-source.js's own header comment on
    // why it exists) would never actually get written by the TRIGGER path,
    // only by the regular tick, letting one redundant re-fire slip through
    // ~CHECK_INTERVAL_MS later even though nothing had changed.
    scheduleStore.upsertItem(jobsSource.id, jobId, jobsSource.defaultIntervalMs);
    const { finding, checkState } = await jobsSource.check(jobId);
    scheduleStore.markDone(`${jobsSource.id}:${jobId}`, checkState !== undefined ? { checkState } : undefined);
    if (finding) await routeFinding('jobs', jobId, finding);
  } catch (err) {
    console.error(`[heartbeat] trigger check failed for job "${jobId}":`, err);
  }
}

/** Idempotent — a second call is a no-op, same guard shape as orchestrator.js's own startOrchestrator(). Call once from heartbeat/index.js. */
export function startTriggers() {
  if (subscribed) return;
  jobEvents.on('status', (payload) => {
    onJobStatus(payload).catch((err) => console.error('[heartbeat] trigger handler failed:', err));
  });
  subscribed = true;
}

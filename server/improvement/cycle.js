// The Self-Improvement background cycle — the only non-leaf top-level
// module in server/improvement/. Two jobs today: (1) capture a job's
// terminal status the moment it happens, via the SAME two-channel
// jobEvents pattern server/jobs/orchestrator.js already uses (subscribe
// directly to jobs/job-events.js, a pure leaf — never orchestrator.js
// itself, so this file stays safe even though nothing here currently needs
// that safety); (2) sweep for `orphaned` jobs on each tick, since a crash
// classification never emits on jobEvents at all (see
// jobs/orchestrator.js's recoverOrphans() — it writes the row directly with
// no jobEvents.emit call).
//
// Reflection and synthesis (the model-calling half of this subsystem) run
// here too, each independently gated on its own cadence/count floor AND
// the shared daily budget (see reflect.js's shouldReflect()/
// synthesize.js's shouldSynthesize(), improvement-store.js's
// tryConsumeDailyBudget()) — a quiet tick where neither gate passes costs
// nothing beyond the free capture sweep above. Research and life-pattern
// noticing (the weekly-budgeted half) are NOT here yet.

import { jobEvents } from '../jobs/job-events.js';
import * as jobStore from '../jobs/job-store.js';
import { recordJobOutcome } from './capture.js';
import { maybeReflect } from './reflect.js';
import { maybeSynthesize } from './synthesize.js';
import { maybeResearch } from './improve-research.js';
import { maybeNoticeLifePatterns } from './life-patterns.js';

// 15 minutes — matches the cadence the design's own cost analysis is built
// on (96 ticks/day). Everything gated behind this tick is either free
// (capture) or independently budget/cadence-gated (reflection etc., once
// added) — the tick interval itself spends nothing.
const TICK_MS = 15 * 60 * 1000;

let tickHandle = null;
let subscribed = false;

function safeRecordJobOutcome(jobId) {
  try {
    recordJobOutcome(jobId);
  } catch (err) {
    console.error(`[improvement] failed to record outcome for job "${jobId}":`, err);
  }
}

/** Anything still `orphaned` gets swept every tick — idempotent (improvement-store.js's UNIQUE(source, source_ref) on job.id), so re-sweeping a job already captured is a harmless no-op, not a growing cost. */
function sweepOrphans() {
  for (const job of jobStore.listJobs({ status: 'orphaned' })) {
    safeRecordJobOutcome(job.id);
  }
}

async function tick() {
  sweepOrphans();
  // Reflection first, synthesis second — synthesis reads the lesson set
  // reflection just added to, so running them in this order within the
  // SAME tick lets a busy day's fresh lessons feed a same-day synthesis
  // pass rather than always waiting for the next tick. Each independently
  // decides whether it's actually worth spending its call; most ticks,
  // most days, both no-op.
  try {
    await maybeReflect();
  } catch (err) {
    console.error('[improvement] reflection pass failed:', err);
  }
  try {
    await maybeSynthesize();
  } catch (err) {
    console.error('[improvement] synthesis pass failed:', err);
  }
  // Weekly-budgeted, cadence-gated independently of reflect/synthesize
  // above (their own shouldResearch()/shouldNotice() hold the real "not
  // more than once a week" floor) — checking every 15-minute tick costs
  // nothing on a week that hasn't earned another pass yet.
  try {
    await maybeResearch();
  } catch (err) {
    console.error('[improvement] outside research pass failed:', err);
  }
  try {
    await maybeNoticeLifePatterns();
  } catch (err) {
    console.error('[improvement] life-pattern pass failed:', err);
  }
}

/**
 * Starts the cycle: subscribes to jobEvents once (idempotent — a second
 * call is a no-op, same guard shape as orchestrator.js's own
 * startOrchestrator), sweeps once immediately, then ticks every TICK_MS.
 * Call once from server.js, after the HTTP server is listening, beside
 * startScheduler()/startOrchestrator().
 */
export function startImprovementCycle() {
  if (!subscribed) {
    jobEvents.on('status', ({ jobId, status }) => {
      if (status === 'done' || status === 'failed' || status === 'cancelled') {
        safeRecordJobOutcome(jobId);
      }
      // 'orphaned' never reaches this listener — see this file's header
      // comment; sweepOrphans() above is what catches it, every tick.
    });
    subscribed = true;
  }
  if (tickHandle) return;
  tick().catch((err) => console.error('[improvement] initial tick failed:', err));
  tickHandle = setInterval(() => {
    tick().catch((err) => console.error('[improvement] tick failed:', err));
  }, TICK_MS);
}

/** Test-only escape hatch — stops the interval so a scratch test process can exit cleanly. Production code never calls this. */
export function _stopForTests() {
  if (tickHandle) clearInterval(tickHandle);
  tickHandle = null;
}

// Entry point — startHeartbeat() is called once from server.js, beside
// startScheduler()/startOrchestrator()/startImprovementCycle(). Named for
// the user's own term for this mechanism; unrelated to jobs/job-store.js's
// `heartbeat_at` column, which is worker-liveness tracking for a single
// running job, a different concept entirely — see root CLAUDE.md's
// Heartbeat section.

import * as scheduleStore from './schedule-store.js';
import { registerSource } from './sources/registry.js';
import { source as jobsSource } from './sources/jobs-source.js';
import { source as commitmentsSource } from './sources/commitments-source.js';
import { startTick } from './engine.js';
import { startTriggers } from './triggers.js';

let started = false;

export function startHeartbeat() {
  if (started) return;
  started = true;

  // A `running=1` row left over from a hard crash mid-check must never
  // wedge that item forever — this is the ONE place this is ever cleared
  // without a matching markDone() (see schedule-store.js's own comment).
  scheduleStore.resetStaleRunning();

  registerSource(jobsSource);
  registerSource(commitmentsSource);

  startTriggers();
  startTick();
}

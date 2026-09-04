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
import { source as environmentSource } from '../ops/environment/source.js';
import { registerDiagnosticChecks } from '../ops/diagnostics/index.js';
import { source as diagnosisSource } from '../ops/diagnostics/source.js';
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
  // Environment awareness (root CLAUDE.md's Operational Awareness item 5) —
  // "is system load unusually high or climbing without a clear cause." Same
  // registerSource() plug-in surface as the two above; see
  // server/ops/environment/source.js for its own checkState dedup.
  registerSource(environmentSource);
  // Self-diagnosis (root CLAUDE.md's Operational Awareness item 1) — real
  // checks registered first so the 'diagnosis' source has something to run
  // on its very first tick; see server/ops/diagnostics/source.js for its
  // own retry-then-escalate self-heal.
  registerDiagnosticChecks();
  registerSource(diagnosisSource);

  startTriggers();
  startTick();
}

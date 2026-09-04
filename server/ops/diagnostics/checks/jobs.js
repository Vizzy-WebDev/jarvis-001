// Are background Jobs actually progressing, or has the Orchestrator's own
// 5-second tick silently stopped? A `queued` job that COULD run right now
// (capacity available, its resource free) but has sat queued far longer
// than any real tick interval is the honest signal — not a raw "is
// anything queued," since a job legitimately waiting on capacity or a busy
// resource is correct behavior, not a malfunction. Reuses job-policy.js's
// own hasCapacity()/resourceAvailable() rather than re-deriving the same
// logic a second time.

import * as jobStore from '../../../jobs/job-store.js';
import { hasCapacity, resourceAvailable } from '../../../jobs/job-policy.js';
import { getPrefs } from '../../../prefs.js';

export const id = 'jobs-progressing';
// Generous relative to the Orchestrator's own 5s tick — many multiples of
// it, so this only fires on genuine silence, never a normal scheduling lag.
const STUCK_THRESHOLD_MS = 3 * 60 * 1000;

export async function probe() {
  const active = jobStore.listActiveJobs();
  const queued = jobStore.listJobs({ status: 'queued' });
  const maxJobs = getPrefs().maxBackgroundJobs;

  const stuckRunnable = queued.filter((job) => {
    const ageMs = Date.now() - new Date(job.createdAt).getTime();
    if (ageMs < STUCK_THRESHOLD_MS) return false;
    // Only a genuine "could run right now but isn't" counts — capacity AND
    // its own resource must both be free, or this is correct blocking, not
    // a malfunction.
    return hasCapacity(active, maxJobs) && resourceAvailable(active, job.resource);
  });

  if (stuckRunnable.length) {
    return {
      ok: false,
      detail: `${stuckRunnable.length} job(s) have been queued for over ${Math.round(STUCK_THRESHOLD_MS / 60000)} minutes with capacity and resources free — the Orchestrator's own tick may have stopped: ${stuckRunnable.map((j) => j.title).join(', ')}.`,
    };
  }
  return { ok: true };
}

// No remedy() — if the Orchestrator's own process-level tick has actually
// stopped, nothing running inside a Heartbeat check (itself dependent on
// the same process being alive) could restart it; this needs the owner to
// notice and restart Jarvis.

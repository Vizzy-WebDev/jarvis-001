// Is the Scheduler's own 30s tick actually running? An ENABLED task whose
// `nextRunAt` sits well in the past — many multiples of the tick interval —
// means the tick loop itself has stopped, not that any one task failed
// (a task's own run failure is already surfaced by scheduler.js's own
// notify() path; this is specifically about the loop that's supposed to be
// checking at all).

import { listTasks } from '../../../scheduler/task-store.js';

export const id = 'scheduler-ticking';
const OVERDUE_THRESHOLD_MS = 5 * 60 * 1000; // many multiples of the real 30s tick

export async function probe() {
  const overdue = listTasks().filter((t) => {
    if (!t.enabled || !t.nextRunAt) return false;
    return Date.now() - new Date(t.nextRunAt).getTime() > OVERDUE_THRESHOLD_MS;
  });

  if (overdue.length) {
    return {
      ok: false,
      detail: `${overdue.length} enabled task(s) are overdue by more than ${Math.round(OVERDUE_THRESHOLD_MS / 60000)} minutes — the Scheduler's own tick may have stopped: ${overdue.map((t) => t.title).join(', ')}.`,
    };
  }
  return { ok: true };
}

// No remedy() — same reasoning as jobs.js: if the process-level tick has
// actually stopped, nothing running inside this same process can restart
// it.

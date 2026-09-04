// Is the Self-Model's own capability recorder (self/self-capture.js's
// recordAttempt()) actually working? Reuses self-store.js's
// captureHealthSummary() directly rather than re-deriving the same
// capture_health rollup a second time — that table already exists
// specifically to tell apart "a broken recorder" from "a tool genuinely
// never used" (see root CLAUDE.md's Self-Model section). A HIGH FAILURE
// RATE here (not just >0 failures — a single transient failure is normal)
// is the honest signal that something is systemically wrong with the
// recorder itself, not with whatever it's recording.

import { captureHealthSummary } from '../../../self/self-store.js';

export const id = 'capture-health';
const MIN_ATTEMPTS_FOR_SIGNAL = 5; // below this, a rate is meaningless noise
const FAILURE_RATE_THRESHOLD = 0.5; // a MAJORITY failing is systemic, not incidental

export async function probe() {
  const summary = captureHealthSummary();
  if (summary.attempts24h < MIN_ATTEMPTS_FOR_SIGNAL) return { ok: true }; // not enough real data to say anything honest

  const rate = summary.failures24h / summary.attempts24h;
  if (rate > FAILURE_RATE_THRESHOLD) {
    return {
      ok: false,
      detail: `The Self-Model's own capability recorder has failed ${summary.failures24h} of ${summary.attempts24h} attempts in the last 24h (last failure: ${summary.lastFailureSource || 'unknown source'} at ${summary.lastFailureAt || 'unknown time'}).`,
    };
  }
  return { ok: true };
}

// No remedy() — a systemically failing recorder needs the owner to look at
// what changed (a schema issue, a disk problem), not an automatic retry.

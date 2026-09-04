// Security check #2: a SPIKE of auth failures or confirm-gate-bypass
// attempts — the two real signals this build currently has a hook for (see
// security-counters.js's own callers: models/health.js on a real auth
// failure, capabilities.js on a same-turn confirm-token refusal).
// Deliberately a fixed count-in-window threshold, not a rolling-median
// comparison like environment/baseline.js's own CPU check — a genuinely
// rare discrete event (an auth failure, a bypass attempt) has no meaningful
// "typical median" to compare against the way continuous CPU load does;
// "more than a small handful in a short window" is the honest definition
// of a spike for this kind of signal, not a cop-out simplification.
//
// Detection only, per the owner's own explicit scope — no remedy(), a
// finding always escalates straight to the owner.

import { countRecent } from './security-counters.js';

const WINDOW_MS = 15 * 60 * 1000;
const AUTH_FAILURE_THRESHOLD = 5;
const CONFIRM_REFUSAL_THRESHOLD = 3;

export const id = 'security-event-spikes';

export async function probe() {
  const authFailures = countRecent('auth_failure', WINDOW_MS);
  const confirmRefusals = countRecent('confirm_refusal', WINDOW_MS);

  const findings = [];
  if (authFailures >= AUTH_FAILURE_THRESHOLD) {
    findings.push(`${authFailures} model auth failures in the last ${Math.round(WINDOW_MS / 60000)} minutes`);
  }
  if (confirmRefusals >= CONFIRM_REFUSAL_THRESHOLD) {
    findings.push(`${confirmRefusals} same-turn confirm-gate bypass attempts in the last ${Math.round(WINDOW_MS / 60000)} minutes`);
  }

  if (findings.length) {
    return { ok: false, detail: findings.join('; ') + '.' };
  }
  return { ok: true };
}

// Deliberately NO remedy() — detection only.

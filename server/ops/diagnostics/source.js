// The Heartbeat source that actually runs registered diagnostic checks —
// same registerSource({id, defaultIntervalMs, listItems, check}) contract
// every other source uses. This is where retry-then-escalate (the SAME
// shape Jobs already built — one automatic attempt, then tell the owner
// plainly) gets applied to self-diagnosis specifically.
//
// **One remedy attempt per NEW failure occurrence, never per tick** — the
// same "canAutoRetry: retries===0" discipline job-policy.js already
// established, adapted here via checkState.lastOutcome instead of a
// numeric retry counter: a check that's ALREADY marked 'failing' from the
// previous tick has already had its one remedy shot (or had none to try),
// so a still-failing check on tick N+1 does not retry again and does not
// re-report — only a genuine ok->failing TRANSITION triggers the
// retry-then-escalate sequence. Own-checkState dedup, same discipline
// heartbeat/CLAUDE.md documents at length as a real, previously-live bug
// (jobs-source.js) — required here for the identical reason: a sustained
// failure must not re-spend a decision.js model call and re-notify on
// every single tick forever.
//
// Every probe/remedy/outcome writes a real ops_trace row
// (source:'diagnosis', sourceRef: check.id) — this is what makes "has
// anything gone wrong with you lately?" a real, queryable answer rather
// than a claim with nothing behind it.

import { listChecks, getCheck } from './registry.js';
import { getItem as getScheduleItem } from '../../heartbeat/schedule-store.js';
import { appendEntry as appendTrace } from '../ops-trace.js';

export const SOURCE_ID = 'diagnosis';
const CHECK_INTERVAL_MS = 5 * 60 * 1000; // cheap probes only — real network/DB reads, no model call of their own

export async function listItems() {
  return listChecks().map((c) => ({ itemKey: c.id, intervalMs: CHECK_INTERVAL_MS }));
}

function traceIntent(checkId, kind, summary, detail) {
  try {
    appendTrace({ source: 'diagnosis', sourceRef: checkId, phase: 'intent', effect: 'read', kind, summary, detail });
  } catch (err) {
    console.error(`[ops] diagnosis trace (intent) failed for "${checkId}":`, err);
  }
}
function traceOutcome(checkId, kind, summary, detail) {
  try {
    appendTrace({ source: 'diagnosis', sourceRef: checkId, phase: 'outcome', effect: 'read', kind, summary, detail });
  } catch (err) {
    console.error(`[ops] diagnosis trace (outcome) failed for "${checkId}":`, err);
  }
}

export async function check(itemKey) {
  const c = getCheck(itemKey);
  if (!c) return { finding: null, checkState: null }; // check was un-registered since this item was scheduled — nothing to report

  const prior = getScheduleItem(SOURCE_ID, itemKey)?.checkState;
  const wasFailing = prior?.lastOutcome === 'failing';

  traceIntent(itemKey, 'probe', `Running diagnostic check: ${c.id}`);
  let result;
  try {
    result = await c.probe();
  } catch (err) {
    result = { ok: false, detail: `Probe threw: ${err?.message || err}` };
  }
  traceOutcome(itemKey, 'probe', result.ok ? 'Probe passed.' : `Probe failed: ${result.detail || 'no detail given'}`, result.detail || null);

  if (result.ok) {
    if (wasFailing) {
      // Recovered — either self-healed by a remedy on a prior tick, or
      // fixed some other way. Worth a trace row either way; not worth
      // interrupting the owner about a problem that's already gone.
      traceOutcome(itemKey, 'note', `"${c.id}" recovered.`);
    }
    return { finding: null, checkState: { lastOutcome: 'ok' } };
  }

  // Failing. Only a genuine NEW occurrence gets a remedy attempt — a check
  // still failing from a previous tick already had its one shot.
  if (wasFailing) {
    return { finding: null }; // already attempted-and/or-reported this exact ongoing failure — checkState left untouched
  }

  if (typeof c.remedy === 'function') {
    traceIntent(itemKey, 'remedy', `Attempting the one automatic remedy for "${c.id}".`);
    try {
      await c.remedy();
    } catch (err) {
      traceOutcome(itemKey, 'remedy', `Remedy threw: ${err?.message || err}`);
    }
    let reprobe;
    try {
      reprobe = await c.probe();
    } catch (err) {
      reprobe = { ok: false, detail: `Re-probe threw: ${err?.message || err}` };
    }
    if (reprobe.ok) {
      traceOutcome(itemKey, 'remedy', `"${c.id}" self-healed after one remedy attempt.`);
      return { finding: null, checkState: { lastOutcome: 'ok' } };
    }
    traceOutcome(itemKey, 'decision', `"${c.id}" still failing after the one automatic remedy attempt — escalating.`, reprobe.detail || null);
    return {
      finding: { summary: `Self-diagnosis: "${c.id}" is failing, and the one automatic remedy attempt didn't fix it.`, detail: reprobe.detail || result.detail || null },
      checkState: { lastOutcome: 'failing' },
    };
  }

  // No remedy declared — escalate straight from the failed probe.
  traceOutcome(itemKey, 'decision', `"${c.id}" is failing with no remedy available — escalating.`, result.detail || null);
  return {
    finding: { summary: `Self-diagnosis: "${c.id}" is failing.`, detail: result.detail || null },
    checkState: { lastOutcome: 'failing' },
  };
}

export const source = { id: SOURCE_ID, defaultIntervalMs: CHECK_INTERVAL_MS, listItems, check };

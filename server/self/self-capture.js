// Turns a live tool-call outcome into (a) a rolling reliability tally
// (self-store.js's self_capability_stats — dimension 2, "what can/can't it
// actually do") and, only for a NOTABLE outcome, (b) one more entry into
// Self-Improvement's existing pipeline (improvement-store.js's
// improvement_outcomes, source:'turn') so reflect.js's own backlog sees it
// too. ZERO model calls anywhere in this file, on purpose — same discipline
// as improvement/capture.js, which this sits directly beside: capture must
// never cost quota, since it runs on every tool call in every live turn.
//
// Leaf-adjacent: imports self-store.js (leaf) and improvement/
// improvement-store.js (leaf) only — safe for models/runner.js's own
// per-step loop to call, same as improvement/capture.js's noteCorrection().
//
// This is the ONE place this build writes into Self-Improvement's pipeline
// — and it feeds the EXISTING pipeline (recordOutcome), never a second one.
// See root CLAUDE.md's Self-Model section: dimensions 3/7 stay a read-only
// VIEW over improvement_lessons/improvement_rules; this file is the other
// direction — new signal flowing IN, still through the one door.

import { recordAttempt, recordCaptureHealth } from './self-store.js';
import { recordOutcome } from '../improvement/improvement-store.js';

/**
 * Called once per tool_result, right where models/runner.js already yields
 * that event. `ok` follows the same rule every caller in this codebase
 * uses: `result.ok !== false`, never `!result.ok` (server/tools/CLAUDE.md —
 * a tool with nothing to report on success, like get_time, has no `ok`
 * field at all, and a bare `!result.ok` check would wrongly read that as
 * failure). `notAllowed` is runner.js's own `opts.allowedTools` refusal
 * (the call never reached invoke() at all); `escalated` is
 * capabilities.js's third confirm mode parking the decision
 * (result.escalated === true) — both count as notable even though neither
 * is a plain failure the model caused.
 *
 * Every call bumps the rolling tally (recordAttempt) — that's dimension 2's
 * whole grounding, and it must never be skipped just because an outcome was
 * routine. Only a notable one ALSO becomes an improvement_outcomes row, so
 * reflect.js's own backlog stays signal, not a flood of ordinary successes.
 *
 * `recordAttempt()` is wrapped in its OWN local try/catch, not just left to
 * whatever wrapper the caller happens to have — found in a real audit: it
 * used to run unguarded here, so a throw from self-store.js's SQLite write
 * propagated straight out, was only ever caught one level up in
 * runner.js's own wrapper, and left NOTHING on record beyond a
 * console.error — a broken recorder and a tool genuinely never used were
 * indistinguishable from every self-model dimension reading
 * self_capability_stats. Either branch now also logs to
 * self-store.js's capture_health table (recordCaptureHealth) — the health
 * of the SENSOR, not what it measures — surfaced via check_myself's
 * can_do dimension (self-model.js's whatItCanDo()) so the model can tell
 * "never used" apart from "recorder is broken" instead of silently
 * assuming the recorder is fine.
 */
export function recordToolOutcome({ name, ok, notAllowed = false, escalated = false, errorText = null }) {
  if (!name) return;

  try {
    recordAttempt('tool', name, ok);
    recordCaptureHealth({ source: 'tool', name, ok: true });
  } catch (err) {
    console.error('[self-capture] recordAttempt failed — the capability tally itself did not update:', err);
    try {
      recordCaptureHealth({ source: 'tool', name, ok: false, errorMessage: err?.message || String(err) });
    } catch (healthErr) {
      // A health-log write failing must never cascade into anything else
      // breaking — same discipline as every other capture-path guard in
      // this file. If this specific line fails, the gap is genuinely
      // unrecorded; there is nothing further down to fall back to.
      console.error('[self-capture] recordCaptureHealth ALSO failed:', healthErr);
    }
  }

  const notable = ok === false || notAllowed || escalated;
  if (!notable) return;

  try {
    recordOutcome({
      source: 'turn',
      sourceRef: null, // each tool call is a genuinely new event, never a duplicate of an earlier one — same reasoning as capture.js's noteCorrection()
      entityRef: name, // the stable key reflect.js/synthesize.js group "this tool tends to fail this way" under
      title: `Tool call: ${name}`,
      goal: null,
      kind: 'tool',
      status: 'failed', // every notable path here IS a failure signal — a plain ok:false, a refused allowlist call, or a parked escalation
      retries: 0,
      error: notAllowed ? 'Not in this turn\'s allowed tools.' : escalated ? 'Escalated — waiting on the owner.' : errorText,
      toolSummary: null,
      escalations: escalated ? 1 : 0,
    });
  } catch (err) {
    // Never let a capture-path failure break the turn that triggered it —
    // same discipline as improvement/capture.js's own callers wrapping
    // noteCorrection() in try/catch.
    console.error('[self-capture] recordToolOutcome failed:', err);
  }
}

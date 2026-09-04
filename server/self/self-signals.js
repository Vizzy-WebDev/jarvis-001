// Self-Model trigger detection — pure, deterministic, zero model calls, zero
// imports. Same discipline as personality.js's detectFloors()/
// jobs/job-policy.js's classifyRecovery(): a bare `node --input-type=module
// -e "..."` script can exercise this file with no server running.
//
// This module decides WHEN the self-model is worth consulting this turn —
// never WHAT it says. It takes only plain data (booleans, small arrays) that
// a caller (models/runner.js) has already computed from real domain state,
// so it never needs to import job-store.js/memory-store.js/etc itself and
// stays leaf-safe by construction, not by convention.
//
// See root CLAUDE.md's "Self-Model" section for the hybrid trigger design:
// passive by default, consulted only when one of these actually fires.

/**
 * `toolCallsThisTurn`: [{name, ok, needsConfirmation}] — this turn's own
 *   tool_result events, in the shape models/runner.js already has.
 * `activeJobs`: [{status}] — job-store.js's listActiveJobs() result; only
 *   `status` is read here.
 * `pendingMemoryConflict`: boolean — whether a memory conflict is currently
 *   awaiting the user (memory-store.js already tracks this; the caller
 *   resolves the boolean, this file never touches that store).
 * `correctionDetected`: boolean — the caller's own noteCorrection() call
 *   already ran this turn's regex; this is just that result, reused rather
 *   than re-matched, so the pattern list has exactly one home
 *   (improvement/capture.js's CORRECTION_PATTERNS).
 * `scopesInPlay`: string[] — the improvement-store.js `scope` strings
 *   relevant to what THIS turn is actually doing (e.g. 'tool:run_code').
 * `activeLessonScopes`: string[] — scopes carried by currently-active
 *   lessons/rules (improvement-store.js's listLessons()/listRules()).
 * `noTrackRecordChecks`: [{axis, key, attempts}] — self-store.js stats the
 *   caller already looked up for whatever this turn is about to attempt;
 *   `attempts` is 0 (or the row didn't exist) when there's genuinely no
 *   track record yet.
 */
export function detectSelfSignals({
  toolCallsThisTurn = [],
  activeJobs = [],
  pendingMemoryConflict = false,
  correctionDetected = false,
  scopesInPlay = [],
  activeLessonScopes = [],
  noTrackRecordChecks = [],
} = {}) {
  const authority =
    toolCallsThisTurn.some((t) => t?.needsConfirmation) ||
    activeJobs.some((j) => j?.status === 'awaiting_decision') ||
    Boolean(pendingMemoryConflict);

  const lessonScopeSet = new Set(activeLessonScopes);
  const matchedScopes = scopesInPlay.filter((s) => lessonScopeSet.has(s));
  const knownFailure = matchedScopes.length > 0;

  const noTrackRecordKeys = noTrackRecordChecks.filter((c) => !c?.attempts).map((c) => ({ axis: c.axis, key: c.key }));
  const noTrackRecord = noTrackRecordKeys.length > 0;

  const blockedOnBackground = activeJobs.some((j) => ['queued', 'planning', 'running'].includes(j?.status));

  return {
    authority,
    knownFailure,
    matchedScopes,
    noTrackRecord,
    noTrackRecordKeys,
    correction: Boolean(correctionDetected),
    blockedOnBackground,
  };
}

/** True if detectSelfSignals() found anything worth surfacing this turn — the single gate prompt.js's volatile section checks before spending any tokens on it. */
export function anySignalFired(signals) {
  if (!signals) return false;
  // toolReliability (attached by self-model.js's computeTurnSignals(), not
  // computed here — this file stays zero-import either way, just checking
  // whatever's already on the object) carrying a real recorded FAILURE for
  // something used this turn is its own reason to surface — reasoning
  // integrity's "steer early" half (root CLAUDE.md's Operational Awareness
  // item 1). A clean track record never fires this; only genuine prior
  // trouble does.
  const hasNotableToolHistory = Array.isArray(signals.toolReliability) && signals.toolReliability.some((r) => r.failures > 0);
  return Boolean(
    signals.authority ||
      signals.knownFailure ||
      signals.noTrackRecord ||
      signals.correction ||
      signals.blockedOnBackground ||
      hasNotableToolHistory
  );
}

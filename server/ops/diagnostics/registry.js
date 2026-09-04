// The entire plug-in surface for a self-diagnosis check — pure, zero
// imports, same posture as heartbeat/sources/registry.js (this project's
// own proven shape for "the engine that runs checks knows nothing about
// what a check actually does"). This is what keeps the check list
// open-ended: a new failure mode to watch for is one registerCheck() call,
// never a change to source.js's own dispatch logic.

const checks = new Map();

/**
 * `id` — stable string, becomes heartbeat_schedule's item_key (via
 * source.js) and ops_trace's source_ref.
 * `probe()` — async, returns `{ ok: boolean, detail?: string }`. `detail`
 * is plain language, safe to surface directly in a finding or a trace row.
 * `remedy()` — optional, async. Best-effort — a thrown remedy is caught by
 * source.js, never by the check itself; a check with no remedy simply
 * escalates straight from a failed probe with no attempt in between.
 */
export function registerCheck(check) {
  if (!check?.id) throw new Error('A diagnostic check needs an id.');
  if (typeof check.probe !== 'function') throw new Error(`Diagnostic check "${check.id}" must implement probe()`);
  checks.set(check.id, check);
}

export function listChecks() {
  return [...checks.values()];
}

export function getCheck(id) {
  return checks.get(id) || null;
}

export function _resetForTests() {
  checks.clear();
}

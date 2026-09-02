// The entire plug-in surface for the Heartbeat — pure, zero imports, same
// discipline as personality.js's detectFloors()/self-signals.js. Adding a
// future source (calendar, business monitoring, ...) is one call to
// registerSource() from wherever that source's own module lives; nothing
// here needs to change, and nothing here knows what any source actually
// checks.
//
// A source is `{ id, defaultIntervalMs, listItems(), check(itemKey) }`:
//   - `id` — a short, stable string (e.g. 'jobs', 'commitments'), used as
//     heartbeat_schedule.source_id.
//   - `defaultIntervalMs` — how often a newly-seen item of this source's own
//     should be checked, unless the source itself gives a per-item override.
//   - `listItems()` — async, returns `[{itemKey, intervalMs?}]`, the CURRENT
//     set of things this source wants watched (dynamic — e.g. every job
//     currently holding an undelivered Tier 1 outbox row). engine.js
//     reconciles this against the persisted schedule every tick: new keys
//     get a row, keys no longer listed get pruned.
//   - `check(itemKey)` — async, returns `{finding, checkState?}`. `finding`
//     is `{summary, detail?}` or `null` (nothing to report right now).
//     `checkState` is optional free-form JSON a source wants remembered
//     between ticks for this exact item (e.g. commitments-source.js's own
//     cached parsed deadline) — omit it entirely to leave the persisted
//     value untouched; pass `null` to explicitly clear it. Read back via
//     schedule-store.js's getItem(sourceId, itemKey). Thrown errors are
//     caught by engine.js, never by the source itself — see engine.js's own
//     per-check isolation.

const sources = new Map();

export function registerSource(source) {
  if (!source?.id) throw new Error('A Heartbeat source needs an id.');
  if (typeof source.listItems !== 'function' || typeof source.check !== 'function') {
    throw new Error(`Heartbeat source "${source.id}" must implement listItems() and check()`);
  }
  sources.set(source.id, source);
}

export function listSources() {
  return [...sources.values()];
}

/** Test-only escape hatch — clears every registered source so a scratch test process can register its own fakes with no leftover state. Production code never calls this. */
export function _resetForTests() {
  sources.clear();
}

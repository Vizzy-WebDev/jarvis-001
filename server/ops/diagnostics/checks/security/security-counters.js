// A tiny, leaf-safe event counter over `ops_security_events` (db.js
// migration 18) — the one place auth-failure and confirm-gate-refusal
// counts actually get recorded, so a diagnostic check can read them
// without importing anything that isn't a leaf itself. **Written to FROM
// non-leaf callers** (models/health.js, capabilities.js) that this file
// never imports back — a one-directional dependency, same shape as
// self/self-capture.js being called from runner.js/capabilities.js without
// importing either. This is what keeps a diagnostic check reading these
// counters safe to import from server/tools/check_my_health.js: nothing in
// this file, or in anything it imports, ever touches
// tools/index.js/capabilities.js/runner.js.
//
// Timestamped events, not a running total — a "spike," for a naturally rare
// discrete event like an auth failure, means "several in a short window,"
// which a bare counter can't answer (it can only grow). Pruned to the last
// 24h on every write so this table never grows unbounded.

import { getDb } from '../../../../db.js';

const RETENTION_MS = 24 * 60 * 60 * 1000;

/** Records one real occurrence of `kind` ('auth_failure' | 'confirm_refusal') right now. Never throws — a counter write failing must never break whatever real thing it's counting. */
export function recordEvent(kind) {
  try {
    const db = getDb();
    db.prepare('INSERT INTO ops_security_events (kind, ts) VALUES (?, ?)').run(kind, new Date().toISOString());
    db.prepare('DELETE FROM ops_security_events WHERE ts < ?').run(new Date(Date.now() - RETENTION_MS).toISOString());
  } catch (err) {
    console.error(`[ops] security counter "${kind}" failed to record:`, err);
  }
}

/** How many real `kind` events landed in the last `windowMs`. */
export function countRecent(kind, windowMs) {
  const since = new Date(Date.now() - windowMs).toISOString();
  const row = getDb().prepare('SELECT COUNT(*) AS n FROM ops_security_events WHERE kind = ? AND ts >= ?').get(kind, since);
  return row?.n || 0;
}

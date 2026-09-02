// Persisted per-item due times for the Heartbeat tick (server/heartbeat/
// engine.js) — the whole reason a restart resumes correctly instead of
// resetting every item's timer to zero or firing everything overdue at once.
// Leaf module: imports only db.js, same discipline as every other
// *-store.js in this project.
//
// `id` is deterministic (`${sourceId}:${itemKey}`), never a separate
// autoincrement key — that's what makes "does this item already have a
// schedule row" a plain upsert instead of a lookup-then-decide.

import { getDb } from '../db.js';

function nowIso() {
  return new Date().toISOString();
}

function rowId(sourceId, itemKey) {
  return `${sourceId}:${itemKey}`;
}

function rowToItem(row) {
  return {
    id: row.id,
    sourceId: row.source_id,
    itemKey: row.item_key,
    intervalMs: row.interval_ms,
    nextDueAt: row.next_due_at,
    lastCheckedAt: row.last_checked_at,
    running: Boolean(row.running),
    checkState: row.check_state ? JSON.parse(row.check_state) : null,
    createdAt: row.created_at,
  };
}

/**
 * Registers (or re-registers) one item's schedule. A NEW item is scheduled
 * to be checked soon (nextDueAt: now) rather than a full interval from now —
 * something a source just started watching is worth a first look promptly.
 * An item that already has a row is left alone (its real nextDueAt is not
 * reset) UNLESS `intervalMs` actually changed, in which case only the
 * interval is updated — never the due time, so tuning a source's cadence
 * mid-flight doesn't itself trigger a burst of immediate re-checks.
 */
export function upsertItem(sourceId, itemKey, intervalMs) {
  const db = getDb();
  const id = rowId(sourceId, itemKey);
  const existing = db.prepare('SELECT * FROM heartbeat_schedule WHERE id = ?').get(id);
  if (!existing) {
    const ts = nowIso();
    db.prepare(
      `INSERT INTO heartbeat_schedule (id, source_id, item_key, interval_ms, next_due_at, running, created_at)
       VALUES (?, ?, ?, ?, ?, 0, ?)`
    ).run(id, sourceId, itemKey, intervalMs, ts, ts);
  } else if (existing.interval_ms !== intervalMs) {
    db.prepare('UPDATE heartbeat_schedule SET interval_ms = ? WHERE id = ?').run(intervalMs, id);
  }
  return rowToItem(db.prepare('SELECT * FROM heartbeat_schedule WHERE id = ?').get(id));
}

/** Items due now, oldest-due first, capped so one tick can never process an unbounded catch-up burst at once — see engine.js's own per-tick cap. */
export function listDue(now, limit) {
  const rows = getDb()
    .prepare('SELECT * FROM heartbeat_schedule WHERE next_due_at <= ? AND running = 0 ORDER BY next_due_at ASC LIMIT ?')
    .all(now, limit);
  return rows.map(rowToItem);
}

export function markRunning(id) {
  getDb().prepare('UPDATE heartbeat_schedule SET running = 1 WHERE id = ?').run(id);
}

/** Advances an item past this check — called in a `finally`, success or failure alike, so a throwing check() still moves the schedule forward instead of retry-storming. */
export function markDone(id, { checkState } = {}) {
  const db = getDb();
  const row = db.prepare('SELECT interval_ms FROM heartbeat_schedule WHERE id = ?').get(id);
  if (!row) return;
  const ts = nowIso();
  const nextDue = new Date(Date.now() + row.interval_ms).toISOString();
  const sets = ['running = 0', 'last_checked_at = ?', 'next_due_at = ?'];
  const params = [ts, nextDue];
  if (checkState !== undefined) {
    sets.push('check_state = ?');
    params.push(checkState === null ? null : JSON.stringify(checkState));
  }
  params.push(id);
  db.prepare(`UPDATE heartbeat_schedule SET ${sets.join(', ')} WHERE id = ?`).run(...params);
}

/** Startup-only: a `running=1` row left over from a hard crash mid-check must never wedge that item forever — this is the one place that's ever allowed to clear it without a matching markDone(). Never called mid-process. */
export function resetStaleRunning() {
  getDb().prepare('UPDATE heartbeat_schedule SET running = 0 WHERE running = 1').run();
}

/** Drops schedule rows for items a source no longer lists (e.g. an archived memory, a job whose outbox row was resolved another way) — keeps the table from growing forever with dead items. */
export function pruneRemoved(sourceId, validItemKeys) {
  const db = getDb();
  const rows = db.prepare('SELECT item_key FROM heartbeat_schedule WHERE source_id = ?').all(sourceId);
  const valid = new Set(validItemKeys);
  const del = db.prepare('DELETE FROM heartbeat_schedule WHERE id = ?');
  for (const row of rows) {
    if (!valid.has(row.item_key)) del.run(rowId(sourceId, row.item_key));
  }
}

export function listForSource(sourceId) {
  return getDb().prepare('SELECT * FROM heartbeat_schedule WHERE source_id = ?').all(sourceId).map(rowToItem);
}

/** One item's own schedule row — what a source's check() reads to recall its own prior checkState (e.g. commitments-source.js's cached parsed deadline) between ticks. */
export function getItem(sourceId, itemKey) {
  const row = getDb().prepare('SELECT * FROM heartbeat_schedule WHERE id = ?').get(rowId(sourceId, itemKey));
  return row ? rowToItem(row) : null;
}

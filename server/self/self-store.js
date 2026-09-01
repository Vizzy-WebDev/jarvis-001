// Self-Model persistence — a rolling per-axis reliability tally
// (self_capability_stats) and a record of what Jarvis declared it
// understood a live conversation's goal to be (self_goals). Built on
// db.js's SQLite connection, same as chat-store.js / memory/memory-store.js
// / jobs/job-store.js / improvement/improvement-store.js.
//
// Leaf module: imports only db.js (itself a leaf) — safe for server/tools/
// and prompt.js to import directly, without tripping the loader/runner
// circular-import invariant (root CLAUDE.md).
//
// Deliberately holds NO prose knowledge and NO facts about the user —
// everything content-shaped (lessons, patterns, corrections) keeps going
// through improvement/improvement-store.js's existing tables. This module
// only ever stores counters and a bare goal statement, which is why it does
// not count as the "second memory-like store" the Self-Model build was told
// never to create. See server/self/CLAUDE.md.

import { getDb } from '../db.js';

function makeId(prefix) {
  return `${prefix}${Date.now().toString(36)}${Math.random().toString(36).slice(2, 8)}`;
}

function nowIso() {
  return new Date().toISOString();
}

// ---------- capability stats ----------

const VALID_AXES = new Set(['tool', 'job_kind', 'task_type']);

function rowToStat(row) {
  return {
    axis: row.axis,
    key: row.key,
    attempts: row.attempts,
    failures: row.failures,
    lastOkAt: row.last_ok_at,
    lastFailedAt: row.last_failed_at,
    updatedAt: row.updated_at,
  };
}

/**
 * Bumps the rolling tally for one (axis, key) — 'tool' + a tool name,
 * 'job_kind' + a job kind, or 'task_type' + a scheduled task's action type.
 * `ok` is the SAME test every other caller in this codebase uses:
 * `result.ok !== false`, never `!result.ok` (see server/tools/CLAUDE.md — a
 * tool with nothing to report on success, like get_time, returns no `ok`
 * field at all). No model call, no throw on an unknown axis — an unrecognized
 * axis is silently ignored rather than crashing a turn's own capture hook.
 */
export function recordAttempt(axis, key, ok) {
  if (!VALID_AXES.has(axis) || !key) return;
  const db = getDb();
  const ts = nowIso();
  db.prepare(
    `INSERT INTO self_capability_stats (axis, key, attempts, failures, last_ok_at, last_failed_at, updated_at)
     VALUES (?, ?, 1, ?, ?, ?, ?)
     ON CONFLICT(axis, key) DO UPDATE SET
       attempts = attempts + 1,
       failures = failures + excluded.failures,
       last_ok_at = COALESCE(excluded.last_ok_at, self_capability_stats.last_ok_at),
       last_failed_at = COALESCE(excluded.last_failed_at, self_capability_stats.last_failed_at),
       updated_at = excluded.updated_at`
  ).run(axis, key, ok ? 0 : 1, ok ? ts : null, ok ? null : ts, ts);
}

/** One (axis, key) tally, or null if nothing has ever been recorded for it — the "no track record" case is a real null, never a zeroed-out fake row. */
export function getStat(axis, key) {
  const row = getDb().prepare('SELECT * FROM self_capability_stats WHERE axis = ? AND key = ?').get(axis, key);
  return row ? rowToStat(row) : null;
}

export function listStats({ axis } = {}) {
  const rows = axis
    ? getDb().prepare('SELECT * FROM self_capability_stats WHERE axis = ? ORDER BY updated_at DESC').all(axis)
    : getDb().prepare('SELECT * FROM self_capability_stats ORDER BY updated_at DESC').all();
  return rows.map(rowToStat);
}

// ---------- goals ----------

function rowToGoal(row) {
  return {
    id: row.id,
    scopeKind: row.scope_kind, // 'conversation' | 'job' (jobs normally use job.goal directly — see header comment)
    scopeRef: row.scope_ref,
    goalText: row.goal_text,
    declaredAt: row.declared_at,
    status: row.status, // 'active' | 'closed'
    closedAt: row.closed_at,
  };
}

/** Declares (or replaces) the active goal for one scope — closes any prior active goal for the same scope first, so scope_ref only ever has one active row at a time. */
export function declareGoal({ scopeKind, scopeRef, goalText }) {
  const trimmed = String(goalText || '').trim();
  if (!scopeKind || !scopeRef || !trimmed) throw new Error('A goal needs a scope and some text.');
  const db = getDb();
  const ts = nowIso();
  db.prepare(
    `UPDATE self_goals SET status = 'closed', closed_at = ? WHERE scope_kind = ? AND scope_ref = ? AND status = 'active'`
  ).run(ts, scopeKind, scopeRef);
  const id = makeId('goal');
  db.prepare(
    `INSERT INTO self_goals (id, scope_kind, scope_ref, goal_text, declared_at, status) VALUES (?, ?, ?, ?, ?, 'active')`
  ).run(id, scopeKind, scopeRef, trimmed, ts);
  return getGoal(id);
}

export function getGoal(id) {
  const row = getDb().prepare('SELECT * FROM self_goals WHERE id = ?').get(id);
  return row ? rowToGoal(row) : null;
}

/** The current active goal for a scope, or null if none has ever been declared. */
export function getActiveGoal(scopeKind, scopeRef) {
  const row = getDb()
    .prepare(`SELECT * FROM self_goals WHERE scope_kind = ? AND scope_ref = ? AND status = 'active' ORDER BY declared_at DESC LIMIT 1`)
    .get(scopeKind, scopeRef);
  return row ? rowToGoal(row) : null;
}

export function closeGoal(id) {
  getDb().prepare(`UPDATE self_goals SET status = 'closed', closed_at = ? WHERE id = ? AND status = 'active'`).run(nowIso(), id);
  return getGoal(id);
}

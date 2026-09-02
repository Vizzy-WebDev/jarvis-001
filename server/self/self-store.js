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

// ---------- capture health ----------
//
// Health of the CAPTURE MECHANISM itself, distinct from what it measures —
// self_capability_stats answers "is this tool reliable"; this answers "is
// the thing that RECORDS that reliable." Without this, a broken recorder
// and a tool genuinely never used are indistinguishable from every
// self-model dimension reading self_capability_stats (self-capture.js's
// recordToolOutcome() calls recordCaptureHealth() on both the success and
// the failure path of its own recordAttempt() call — see that file).

// A capture_health row older than this is no longer useful for
// captureHealthSummary()'s own 24h window and is pruned opportunistically —
// same cheap-indexed-range-delete discipline as improvement-store.js's
// pruneReviewedOutcomes(), just with a time cutoff instead of a row-count
// cap, since this table's own read pattern is entirely time-windowed.
const CAPTURE_HEALTH_RETENTION_MS = 7 * 24 * 60 * 60 * 1000;

/** Logs one capture attempt's own outcome — `ok:true` on every successful write, `ok:false` (with `errorMessage`) when the write itself threw. Never throws itself; a caller (self-capture.js) already wraps this in its own try/catch, but an unrecognized `source` is silently accepted here rather than validated, since this is a health LOG, not a policy gate — losing a health signal to an over-strict check would defeat its own purpose. */
export function recordCaptureHealth({ source, name = null, ok, errorMessage = null }) {
  getDb()
    .prepare('INSERT INTO capture_health (ts, source, name, ok, error_message) VALUES (?, ?, ?, ?, ?)')
    .run(nowIso(), source, name, ok ? 1 : 0, ok ? null : errorMessage);
}

function pruneOldCaptureHealth() {
  const cutoff = new Date(Date.now() - CAPTURE_HEALTH_RETENTION_MS).toISOString();
  getDb().prepare('DELETE FROM capture_health WHERE ts < ?').run(cutoff);
}

/**
 * The last 24h of capture health, for check_myself's `can_do` dimension to
 * report alongside (never instead of) the reliability tallies it's built
 * on — so the model can tell "no track record because it's never been
 * used" apart from "no track record because the recorder itself is
 * broken." Prunes opportunistically on each call (see above) — this is
 * called far less often than recordCaptureHealth() itself (only when
 * can_do is actually checked), so the prune cost is paid rarely, not per
 * tool call.
 */
export function captureHealthSummary() {
  pruneOldCaptureHealth();
  const since = new Date(Date.now() - 24 * 60 * 60 * 1000).toISOString();
  const totals = getDb()
    .prepare('SELECT COUNT(*) AS attempts, SUM(CASE WHEN ok = 0 THEN 1 ELSE 0 END) AS failures FROM capture_health WHERE ts >= ?')
    .get(since);
  const lastFailure = getDb().prepare('SELECT ts, source FROM capture_health WHERE ok = 0 ORDER BY ts DESC LIMIT 1').get();
  return {
    attempts24h: totals?.attempts || 0,
    failures24h: totals?.failures || 0,
    lastFailureAt: lastFailure?.ts || null,
    lastFailureSource: lastFailure?.source || null,
  };
}

// ---------- self-model snapshots + citations ----------
//
// The write side of utterance provenance (root CLAUDE.md's "Self-Model"
// section) — server/self/self-verify.js's verifyCitation() is the read
// side that actually checks a claim, kept in a separate file since it also
// needs chat-store.js (the real conversation text), which this leaf module
// deliberately never imports.

function rowToSnapshot(row) {
  return {
    id: row.id,
    conversationId: row.conversation_id,
    turnId: row.turn_id,
    toolCallId: row.tool_call_id,
    snapshot: JSON.parse(row.snapshot_json),
    createdAt: row.created_at,
  };
}

/** Persists exactly what one check_myself call returned — the durable record "verifyCitation()" later re-reads, so a claim can be checked long after the live conversation window has moved on. */
export function saveSelfModelSnapshot({ conversationId, turnId = null, toolCallId = null, snapshot }) {
  const db = getDb();
  const id = makeId('snap');
  db.prepare(
    'INSERT INTO self_model_snapshots (id, conversation_id, turn_id, tool_call_id, snapshot_json, created_at) VALUES (?, ?, ?, ?, ?, ?)'
  ).run(id, conversationId, turnId, toolCallId, JSON.stringify(snapshot), nowIso());
  return id;
}

export function getSelfModelSnapshot(id) {
  const row = getDb().prepare('SELECT * FROM self_model_snapshots WHERE id = ?').get(id);
  return row ? rowToSnapshot(row) : null;
}

/** Finds a snapshot by the real tool_call id that produced it, for a forensic script that only has the conversation transcript to start from — never trusts a caller to already know the snapshot's own generated id. */
export function getSnapshotByToolCallId(conversationId, toolCallId) {
  const row = getDb()
    .prepare('SELECT * FROM self_model_snapshots WHERE conversation_id = ? AND tool_call_id = ? ORDER BY created_at DESC LIMIT 1')
    .get(conversationId, toolCallId);
  return row ? rowToSnapshot(row) : null;
}

/** One candidate checkable fact from a snapshot — `fieldValue` is stored for a human skimming the raw table, but `verifyCitation()` never trusts it; it re-reads the snapshot itself instead, so this row going stale can never produce a wrong verdict. */
export function recordSelfModelCitation({ snapshotId, toolCallId = null, fieldName, fieldValue }) {
  const db = getDb();
  const id = makeId('cite');
  db.prepare(
    'INSERT INTO self_model_citations (id, snapshot_id, tool_call_id, field_name, field_value, created_at) VALUES (?, ?, ?, ?, ?, ?)'
  ).run(id, snapshotId, toolCallId, fieldName, String(fieldValue), nowIso());
  return id;
}

export function listCitationsForSnapshot(snapshotId) {
  return getDb()
    .prepare('SELECT id, snapshot_id, tool_call_id, field_name, field_value, created_at FROM self_model_citations WHERE snapshot_id = ?')
    .all(snapshotId)
    .map((r) => ({ id: r.id, snapshotId: r.snapshot_id, toolCallId: r.tool_call_id, fieldName: r.field_name, fieldValue: r.field_value, createdAt: r.created_at }));
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

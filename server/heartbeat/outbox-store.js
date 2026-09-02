// The Tier 1/2/3 Interruption Broker's real storage — generalized off Jobs'
// original `job_outbox` table (see db.js migration 13's own comment) so a
// Heartbeat/Trigger finding with no job behind it can use the exact same
// mechanism instead of a second one. Leaf module: imports only db.js, same
// discipline as jobs/job-store.js and every other *-store.js in this
// project — safe for server/tools/ to import directly.
//
// `job-store.js` re-exports its four original outbox functions as thin
// wrappers over this file (source:'job', sourceRef:jobId baked in), so every
// existing Jobs call site (job-actions.js, worker.js, orchestrator.js,
// server.js's job routes) is unchanged in both signature and behavior.

import { getDb } from '../db.js';

function nowIso() {
  return new Date().toISOString();
}

function rowToEntry(row) {
  return {
    id: row.id,
    source: row.source, // 'job' | 'heartbeat'
    sourceRef: row.source_ref,
    jobId: row.job_id,
    tier: row.tier,
    reason: row.reason, // 'permission' | 'stuck' | 'notice'
    summary: row.summary,
    detail: row.detail,
    confirmPayload: row.confirm_payload ? JSON.parse(row.confirm_payload) : null,
    createdAt: row.created_at,
    deliveredAt: row.delivered_at,
  };
}

/**
 * Parks a Tier 1/2/3 decision. `source` defaults to 'heartbeat' here — the
 * job-store.js wrapper is what passes `source:'job'` explicitly. `jobId` is
 * only ever set for a job-sourced entry (carries the real FK/cascade);
 * `sourceRef` is the generic pointer every source uses to find its own rows
 * again (a job's own id, a memory's own id, ...).
 */
export function addEntry({ source = 'heartbeat', sourceRef = null, jobId = null, tier, reason = 'notice', summary, detail = null, confirmPayload = null }) {
  if (!summary) throw new Error('An outbox entry needs a summary.');
  const db = getDb();
  const ts = nowIso();
  const result = db
    .prepare(
      `INSERT INTO outbox (source, source_ref, job_id, tier, reason, summary, detail, confirm_payload, created_at)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)`
    )
    .run(source, sourceRef, jobId, tier, reason, summary, detail, confirmPayload ? JSON.stringify(confirmPayload) : null, ts);
  return rowToEntry(db.prepare('SELECT * FROM outbox WHERE id = ?').get(result.lastInsertRowid));
}

/**
 * Everything not yet delivered, lowest tier (most urgent) first — what
 * prompt.js's jobsSection() drains on a turn the user already started.
 * `tier` optionally narrows to one tier; `source` optionally narrows to
 * 'job' or 'heartbeat'.
 */
export function listPending({ tier, source } = {}) {
  const clauses = ['delivered_at IS NULL'];
  const params = [];
  if (tier !== undefined) {
    clauses.push('tier = ?');
    params.push(tier);
  }
  if (source !== undefined) {
    clauses.push('source = ?');
    params.push(source);
  }
  const rows = getDb()
    .prepare(`SELECT * FROM outbox WHERE ${clauses.join(' AND ')} ORDER BY tier ASC, created_at ASC`)
    .all(...params);
  return rows.map(rowToEntry);
}

export function getById(id) {
  const row = getDb().prepare('SELECT * FROM outbox WHERE id = ?').get(id);
  return row ? rowToEntry(row) : null;
}

export function getForJob(jobId) {
  const rows = getDb().prepare('SELECT * FROM outbox WHERE job_id = ? ORDER BY created_at ASC').all(jobId);
  return rows.map(rowToEntry);
}

/** The dedup lookup a source's own check() should use before creating a second undelivered row for the same finding — e.g. don't re-park the same job's Tier 1 permission ask twice. */
export function getPendingForSourceRef(source, sourceRef) {
  const rows = getDb()
    .prepare('SELECT * FROM outbox WHERE source = ? AND source_ref = ? AND delivered_at IS NULL ORDER BY created_at ASC')
    .all(source, sourceRef);
  return rows.map(rowToEntry);
}

export function markDelivered(id) {
  getDb().prepare('UPDATE outbox SET delivered_at = ? WHERE id = ?').run(nowIso(), id);
}

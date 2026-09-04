// The generalized write-ahead activity trace — generalized off Jobs' own
// `job_trace` table (db.js migration 15's own comment) the exact same way
// heartbeat/outbox-store.js generalized `job_outbox` -> `outbox` in
// migration 13. A self-diagnosis probe, a verification check, or an
// environment finding can now log its own intent-then-outcome pair with the
// same durable, sequenced, crash-honest guarantee Jobs already relies on,
// instead of a second, parallel mechanism.
//
// Leaf module: imports only db.js, same discipline as every other
// *-store.js in this project — safe for server/tools/ to import directly
// without tripping the loader/runner/scheduler circular-import invariant in
// root CLAUDE.md.
//
// `jobs/job-store.js` re-exports its three original trace functions
// (appendTrace/getTrace/getTraceTail) as thin wrappers over this file
// (source:'job', sourceRef:jobId, jobId baked in), so every existing Jobs
// call site (worker.js, orchestrator.js, job-policy.js's callers) keeps its
// exact original signature and behavior.

import { getDb } from '../db.js';

const DEFAULT_TAIL_SIZE = 8;

function nowIso() {
  return new Date().toISOString();
}

function rowToEntry(row) {
  return {
    id: row.id,
    source: row.source, // 'job' | 'diagnosis' | 'verification' | 'environment'
    sourceRef: row.source_ref,
    jobId: row.job_id,
    seq: row.seq,
    phase: row.phase, // 'intent' | 'outcome'
    effect: row.effect, // 'read' | 'workspace' | 'external'
    kind: row.kind, // 'tool' | 'decision' | 'source' | 'error' | 'note' | 'probe' | 'remedy'
    summary: row.summary,
    // Free-form — a tool entry's caller JSON.stringifies {name, args} (intent)
    // or {name, ok} (outcome) here; job-policy.js's diagnoseStall() is the
    // one place that parses a job-sourced row's detail back out. Never
    // auto-decoded by the store — not every source uses it the same way.
    detail: row.detail,
    createdAt: row.created_at,
  };
}

/**
 * Appends ONE trace row — the caller decides intent vs outcome and effect,
 * same contract job-store.js's original appendTrace() always had. Scoped by
 * (source, sourceRef) for its own sequence counter, so a job's own seq
 * numbering is unaffected: job-store.js always passes sourceRef:jobId, the
 * exact scope job_id used to provide directly pre-migration. `source`
 * defaults to 'ops' here — the job-store.js wrapper is what passes
 * 'job' explicitly.
 */
export function appendEntry({ source = 'ops', sourceRef, jobId = null, phase, effect, kind, summary, detail = null }) {
  if (!sourceRef) throw new Error('A trace entry needs a sourceRef to scope its sequence to.');
  if (!phase || !effect || !kind || !summary) {
    throw new Error('A trace entry needs phase, effect, kind, and summary.');
  }
  const db = getDb();
  const ts = nowIso();
  db.exec('BEGIN');
  try {
    const { seq: lastSeq } =
      db.prepare('SELECT COALESCE(MAX(seq), 0) AS seq FROM trace WHERE source = ? AND source_ref = ?').get(source, sourceRef) ||
      { seq: 0 };
    const nextSeq = lastSeq + 1;
    db.prepare(
      `INSERT INTO trace (source, source_ref, job_id, seq, phase, effect, kind, summary, detail, created_at)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`
    ).run(source, sourceRef, jobId, nextSeq, phase, effect, kind, summary, detail, ts);
    db.exec('COMMIT');
  } catch (err) {
    db.exec('ROLLBACK');
    throw err;
  }
  return getTail(source, sourceRef, 1)[0];
}

/** The full trace for one (source, sourceRef), oldest first — used for classifyRecovery() on an orphaned job, or for inspecting one diagnosis check's own history. */
export function getFull(source, sourceRef) {
  const rows = getDb()
    .prepare('SELECT * FROM trace WHERE source = ? AND source_ref = ? ORDER BY seq ASC')
    .all(source, sourceRef);
  return rows.map(rowToEntry);
}

/**
 * The last `n` rows for one (source, sourceRef), oldest-first within that
 * window — what a supervisor tick (or a diagnostic re-probe) feeds to a
 * stall/health check so it costs a bounded query, not a full-history read,
 * on every tick.
 */
export function getTail(source, sourceRef, n = DEFAULT_TAIL_SIZE) {
  const rows = getDb()
    .prepare('SELECT * FROM trace WHERE source = ? AND source_ref = ? ORDER BY seq DESC LIMIT ?')
    .all(source, sourceRef, n);
  return rows.reverse().map(rowToEntry);
}

/**
 * Every row across every source_ref for one source, newest first — what
 * "has anything gone wrong with you lately?" reads (check_my_health.js).
 * Capped, never unbounded, since a chatty source's history could otherwise
 * grow this into a full-table scan.
 */
export function listRecentForSource(source, { limit = 50 } = {}) {
  const rows = getDb()
    .prepare('SELECT * FROM trace WHERE source = ? ORDER BY created_at DESC LIMIT ?')
    .all(source, limit);
  return rows.map(rowToEntry);
}

// Background Task Orchestration ("Jobs") persistence — the durable truth for
// every in-flight or finished background job and its write-ahead activity
// trace. Built on db.js's SQLite connection, same as chat-store.js and
// memory/memory-store.js.
//
// The Tier 1/2/3 interruption outbox this file used to own directly now
// lives in server/heartbeat/outbox-store.js — generalized (db.js migration
// 13) so a Heartbeat/Trigger finding with no job behind it can use the same
// broker instead of a second one (see root CLAUDE.md's Heartbeat section).
// The write-ahead trace this file used to own directly now lives in
// server/ops/ops-trace.js — generalized (db.js migration 15) the exact same
// way, so a self-diagnosis probe or verification check can log its own
// intent/outcome pair without a second trace mechanism (see root CLAUDE.md's
// "Operational Awareness" section). The seven functions below are thin
// wrappers over those two stores with `source:'job'`/`jobId` baked in, so
// every existing call site here keeps its exact original signature and
// behavior.
//
// Leaf module: imports only db.js, heartbeat/outbox-store.js, and
// ops/ops-trace.js (all three leaves) — safe for server/tools/
// (work_in_background.js, check_on_work.js, stop_working_on.js) to import
// directly without tripping the loader/runner/scheduler circular-import
// invariant in root CLAUDE.md.

import { getDb } from '../db.js';
import * as outboxStore from '../heartbeat/outbox-store.js';
import * as traceStore from '../ops/ops-trace.js';

const DEFAULT_TAIL_SIZE = 8;

function makeId() {
  return `j${Date.now().toString(36)}${Math.random().toString(36).slice(2, 8)}`;
}

function nowIso() {
  return new Date().toISOString();
}

function rowToJob(row) {
  return {
    id: row.id,
    parentId: row.parent_id,
    conversationId: row.conversation_id,
    title: row.title,
    goal: row.goal,
    kind: row.kind,
    status: row.status,
    plan: row.plan ? JSON.parse(row.plan) : null,
    resource: row.resource,
    recovery: row.recovery,
    result: row.result,
    error: row.error,
    retries: row.retries,
    // A full snapshot of the worker's conversation.getMessages(sessionId) at
    // its last step, overwritten (not appended) each time — see
    // updateJob()'s `transcript` handling. What lets a job classified
    // `resumable` continue with its real prior context instead of
    // restarting from `goal` alone after a crash.
    transcript: row.transcript ? JSON.parse(row.transcript) : null,
    resumeNote: row.resume_note,
    createdAt: row.created_at,
    startedAt: row.started_at,
    heartbeatAt: row.heartbeat_at,
    finishedAt: row.finished_at,
  };
}

/**
 * Creates a job in `queued` status. `parentId` set only for a level-1 peer
 * spawned by a split request (see root CLAUDE.md's Jobs section — a job
 * with a non-null parentId can only ever itself produce a level-1 peer,
 * never a level-2 child; that ceiling is enforced by the orchestrator, not
 * this store). `conversationId` is deliberately NOT a foreign key (see
 * db.js's migration 4 comment) — a job must outlive the conversation that
 * started it.
 */
export function createJob({ parentId = null, conversationId = null, title, goal, kind, resource = null, plan = null }) {
  if (!goal) throw new Error('A job needs a goal.');
  const db = getDb();
  const id = makeId();
  const ts = nowIso();
  db.prepare(
    `INSERT INTO jobs (id, parent_id, conversation_id, title, goal, kind, status, resource, retries, plan, created_at)
     VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, 0, ?, ?)`
  ).run(id, parentId, conversationId, title || goal.slice(0, 80), goal, kind, resource, plan ? JSON.stringify(plan) : null, ts);
  return getJob(id);
}

export function getJob(id) {
  const row = getDb().prepare('SELECT * FROM jobs WHERE id = ?').get(id);
  return row ? rowToJob(row) : null;
}

/**
 * `status` and `parentId` each accept either a single value or an array
 * (IN-clause) — the orchestrator's steady-state checks (capacity, resource
 * contention) want "every active job" as one query, e.g.
 * `listJobs({ status: ['queued','planning','running','awaiting_decision'] })`.
 */
export function listJobs({ status, parentId, kind } = {}) {
  const clauses = [];
  const params = [];
  const addInClause = (column, value) => {
    const values = Array.isArray(value) ? value : [value];
    if (!values.length) return;
    clauses.push(`${column} IN (${values.map(() => '?').join(',')})`);
    params.push(...values);
  };
  if (status !== undefined) addInClause('status', status);
  if (parentId !== undefined) addInClause('parent_id', parentId);
  if (kind !== undefined) addInClause('kind', kind);

  const where = clauses.length ? `WHERE ${clauses.join(' AND ')}` : '';
  const rows = getDb().prepare(`SELECT * FROM jobs ${where} ORDER BY created_at ASC`).all(...params);
  return rows.map(rowToJob);
}

// Two different lists on purpose, found via live testing rather than
// assumed correct up front: a job parked in 'awaiting_decision' consumes NO
// resources (no worker is driving it) but hasn't RELEASED whatever resource
// it was holding either. Capacity is about how much active work is actually
// running — an escalated job sitting untouched must never permanently
// occupy a concurrency slot, or two stalled jobs the owner hasn't gotten to
// yet would block all new work indefinitely. Resource contention is the
// opposite: a job waiting on a decision still holds its claim, since
// another job racing for the same exclusive resource (e.g. 'computer')
// could genuinely collide with it the moment it resumes.
export const RUNNING_STATUSES = ['queued', 'planning', 'running'];
export const RESOURCE_HOLDING_STATUSES = ['queued', 'planning', 'running', 'awaiting_decision'];

/** Jobs that count against prefs.maxBackgroundJobs — see job-policy.js's hasCapacity(). */
export function listActiveJobs() {
  return listJobs({ status: RUNNING_STATUSES });
}

/** Jobs that still hold (or will hold) an exclusive `resource` claim — see job-policy.js's resourceAvailable(). Broader than listActiveJobs() on purpose. */
export function listResourceHoldingJobs() {
  return listJobs({ status: RESOURCE_HOLDING_STATUSES });
}

const UPDATABLE_FIELDS = {
  title: 'title',
  status: 'status',
  plan: 'plan', // object -> JSON
  resource: 'resource',
  recovery: 'recovery',
  result: 'result',
  error: 'error',
  retries: 'retries',
  transcript: 'transcript', // array -> JSON, whole-snapshot overwrite
  resumeNote: 'resume_note',
  startedAt: 'started_at',
  heartbeatAt: 'heartbeat_at',
  finishedAt: 'finished_at',
};

/**
 * Whitelisted patch, same discipline as models/registry.js's updateModel() —
 * `id`/`parentId`/`conversationId`/`kind`/`goal`/`createdAt` can never be
 * set through this, so a job can't desync from what created it. `plan` and
 * `transcript` are passed as plain JS values (object / array) and encoded
 * here; every other field is a plain scalar.
 */
export function updateJob(id, patch = {}) {
  const sets = [];
  const params = [];
  for (const [key, column] of Object.entries(UPDATABLE_FIELDS)) {
    if (!(key in patch)) continue;
    const value = patch[key];
    sets.push(`${column} = ?`);
    params.push(key === 'plan' || key === 'transcript' ? (value == null ? null : JSON.stringify(value)) : value);
  }
  if (!sets.length) return getJob(id);
  params.push(id);
  getDb().prepare(`UPDATE jobs SET ${sets.join(', ')} WHERE id = ?`).run(...params);
  return getJob(id);
}

/** Convenience wrapper — every trace append and every runTurn event calls this. */
export function touchHeartbeat(id) {
  return updateJob(id, { heartbeatAt: nowIso() });
}

export function deleteJob(id) {
  getDb().prepare('DELETE FROM jobs WHERE id = ?').run(id); // cascades to job_trace/job_outbox
}

/**
 * Appends ONE trace row — the caller decides intent vs outcome and effect.
 * Thin wrapper over ops/ops-trace.js's appendEntry() with `source:'job'`,
 * `sourceRef:jobId`, `jobId` baked in — see this file's header comment.
 * `detail` is an opaque string; a caller recording a tool call passes
 * `JSON.stringify({name, args})` for an 'intent' row and
 * `JSON.stringify({name, ok})` for its 'outcome' — job-policy.js's
 * diagnoseStall() is what parses it back out.
 */
export function appendTrace(jobId, { phase, effect, kind, summary, detail = null }) {
  return traceStore.appendEntry({ source: 'job', sourceRef: jobId, jobId, phase, effect, kind, summary, detail });
}

/** The full trace for one job, oldest first — used for classifyRecovery() on an orphaned job. */
export function getTrace(jobId) {
  return traceStore.getFull('job', jobId);
}

/**
 * The last `n` trace rows, oldest-first within that window — what a
 * supervisor tick feeds to diagnoseStall() so a stall check costs a bounded
 * query, not a full-history read, on every tick for every running job.
 */
export function getTraceTail(jobId, n = DEFAULT_TAIL_SIZE) {
  return traceStore.getTail('job', jobId, n);
}

/**
 * Parks a Tier 1/2/3 decision — `reason: 'permission'` (a confirm-gated tool
 * call escalated via capabilities.js's ctx.onEscalate) or `reason: 'stuck'`
 * (a stall that survived its one recovery retry). `confirmPayload` carries
 * `{name, args}` for a 'permission' row; left null for 'stuck'. Thin wrapper
 * over heartbeat/outbox-store.js's generalized addEntry() — see this file's
 * header comment.
 */
export function addOutboxEntry(jobId, { tier, reason = 'permission', summary, detail = null, confirmPayload = null }) {
  return outboxStore.addEntry({ source: 'job', sourceRef: jobId, jobId, tier, reason, summary, detail, confirmPayload });
}

/**
 * Job-sourced outbox rows not yet delivered, lowest tier (most urgent)
 * first. `tier` optionally narrows to one tier. Kept for anything that
 * specifically wants a job-only view — prompt.js's own drain now reads
 * heartbeat/outbox-store.js's listPending() directly, across every source.
 */
export function listPendingOutbox({ tier } = {}) {
  return outboxStore.listPending({ tier, source: 'job' });
}

export function getOutboxForJob(jobId) {
  return outboxStore.getForJob(jobId);
}

export function markOutboxDelivered(id) {
  outboxStore.markDelivered(id);
}

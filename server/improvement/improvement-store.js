// Self-Improvement persistence — outcomes (what happened), lessons (what
// was noticed), proposals (what's suggested), rules (what's actually live
// in the system prompt), and the change/undo log. Built on db.js's SQLite
// connection, same as chat-store.js and memory/memory-store.js.
//
// Leaf module: imports only db.js (itself a leaf) — safe for server/tools/
// and prompt.js to import directly without tripping the loader/runner
// circular-import invariant (root CLAUDE.md).

import { getDb } from '../db.js';

function makeId(prefix) {
  return `${prefix}${Date.now().toString(36)}${Math.random().toString(36).slice(2, 8)}`;
}

function nowIso() {
  return new Date().toISOString();
}

function parseJson(text, fallback) {
  if (text == null) return fallback;
  try {
    return JSON.parse(text);
  } catch {
    return fallback;
  }
}

function rowToOutcome(row) {
  return {
    id: row.id,
    source: row.source, // 'job' | 'task' | 'correction' | 'explicit'
    sourceRef: row.source_ref,
    entityRef: row.entity_ref,
    title: row.title,
    goal: row.goal,
    kind: row.kind,
    status: row.status,
    retries: row.retries,
    error: row.error,
    toolSummary: parseJson(row.tool_summary, null),
    escalations: row.escalations,
    reviewedAt: row.reviewed_at,
    createdAt: row.created_at,
  };
}

function rowToLesson(row) {
  return {
    id: row.id,
    kind: row.kind, // 'lesson' | 'pattern'
    text: row.text,
    scope: row.scope, // 'general' | 'tool:<name>' | 'job_kind:<k>' | 'task:<id>'
    evidence: parseJson(row.evidence, []),
    confidence: row.confidence,
    sourceTier: row.source_tier, // 1 = own task history, 2 = official docs, 3 = communities, 4 = general web
    sourceUrl: row.source_url,
    status: row.status, // 'active' | 'archived' | 'superseded'
    createdAt: row.created_at,
    updatedAt: row.updated_at,
  };
}

function rowToProposal(row) {
  return {
    id: row.id,
    kind: row.kind, // 'rule' | 'setting' | 'skill' | 'code' | 'idea' | 'conflict'
    title: row.title,
    rationale: row.rationale,
    helpsJarvis: row.helps_jarvis,
    helpsUser: row.helps_user,
    payload: parseJson(row.payload, null),
    evidence: parseJson(row.evidence, []),
    sourceTier: row.source_tier,
    sourceUrl: row.source_url,
    verified: Boolean(row.verified),
    batchId: row.batch_id,
    status: row.status, // 'pending' | 'applied' | 'rejected' | 'superseded'
    conflictWith: row.conflict_with,
    implementationPrompt: row.implementation_prompt,
    implementationTarget: row.implementation_target,
    createdAt: row.created_at,
    resolvedAt: row.resolved_at,
  };
}

function rowToRule(row) {
  return {
    id: row.id,
    text: row.text,
    scope: row.scope,
    active: Boolean(row.active),
    archivedAt: row.archived_at,
    sourceProposalId: row.source_proposal_id,
    lastSupportedAt: row.last_supported_at,
    createdAt: row.created_at,
    updatedAt: row.updated_at,
  };
}

function rowToChange(row) {
  return {
    id: row.id,
    kind: row.kind, // 'rule' | 'setting' | 'skill' | 'undo'
    target: row.target,
    before: parseJson(row.before, null),
    after: parseJson(row.after, null),
    reason: row.reason,
    proposalId: row.proposal_id,
    appliedAt: row.applied_at,
    undoneAt: row.undone_at,
  };
}

// ---------- outcomes ----------

/**
 * Records one completed unit of work — a job or scheduled task reaching a
 * terminal status, a plainly-worded correction, or something the user
 * explicitly taught. `INSERT OR IGNORE` on `(source, source_ref)` makes
 * this idempotent by construction: server/jobs/orchestrator.js and
 * worker.js both have real, confirmed-live paths that can emit a 'failed'
 * status twice for the same job, and capture.js relies on this store, not
 * the caller, to make a duplicate emission a no-op rather than two rows.
 * Returns the row that ended up saved (whichever call actually inserted
 * it), or null if nothing was ever written (a source/sourceRef pair that
 * doesn't uniquely identify anything — the caller's mistake, not silently
 * swallowed data loss of a real row).
 */
export function recordOutcome({ source, sourceRef = null, entityRef = null, title, goal, kind, status, retries = 0, error = null, toolSummary = null, escalations = 0 }) {
  if (!source || !status) throw new Error('An outcome needs a source and a status.');
  const db = getDb();
  const id = makeId('out');
  const ts = nowIso();
  const result = db
    .prepare(
      `INSERT OR IGNORE INTO improvement_outcomes
        (id, source, source_ref, entity_ref, title, goal, kind, status, retries, error, tool_summary, escalations, created_at)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`
    )
    .run(id, source, sourceRef, entityRef, title || null, goal || null, kind || null, status, retries, error, toolSummary ? JSON.stringify(toolSummary) : null, escalations, ts);
  if (result.changes === 0) {
    // Already recorded (the UNIQUE(source, source_ref) constraint caught a
    // duplicate emission) — return the existing row rather than nothing, so
    // a caller that doesn't care about idempotency still gets a real
    // outcome back.
    if (sourceRef == null) return null; // no natural key to look the existing row up by
    const existing = db.prepare('SELECT * FROM improvement_outcomes WHERE source = ? AND source_ref = ?').get(source, sourceRef);
    return existing ? rowToOutcome(existing) : null;
  }
  return getOutcome(id);
}

export function getOutcome(id) {
  const row = getDb().prepare('SELECT * FROM improvement_outcomes WHERE id = ?').get(id);
  return row ? rowToOutcome(row) : null;
}

/** Batch fetch for the screen's detail-view "Evidence (N)" expand — a rule/lesson/proposal's `evidence` array is a list of outcome ids; this turns them back into real, readable summaries in one query. Ids that no longer exist (an outcome pruned by pruneReviewedOutcomes()) are silently skipped, not an error — evidence is inherently best-effort once the underlying row has aged out. */
export function lookupOutcomes(ids) {
  const list = Array.isArray(ids) ? ids.filter(Boolean) : [];
  if (!list.length) return [];
  const placeholders = list.map(() => '?').join(',');
  const rows = getDb().prepare(`SELECT * FROM improvement_outcomes WHERE id IN (${placeholders})`).all(...list);
  return rows.map(rowToOutcome);
}

export function listUnreviewedOutcomes({ limit } = {}) {
  const sql = `SELECT * FROM improvement_outcomes WHERE reviewed_at IS NULL ORDER BY created_at ASC${limit ? ' LIMIT ?' : ''}`;
  const rows = limit ? getDb().prepare(sql).all(limit) : getDb().prepare(sql).all();
  return rows.map(rowToOutcome);
}

export function countUnreviewedOutcomes() {
  return getDb().prepare('SELECT COUNT(*) AS n FROM improvement_outcomes WHERE reviewed_at IS NULL').get().n;
}

/** Marks a batch of outcomes as reviewed — called once reflect.js has folded them into lessons (or explicitly decided there was nothing worth extracting). */
export function markOutcomesReviewed(ids) {
  if (!ids?.length) return;
  const db = getDb();
  const ts = nowIso();
  const stmt = db.prepare('UPDATE improvement_outcomes SET reviewed_at = ? WHERE id = ?');
  db.exec('BEGIN');
  try {
    for (const id of ids) stmt.run(ts, id);
    db.exec('COMMIT');
  } catch (err) {
    db.exec('ROLLBACK');
    throw err;
  }
}

// Retention cap mirroring task-store.js's MAX_RUNS_KEPT — without this,
// reviewed outcomes accumulate forever even though nothing past reflection
// ever reads them again, and the table would grow unbounded over months of
// use for no benefit.
const MAX_REVIEWED_OUTCOMES_KEPT = 300;

/** Prunes old, already-reviewed outcomes past the retention cap — never touches an unreviewed one, regardless of age. */
export function pruneReviewedOutcomes() {
  getDb()
    .prepare(
      `DELETE FROM improvement_outcomes WHERE id IN (
         SELECT id FROM improvement_outcomes WHERE reviewed_at IS NOT NULL
         ORDER BY created_at DESC LIMIT -1 OFFSET ?
       )`
    )
    .run(MAX_REVIEWED_OUTCOMES_KEPT);
}

/**
 * Aggregate reliability over improvement_outcomes, grouped by `kind` (a
 * job's own `kind`, or a scheduled task's `action.type` — see
 * improvement/capture.js's recordJobOutcome()/recordTaskOutcome()). Added
 * for server/self/self-model.js's dimension-2 grounding ("what can/can't it
 * actually do") — jobs/tasks have no rolling self_capability_stats tally of
 * their own (only 'tool' calls do; see self-store.js's header comment on
 * why), so this table is the real evidence for them instead. `'cancelled'`
 * outcomes are excluded from both attempts and failures — a cancellation
 * isn't evidence Jarvis succeeded OR failed at the thing, so counting it
 * either way would misrepresent the actual track record.
 */
export function outcomeReliability({ source, kind } = {}) {
  const clauses = [];
  const params = [];
  if (source) {
    clauses.push('source = ?');
    params.push(source);
  }
  if (kind) {
    clauses.push('kind = ?');
    params.push(kind);
  }
  clauses.push("status != 'cancelled'");
  const where = `WHERE ${clauses.join(' AND ')}`;
  const row = getDb()
    .prepare(`SELECT COUNT(*) AS attempts, SUM(CASE WHEN status = 'failed' OR status = 'orphaned' THEN 1 ELSE 0 END) AS failures FROM improvement_outcomes ${where}`)
    .get(...params);
  return { attempts: row?.attempts || 0, failures: row?.failures || 0 };
}

// ---------- lessons ----------

export function createLesson({ kind = 'lesson', text, scope = 'general', evidence = [], confidence = null, sourceTier = 1, sourceUrl = null }) {
  const trimmed = String(text || '').trim();
  if (!trimmed) throw new Error('A lesson needs some text.');
  const db = getDb();
  const id = makeId('les');
  const ts = nowIso();
  db.prepare(
    `INSERT INTO improvement_lessons (id, kind, text, scope, evidence, confidence, source_tier, source_url, status, created_at, updated_at)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)`
  ).run(id, kind, trimmed, scope, JSON.stringify(evidence || []), confidence, sourceTier, sourceUrl, ts, ts);
  return getLesson(id);
}

export function getLesson(id) {
  const row = getDb().prepare('SELECT * FROM improvement_lessons WHERE id = ?').get(id);
  return row ? rowToLesson(row) : null;
}

export function listLessons({ status = 'active', scope, kind } = {}) {
  const clauses = [];
  const params = [];
  if (status) {
    clauses.push('status = ?');
    params.push(status);
  }
  if (scope) {
    clauses.push('scope = ?');
    params.push(scope);
  }
  if (kind) {
    clauses.push('kind = ?');
    params.push(kind);
  }
  const where = clauses.length ? `WHERE ${clauses.join(' AND ')}` : '';
  const rows = getDb().prepare(`SELECT * FROM improvement_lessons ${where} ORDER BY created_at DESC`).all(...params);
  return rows.map(rowToLesson);
}

export function updateLessonStatus(id, status) {
  getDb().prepare('UPDATE improvement_lessons SET status = ?, updated_at = ? WHERE id = ?').run(status, nowIso(), id);
  return getLesson(id);
}

/** Hard delete — only ever reachable from the screen's "Show archived" view. No improvement_changes row ever references a lesson id (only rules and prefs do — see apply.js), so this carries no undo-orphaning risk the way deleteRule() does. */
export function deleteLesson(id) {
  getDb().prepare('DELETE FROM improvement_lessons WHERE id = ?').run(id);
}

// ---------- proposals ----------

export function createProposal({
  kind,
  title,
  rationale = null,
  helpsJarvis = null,
  helpsUser = null,
  payload = null,
  evidence = [],
  sourceTier = 1,
  sourceUrl = null,
  verified = false,
  batchId = null,
  conflictWith = null,
}) {
  if (!kind || !title) throw new Error('A proposal needs a kind and a title.');
  const db = getDb();
  const id = makeId('prop');
  const ts = nowIso();
  db.prepare(
    `INSERT INTO improvement_proposals
       (id, kind, title, rationale, helps_jarvis, helps_user, payload, evidence, source_tier, source_url, verified, batch_id, status, conflict_with, created_at)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)`
  ).run(
    id,
    kind,
    title,
    rationale,
    helpsJarvis,
    helpsUser,
    payload ? JSON.stringify(payload) : null,
    JSON.stringify(evidence || []),
    sourceTier,
    sourceUrl,
    verified ? 1 : 0,
    batchId,
    conflictWith,
    ts
  );
  return getProposal(id);
}

export function getProposal(id) {
  const row = getDb().prepare('SELECT * FROM improvement_proposals WHERE id = ?').get(id);
  return row ? rowToProposal(row) : null;
}

export function listProposals({ status, batchId } = {}) {
  const clauses = [];
  const params = [];
  if (status) {
    clauses.push('status = ?');
    params.push(status);
  }
  if (batchId) {
    clauses.push('batch_id = ?');
    params.push(batchId);
  }
  const where = clauses.length ? `WHERE ${clauses.join(' AND ')}` : '';
  const rows = getDb().prepare(`SELECT * FROM improvement_proposals ${where} ORDER BY created_at DESC`).all(...params);
  return rows.map(rowToProposal);
}

export function setProposalStatus(id, status) {
  getDb().prepare('UPDATE improvement_proposals SET status = ?, resolved_at = ? WHERE id = ?').run(status, nowIso(), id);
  return getProposal(id);
}

/** Brings a rejected suggestion back to 'pending' — the screen's "Show rejected" view's Restore action. */
export function restoreProposal(id) {
  getDb().prepare("UPDATE improvement_proposals SET status = 'pending', resolved_at = NULL WHERE id = ?").run(id);
  return getProposal(id);
}

/** Hard delete — only ever reachable from the "Show rejected" view. improvement_changes.proposal_id references a proposal only for traceability and is never dereferenced for display, so deleting one carries no display-breaking risk. */
export function deleteProposal(id) {
  getDb().prepare('DELETE FROM improvement_proposals WHERE id = ?').run(id);
}

export function setProposalImplementation(id, { implementationPrompt, implementationTarget }) {
  getDb()
    .prepare('UPDATE improvement_proposals SET implementation_prompt = ?, implementation_target = ? WHERE id = ?')
    .run(implementationPrompt, implementationTarget || null, id);
  return getProposal(id);
}

/**
 * Ids of active rules a still-PENDING conflict proposal names — the direct
 * mirror of memory-store.js's conflictedMemoryIds(): a rule contradicted by
 * a pending conflict is held OUT of activeRulesText() below while a human
 * decides, rather than keeping on asserting the possibly-wrong rule every
 * single turn until they get around to it.
 */
function conflictedRuleIds() {
  const rows = getDb()
    .prepare(`SELECT DISTINCT conflict_with AS id FROM improvement_proposals WHERE status = 'pending' AND kind = 'conflict' AND conflict_with IS NOT NULL`)
    .all();
  return new Set(rows.map((r) => r.id));
}

// ---------- rules ----------

/** Creates a rule directly — only ever called from apply.js once policy has already decided auto-apply, or from the screen once the user approves a 'rule' proposal by hand. */
export function createRule({ text, scope = 'general', sourceProposalId = null }) {
  const trimmed = String(text || '').trim();
  if (!trimmed) throw new Error('A rule needs some text.');
  const db = getDb();
  const id = makeId('rule');
  const ts = nowIso();
  db.prepare(
    `INSERT INTO improvement_rules (id, text, scope, active, source_proposal_id, created_at, updated_at)
     VALUES (?, ?, ?, 1, ?, ?, ?)`
  ).run(id, trimmed, scope, sourceProposalId, ts, ts);
  return getRule(id);
}

export function getRule(id) {
  const row = getDb().prepare('SELECT * FROM improvement_rules WHERE id = ?').get(id);
  return row ? rowToRule(row) : null;
}

/** `includeArchived` (default false) matches listMemories()'s own default — an archived rule is hidden from every ordinary list (the Learned tab, activeRulesText()'s own prompt injection) unless explicitly asked for, the same "out of the way unless you go looking" behavior Memory's own archived flag already gives. */
export function listRules({ activeOnly = false, scope, includeArchived = false } = {}) {
  const clauses = [];
  const params = [];
  if (!includeArchived) clauses.push('archived_at IS NULL');
  if (activeOnly) clauses.push('active = 1');
  if (scope) {
    clauses.push('scope = ?');
    params.push(scope);
  }
  const where = clauses.length ? `WHERE ${clauses.join(' AND ')}` : '';
  const rows = getDb().prepare(`SELECT * FROM improvement_rules ${where} ORDER BY created_at ASC`).all(...params);
  return rows.map(rowToRule);
}

/** Flips a rule's active flag without touching its text/history — the "mute without unwinding via undo" control the screen needs. */
export function setRuleActive(id, active) {
  getDb().prepare('UPDATE improvement_rules SET active = ?, updated_at = ? WHERE id = ?').run(active ? 1 : 0, nowIso(), id);
  return getRule(id);
}

export function touchRuleSupported(id) {
  getDb().prepare('UPDATE improvement_rules SET last_supported_at = ? WHERE id = ?').run(nowIso(), id);
}

/**
 * Edits a rule's own wording directly — the screen's own detail-view Save
 * button. Deliberately does NOT write an improvement_changes row itself
 * (unlike memory-store.js's updateMemory(), which owns its own version
 * history table) — the CALLER (the server.js route) records the change,
 * exactly the same way apply.js already does for a freshly-applied rule,
 * so an edit rides the SAME undo machinery as a creation rather than a
 * second, parallel history mechanism.
 */
export function updateRuleText(id, text) {
  const trimmed = String(text || '').trim();
  if (!trimmed) throw new Error('A rule needs some text.');
  getDb().prepare('UPDATE improvement_rules SET text = ?, updated_at = ? WHERE id = ?').run(trimmed, nowIso(), id);
  return getRule(id);
}

/**
 * Archives a rule — hides it from every ordinary list AND deactivates it
 * (see db.js's migration-8 comment: archiving always also stops a rule
 * applying, so there is never a confusing "archived but still live"
 * state). The safe, everyday "get this out of my way" action; never a hard
 * delete.
 */
export function archiveRule(id) {
  getDb().prepare('UPDATE improvement_rules SET archived_at = ?, active = 0, updated_at = ? WHERE id = ?').run(nowIso(), nowIso(), id);
  return getRule(id);
}

/** Restores an archived rule back into the ordinary lists — deliberately comes back INACTIVE (never silently reactivates something the user chose to put away); the user re-enables it explicitly via the existing mute toggle if they want it live again. */
export function restoreRule(id) {
  getDb().prepare('UPDATE improvement_rules SET archived_at = NULL, updated_at = ? WHERE id = ?').run(nowIso(), id);
  return getRule(id);
}

/** Hard delete — only ever reachable from the archived view on the screen. See apply.js's undoChange() for how a Change row pointing at a now-deleted rule is handled honestly rather than silently. */
export function deleteRule(id) {
  getDb().prepare('DELETE FROM improvement_rules WHERE id = ?').run(id);
}

// Same hard-ceiling discipline as memory-store.js's approvedMemoriesText() —
// what keeps this affordable regardless of how many rules accumulate.
const MAX_INJECTED_RULES = 20;
const MAX_INJECTED_CHARS = 3000;

/**
 * Rules formatted for injection into the system prompt (prompt.js's
 * improvementSection()). `scope: 'general'` (no argument) returns only
 * general-purpose rules — the ones safe to cache in the `stable` prefix,
 * since they don't vary per turn. Passing a specific scope (e.g.
 * 'job_kind:research') returns rules for THAT scope only — a caller wanting
 * both calls this twice. Ordering is oldest-first (a rule earned by more
 * evidence over time reads first, mirroring how a person would list settled
 * habits before newer ones) and capped the same way memories are, so the
 * prompt never grows unbounded as rules accumulate. A rule with a pending
 * conflict against it is silently excluded — see conflictedRuleIds() above.
 */
export function activeRulesText({ scope = 'general' } = {}) {
  const conflicted = conflictedRuleIds();
  const rules = listRules({ activeOnly: true, scope }).filter((r) => !conflicted.has(r.id));
  if (!rules.length) return '';

  const lines = [];
  let used = 0;
  let count = 0;
  for (const r of rules) {
    if (count >= MAX_INJECTED_RULES) break;
    const line = `- ${r.text}`;
    if (used + line.length > MAX_INJECTED_CHARS) break;
    lines.push(line);
    used += line.length;
    count++;
  }
  return lines.join('\n');
}

// ---------- change / undo log ----------

/** Records ONE applied change — `before`/`after` are plain JS values (JSON-encoded here). Always the moment a change actually takes effect, never before. */
export function recordChange({ kind, target, before, after, reason = null, proposalId = null }) {
  if (!kind || !target) throw new Error('A change needs a kind and a target.');
  const db = getDb();
  const ts = nowIso();
  const result = db
    .prepare(
      `INSERT INTO improvement_changes (kind, target, before, after, reason, proposal_id, applied_at)
       VALUES (?, ?, ?, ?, ?, ?, ?)`
    )
    .run(kind, target, before === undefined ? null : JSON.stringify(before), after === undefined ? null : JSON.stringify(after), reason, proposalId, ts);
  return rowToChange(db.prepare('SELECT * FROM improvement_changes WHERE id = ?').get(result.lastInsertRowid));
}

export function getChange(id) {
  const row = getDb().prepare('SELECT * FROM improvement_changes WHERE id = ?').get(id);
  return row ? rowToChange(row) : null;
}

export function listChanges({ limit = 100 } = {}) {
  const rows = getDb().prepare('SELECT * FROM improvement_changes ORDER BY applied_at DESC LIMIT ?').all(limit);
  return rows.map(rowToChange);
}

/** Marks a change as undone — apply.js calls this AFTER it has actually reverted the target; this alone never reverts anything. */
export function markChangeUndone(id) {
  getDb().prepare('UPDATE improvement_changes SET undone_at = ? WHERE id = ?').run(nowIso(), id);
  return getChange(id);
}

// ---------- budget ledger ----------
//
// A real spend cap, not a hoped-for cadence — reuses db.js's existing
// app_state key/value table (the same one chat-store.js's active
// conversation id lives in) rather than a new table, since this is exactly
// the shape app_state already exists for: a handful of small, singleton,
// non-relational facts. Two independent buckets: 'daily' (reflect +
// synthesize combined) resets every calendar day, 'weekly' (research +
// life-patterns combined) resets every 7 days from its own first use.

export const DAILY_BUDGET = 2;
export const WEEKLY_BUDGET = 4;

function todayKey() {
  return new Date().toISOString().slice(0, 10); // 'YYYY-MM-DD'
}

function readAppState(key, fallback) {
  const row = getDb().prepare('SELECT value FROM app_state WHERE key = ?').get(key);
  if (!row) return fallback;
  return parseJson(row.value, fallback);
}

function writeAppState(key, value) {
  getDb()
    .prepare('INSERT INTO app_state (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value')
    .run(key, JSON.stringify(value));
}

/** True if a daily-budget call may proceed — does NOT itself consume the budget (use tryConsumeDailyBudget for that); useful for a caller that wants to check before doing other gate work. */
export function dailyBudgetRemaining() {
  const state = readAppState('improvement_daily_budget', { day: todayKey(), used: 0 });
  if (state.day !== todayKey()) return DAILY_BUDGET;
  return Math.max(0, DAILY_BUDGET - state.used);
}

/** Atomically consumes one daily-budget unit if any remains; returns true if consumed, false if the budget is already spent for today. Rolls over automatically on a new day. */
export function tryConsumeDailyBudget() {
  const today = todayKey();
  const state = readAppState('improvement_daily_budget', { day: today, used: 0 });
  const current = state.day === today ? state : { day: today, used: 0 };
  if (current.used >= DAILY_BUDGET) return false;
  writeAppState('improvement_daily_budget', { day: today, used: current.used + 1 });
  return true;
}

function weekKey(state) {
  // A rolling 7-day window from whenever the ledger was first touched,
  // rather than a fixed Monday-start week — simpler, and "how many calls in
  // the last 7 days" is the actual thing being budgeted, not "this
  // calendar week."
  if (!state?.weekStart) return { weekStart: new Date().toISOString(), used: 0 };
  const start = Date.parse(state.weekStart);
  if (!Number.isFinite(start) || Date.now() - start > 7 * 24 * 60 * 60 * 1000) {
    return { weekStart: new Date().toISOString(), used: 0 };
  }
  return state;
}

export function weeklyBudgetRemaining() {
  const state = weekKey(readAppState('improvement_weekly_budget', null));
  return Math.max(0, WEEKLY_BUDGET - state.used);
}

export function tryConsumeWeeklyBudget() {
  const state = weekKey(readAppState('improvement_weekly_budget', null));
  if (state.used >= WEEKLY_BUDGET) return false;
  writeAppState('improvement_weekly_budget', { weekStart: state.weekStart, used: state.used + 1 });
  return true;
}

/** The last time reflect.js/synthesize.js actually ran, per app_state key — cheap cadence gates (">= 4h since last reflect") sit on top of the budget above; both must pass. */
export function getLastRunAt(key) {
  return readAppState(`improvement_last_${key}`, null);
}

export function setLastRunAt(key) {
  writeAppState(`improvement_last_${key}`, nowIso());
}

// Which lessons improve-research.js has already spent a weekly-budget call
// investigating — so the same well-evidenced lesson isn't picked again
// every week once it's already been looked into (whether or not it turned
// up anything). A plain array in app_state; the set stays small (bounded by
// how many well-evidenced lessons ever exist) so no separate table is
// worth it.
export function wasLessonResearched(lessonId) {
  const ids = readAppState('improvement_researched_lesson_ids', []);
  return Array.isArray(ids) && ids.includes(lessonId);
}

export function markLessonResearched(lessonId) {
  const ids = readAppState('improvement_researched_lesson_ids', []);
  const next = Array.isArray(ids) ? ids : [];
  if (!next.includes(lessonId)) next.push(lessonId);
  writeAppState('improvement_researched_lesson_ids', next);
}

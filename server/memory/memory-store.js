// The Memory Manager — the complete backend for browsing, searching,
// editing, merging, archiving, restoring, and deleting durable facts about
// the user, plus the pending-candidate queue and category taxonomy that sit
// in front of it. Real and complete, on purpose: this is the whole engine,
// not a stub — see root CLAUDE.md's Memory section. There is deliberately
// NO screen for it; every capability here is reached only through
// conversation (server/skills/remember_about_me.js, forget_something.js,
// update_memory.js, review_memories.js) or the review-card REST routes in
// server.js. If a visual browser is ever wanted, it's a matter of exposing
// what already exists here, not rebuilding it.
//
// Leaf module: imports only db.js (itself a leaf). Safe for anything under
// server/skills/ to import — see root CLAUDE.md's circular-import
// invariant.
//
// Storage choice: plain SQLite tables (db.js), not JSON — deliberately the
// ONE inconsistency with the rest of data/*.json. A future self-improvement
// system writes into this store (per the user's own stated reason for
// building Memory at all), and the tables here are small enough that
// SQLite's real transactions matter more than JSON's hand-editability. See
// db.js's migration-2 comment for the independence guarantee this schema
// enforces structurally.

import { getDb } from '../db.js';

function makeId(prefix) {
  return `${prefix}${Date.now().toString(36)}${Math.random().toString(36).slice(2, 8)}`;
}

function nowIso() {
  return new Date().toISOString();
}

function rowToMemory(row) {
  return {
    id: row.id,
    category: row.category,
    text: row.text,
    sourceKind: row.source_kind,
    sourceRef: row.source_ref,
    createdAt: row.created_at,
    updatedAt: row.updated_at,
    archived: Boolean(row.archived),
    confidence: row.confidence ?? null,
    // How consent was given — 'approved' (user approved a candidate),
    // 'auto' (cleared the trust threshold, see memory-policy.js),
    // 'explicit' (remember_about_me/update_memory — the user said it
    // directly), 'legacy' (migrated from the old profile.json). Distinct
    // from sourceKind, which records WHERE the content came from, not how
    // it got consent.
    origin: row.origin,
  };
}

function rowToVersion(row) {
  return { id: row.id, text: row.text, category: row.category, changedAt: row.changed_at, reason: row.reason };
}

function rowToCandidate(row) {
  return {
    id: row.id,
    conversationId: row.conversation_id,
    sourceKind: row.source_kind,
    sourceRef: row.source_ref,
    category: row.category,
    text: row.text,
    conflictWith: row.conflict_with,
    status: row.status,
    createdAt: row.created_at,
    resolvedAt: row.resolved_at,
    confidence: row.confidence ?? null,
  };
}

// ---------- memories ----------

export function listMemories({ category, includeArchived = false, query, origin } = {}) {
  const db = getDb();
  const clauses = [];
  const params = [];
  if (!includeArchived) clauses.push('archived = 0');
  if (category) {
    clauses.push('category = ?');
    params.push(category);
  }
  if (origin) {
    clauses.push('origin = ?');
    params.push(origin);
  }
  if (query && String(query).trim()) {
    clauses.push('text LIKE ?');
    params.push(`%${String(query).trim()}%`);
  }
  const where = clauses.length ? `WHERE ${clauses.join(' AND ')}` : '';
  const rows = db.prepare(`SELECT * FROM memories ${where} ORDER BY updated_at DESC`).all(...params);
  return rows.map(rowToMemory);
}

export function getMemory(id) {
  const row = getDb().prepare('SELECT * FROM memories WHERE id = ?').get(id);
  return row ? rowToMemory(row) : null;
}

/**
 * Ids of memories a still-PENDING candidate says contradicts what's
 * currently saved (memory_candidates.conflict_with, status='pending') —
 * used by approvedMemoriesText() to hold a contradicted memory out of the
 * prompt until the user actually resolves the conflict on the Memory
 * screen. memory-policy.js already makes a conflicting candidate
 * un-auto-resolvable at every trust level (a conflict always needs a human
 * decision) — this is the other half of that: while it waits, the OLD,
 * possibly-wrong memory shouldn't keep being asserted to the model as
 * settled fact every single turn. Confirmed live: a memory reading "User
 * uses a Mac for development" kept being injected while a pending
 * candidate said the opposite, directly contradicting the base system
 * prompt's own "present with them on their Windows PC" line.
 */
function conflictedMemoryIds() {
  const rows = getDb()
    .prepare(`SELECT DISTINCT conflict_with AS id FROM memory_candidates WHERE status = 'pending' AND conflict_with IS NOT NULL`)
    .all();
  return new Set(rows.map((r) => r.id));
}

// Hard ceiling on what gets injected into the system prompt, regardless of
// how large the underlying memory set grows — this is what keeps
// "curation is what keeps this affordable" (root CLAUDE.md's Memory
// section) true even once memoryTrust:'auto' lets memories accumulate with
// no per-save review. listMemories() already returns newest-updated first,
// so capping is just "keep the front of the list".
const MAX_INJECTED_MEMORIES = 30;
const MAX_INJECTED_CHARS = 6000;

/** "Aug 12, 2026" — an absolute date, not a relative one, so it stays true no matter how long the memory sits in the prompt unread before the model actually processes this turn. */
function shortDate(iso) {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return null;
  return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
}

/**
 * Every approved, non-archived, non-conflicted memory, formatted for
 * injection into the system prompt (see server/prompt.js) — grouped by
 * category so it reads as organized context, not a flat dump, and dated so
 * the model can tell how old a note is (the same discipline
 * search_conversations.js's own tool already asks the model to apply to
 * anything it recalls — a memory is no different: an old note isn't
 * automatically still true). This is the whole of "recall": the set is
 * small enough to just always be in context, so nothing needs to be
 * searched for at answer time — capped (see MAX_INJECTED_MEMORIES/CHARS)
 * so that stays true as the store grows, rather than an assumption that
 * quietly stops holding.
 */
export function approvedMemoriesText() {
  const conflicted = conflictedMemoryIds();
  const memories = listMemories({}).filter((m) => !conflicted.has(m.id));
  if (!memories.length) return '';

  const byCategory = new Map();
  let used = 0;
  let count = 0;
  for (const m of memories) {
    if (count >= MAX_INJECTED_MEMORIES) break;
    const date = shortDate(m.updatedAt || m.createdAt);
    const line = `- ${m.text}${date ? ` (noted ${date})` : ''}`;
    if (used + line.length > MAX_INJECTED_CHARS) break;
    if (!byCategory.has(m.category)) byCategory.set(m.category, []);
    byCategory.get(m.category).push(line);
    used += line.length;
    count++;
  }

  const lines = [];
  for (const [category, entries] of byCategory) {
    lines.push(`${category}:`);
    lines.push(...entries);
  }
  return lines.join('\n');
}

/** Creates a memory directly, bypassing the candidate/approval queue — only for callers where the user has ALREADY explicitly consented (e.g. remember_about_me's own confirm read-back). Writes an initial version row too, so history reads "created" from the start. `origin` records HOW consent was given (defaults to 'approved' — the right default for every existing caller, all of which go through some form of user approval); `confidence` is the extraction model's own score, null for anything not extracted (explicit tools never set it). */
export function createMemory({ category, text, sourceKind, sourceRef, confidence = null, origin = 'approved' } = {}) {
  const trimmedText = String(text || '').trim();
  if (!trimmedText) throw new Error('A memory needs some text.');
  const cat = String(category || 'Uncategorized').trim() || 'Uncategorized';
  const db = getDb();
  const id = makeId('mem');
  const ts = nowIso();
  db.exec('BEGIN');
  try {
    db.prepare(
      'INSERT INTO memories (id, category, text, source_kind, source_ref, created_at, updated_at, archived, confidence, origin) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?)'
    ).run(id, cat, trimmedText, sourceKind || null, sourceRef || null, ts, ts, confidence, origin);
    db.prepare('INSERT INTO memory_versions (memory_id, text, category, changed_at, reason) VALUES (?, ?, ?, ?, ?)').run(
      id,
      trimmedText,
      cat,
      ts,
      'Created.'
    );
    db.exec('COMMIT');
  } catch (err) {
    db.exec('ROLLBACK');
    throw err;
  }
  return getMemory(id);
}

/**
 * Edits a memory's text/category, recording the PRE-edit state as a version
 * row first — never a silent overwrite. `reason` is a short human-readable
 * note ("user requested update via conversation", "merged with <id>", ...).
 * `origin`, if given, replaces the memory's origin badge too — used when
 * the user EXPLICITLY corrects a memory's content (update_memory.js), since
 * the badge should reflect how the CURRENT text got its consent, not just
 * how the row was first created; omitted for edits that aren't a fresh
 * user statement (e.g. mergeMemories()'s bookkeeping calls below).
 */
export function updateMemory(id, { text, category } = {}, reason = 'Edited.', origin) {
  const existing = getDb().prepare('SELECT * FROM memories WHERE id = ?').get(id);
  if (!existing) throw new Error('That memory no longer exists.');
  const db = getDb();
  const ts = nowIso();
  const nextText = text !== undefined ? String(text).trim() : existing.text;
  const nextCategory = category !== undefined ? String(category).trim() || existing.category : existing.category;
  const nextOrigin = origin !== undefined ? origin : existing.origin;
  if (!nextText) throw new Error('A memory needs some text.');

  db.exec('BEGIN');
  try {
    // The version row records what it USED TO say, right before overwriting it.
    db.prepare('INSERT INTO memory_versions (memory_id, text, category, changed_at, reason) VALUES (?, ?, ?, ?, ?)').run(
      id,
      existing.text,
      existing.category,
      ts,
      reason
    );
    db.prepare('UPDATE memories SET text = ?, category = ?, origin = ?, updated_at = ? WHERE id = ?').run(
      nextText,
      nextCategory,
      nextOrigin,
      ts,
      id
    );
    db.exec('COMMIT');
  } catch (err) {
    db.exec('ROLLBACK');
    throw err;
  }
  return getMemory(id);
}

export function archiveMemory(id) {
  getDb().prepare('UPDATE memories SET archived = 1, updated_at = ? WHERE id = ?').run(nowIso(), id);
  return getMemory(id);
}

export function restoreMemory(id) {
  getDb().prepare('UPDATE memories SET archived = 0, updated_at = ? WHERE id = ?').run(nowIso(), id);
  return getMemory(id);
}

/** Hard delete — the memory and its version history both go. Used sparingly (forget_something.js); archiving is the softer, reversible default. */
export function deleteMemory(id) {
  getDb().prepare('DELETE FROM memories WHERE id = ?').run(id);
}

export function getVersionHistory(memoryId) {
  const rows = getDb().prepare('SELECT * FROM memory_versions WHERE memory_id = ? ORDER BY changed_at DESC').all(memoryId);
  return rows.map(rowToVersion);
}

/** Folds `otherIds` into `primaryId` — primary's text/category is replaced with the merged result (versioned, like any edit) and the others are archived (never deleted — their own version history stays intact and answerable). */
export function mergeMemories(primaryId, otherIds, mergedText, mergedCategory) {
  const primary = getMemory(primaryId);
  if (!primary) throw new Error('That memory no longer exists.');
  const updated = updateMemory(primaryId, { text: mergedText, category: mergedCategory }, `Merged with ${otherIds.join(', ')}.`);
  for (const otherId of otherIds) {
    if (otherId === primaryId) continue;
    const other = getMemory(otherId);
    if (!other) continue;
    updateMemory(otherId, {}, `Merged into ${primaryId}.`);
    archiveMemory(otherId);
  }
  return updated;
}

// ---------- categories ----------

export function listCategories({ includePending = true } = {}) {
  const rows = getDb().prepare('SELECT * FROM memory_categories ORDER BY name ASC').all();
  return rows.filter((r) => includePending || r.status === 'approved').map((r) => ({ name: r.name, status: r.status }));
}

/** Registers a proposed category as 'pending' if it doesn't already exist (approved or pending) — never overwrites an existing one. */
export function proposeCategory(name) {
  const trimmed = String(name || '').trim();
  if (!trimmed) return null;
  getDb().prepare('INSERT OR IGNORE INTO memory_categories (name, status) VALUES (?, ?)').run(trimmed, 'pending');
  return getDb().prepare('SELECT * FROM memory_categories WHERE name = ?').get(trimmed);
}

export function approveCategory(name) {
  getDb().prepare('UPDATE memory_categories SET status = ? WHERE name = ?').run('approved', name);
}

/** Renames every candidate/memory using `from` to `to`, then drops `from` — used when the user decides a proposed category duplicates an existing one. */
export function mergeCategoryInto(from, to) {
  const db = getDb();
  db.exec('BEGIN');
  try {
    db.prepare('UPDATE memories SET category = ? WHERE category = ?').run(to, from);
    db.prepare('UPDATE memory_candidates SET category = ? WHERE category = ?').run(to, from);
    db.prepare('DELETE FROM memory_categories WHERE name = ?').run(from);
    db.exec('COMMIT');
  } catch (err) {
    db.exec('ROLLBACK');
    throw err;
  }
}

// ---------- candidates (the pending-approval queue) ----------

/** Adds the conflicting memory's CURRENT text (display convenience for a review card's old-vs-new view) — a candidate only stores the id, not a snapshot, since the memory could itself have changed since the candidate was created. */
export function hydrateCandidate(candidate) {
  if (!candidate?.conflictWith) return candidate;
  const conflictMemory = getMemory(candidate.conflictWith);
  return { ...candidate, conflictText: conflictMemory?.text || null };
}

export function listPendingCandidates() {
  const rows = getDb().prepare("SELECT * FROM memory_candidates WHERE status = 'pending' ORDER BY created_at ASC").all();
  return rows.map(rowToCandidate).map(hydrateCandidate);
}

export function getCandidate(id) {
  const row = getDb().prepare('SELECT * FROM memory_candidates WHERE id = ?').get(id);
  return row ? rowToCandidate(row) : null;
}

/** Written the moment a candidate exists — crash-safe by construction, since it's just a normal committed SQLite row, not something held in process memory waiting to be flushed. `confidence` is the extraction model's own 0..1 score, consulted by memory-policy.js's decide() to route this candidate. */
export function createCandidate({ conversationId, sourceKind, sourceRef, category, text, conflictWith, confidence = null } = {}) {
  const trimmedText = String(text || '').trim();
  if (!trimmedText) return null;
  const db = getDb();
  const id = makeId('cand');
  db.prepare(
    'INSERT INTO memory_candidates (id, conversation_id, source_kind, source_ref, category, text, conflict_with, status, created_at, confidence) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)'
  ).run(id, conversationId || null, sourceKind || 'chat', sourceRef || null, category || 'Uncategorized', trimmedText, conflictWith || null, 'pending', nowIso(), confidence);
  return getCandidate(id);
}

/**
 * Shared core of turning a pending candidate into a real memory — creates
 * the memory (carrying the candidate's own confidence score forward),
 * approves a pending category the moment anything in it is saved (whether
 * by a human clicking Approve or by clearing the auto-save threshold), and
 * marks the candidate resolved with the given status. `approveCandidate()`
 * and `autoApproveCandidate()` both call this and ONLY this — one code path
 * for "a candidate became a memory" means the two can never quietly diverge
 * in what a saved memory ends up looking like.
 */
function resolveCandidateIntoMemory(id, edits, { origin, status }) {
  const candidate = getCandidate(id);
  if (!candidate) throw new Error('That memory suggestion is no longer pending.');
  const text = edits.text !== undefined ? edits.text : candidate.text;
  const category = edits.category !== undefined ? edits.category : candidate.category;

  const memory = createMemory({
    category,
    text,
    sourceKind: candidate.sourceKind,
    sourceRef: candidate.sourceRef,
    confidence: candidate.confidence,
    origin,
  });
  if (candidate.category) approveCategory(candidate.category); // a category that was pending is approved the moment anything in it is saved
  getDb().prepare('UPDATE memory_candidates SET status = ?, resolved_at = ? WHERE id = ?').run(status, nowIso(), id);
  return memory;
}

/** Approves a candidate into a real memory — `edits` (optional) lets the user change the text/category before it's saved, per the spec's "approve, reject, or edit" requirement. */
export function approveCandidate(id, edits = {}) {
  return resolveCandidateIntoMemory(id, edits, { origin: 'approved', status: 'approved' });
}

/**
 * Auto-approves a candidate that cleared the trust threshold — see
 * memory-policy.js's decide(). Never called for a candidate carrying a
 * conflict (decide() always routes those to require-approval instead), so
 * this path never needs edits: nothing here is a human correcting text
 * before it saves, it's the model's own extraction going straight through.
 */
export function autoApproveCandidate(id) {
  return resolveCandidateIntoMemory(id, {}, { origin: 'auto', status: 'auto_approved' });
}

export function rejectCandidate(id) {
  getDb().prepare("UPDATE memory_candidates SET status = 'rejected', resolved_at = ? WHERE id = ?").run(nowIso(), id);
}

/** Resolves a conflict candidate per the user's explicit choice — 'update' replaces the old memory's text, 'keep_both' approves the candidate as a separate memory, 'discard' rejects it outright. Never resolved automatically. */
export function resolveConflict(id, choice, edits = {}) {
  const candidate = getCandidate(id);
  if (!candidate) throw new Error('That memory suggestion is no longer pending.');
  if (choice === 'discard') {
    rejectCandidate(id);
    return null;
  }
  if (choice === 'update' && candidate.conflictWith) {
    const text = edits.text !== undefined ? edits.text : candidate.text;
    const category = edits.category !== undefined ? edits.category : candidate.category;
    // origin: 'approved' — the user just clicked "Update the old one" on
    // the review card, the same explicit approval approveCandidate() below
    // records for a plain (non-conflict) candidate.
    const updated = updateMemory(candidate.conflictWith, { text, category }, 'Updated from a conflicting memory suggestion.', 'approved');
    getDb().prepare("UPDATE memory_candidates SET status = 'approved', resolved_at = ? WHERE id = ?").run(nowIso(), id);
    return updated;
  }
  // 'keep_both' (or an update whose target memory vanished in the meantime) — approve as a new, separate memory.
  return approveCandidate(id, edits);
}

// ---------- checkpoint bookkeeping (memory-review.js) ----------

export function getCheckpoint(conversationId) {
  const row = getDb().prepare('SELECT * FROM memory_checkpoints WHERE conversation_id = ?').get(conversationId);
  return row ? { lastSeq: row.last_seq, checkedAt: row.checked_at } : { lastSeq: 0, checkedAt: null };
}

export function setCheckpoint(conversationId, lastSeq) {
  getDb()
    .prepare(
      'INSERT INTO memory_checkpoints (conversation_id, last_seq, checked_at) VALUES (?, ?, ?) ' +
        'ON CONFLICT(conversation_id) DO UPDATE SET last_seq = excluded.last_seq, checked_at = excluded.checked_at'
    )
    .run(conversationId, lastSeq, nowIso());
}

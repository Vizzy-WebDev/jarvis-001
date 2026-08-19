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
  };
}

// ---------- memories ----------

export function listMemories({ category, includeArchived = false, query } = {}) {
  const db = getDb();
  const clauses = [];
  const params = [];
  if (!includeArchived) clauses.push('archived = 0');
  if (category) {
    clauses.push('category = ?');
    params.push(category);
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
 * Every approved, non-archived memory, formatted for injection into the
 * system prompt (see server/prompt.js) — grouped by category so it reads as
 * organized context, not a flat dump. This is the whole of "recall": the
 * set is small enough to just always be in context, so nothing needs to be
 * searched for at answer time.
 */
export function approvedMemoriesText() {
  const memories = listMemories({});
  if (!memories.length) return '';
  const byCategory = new Map();
  for (const m of memories) {
    if (!byCategory.has(m.category)) byCategory.set(m.category, []);
    byCategory.get(m.category).push(m.text);
  }
  const lines = [];
  for (const [category, texts] of byCategory) {
    lines.push(`${category}:`);
    for (const t of texts) lines.push(`- ${t}`);
  }
  return lines.join('\n');
}

/** Creates a memory directly, bypassing the candidate/approval queue — only for callers where the user has ALREADY explicitly consented (e.g. remember_about_me's own confirm read-back). Writes an initial version row too, so history reads "created" from the start. */
export function createMemory({ category, text, sourceKind, sourceRef } = {}) {
  const trimmedText = String(text || '').trim();
  if (!trimmedText) throw new Error('A memory needs some text.');
  const cat = String(category || 'Uncategorized').trim() || 'Uncategorized';
  const db = getDb();
  const id = makeId('mem');
  const ts = nowIso();
  db.exec('BEGIN');
  try {
    db.prepare(
      'INSERT INTO memories (id, category, text, source_kind, source_ref, created_at, updated_at, archived) VALUES (?, ?, ?, ?, ?, ?, ?, 0)'
    ).run(id, cat, trimmedText, sourceKind || null, sourceRef || null, ts, ts);
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

/** Edits a memory's text/category, recording the PRE-edit state as a version row first — never a silent overwrite. `reason` is a short human-readable note ("user requested update via conversation", "merged with <id>", ...). */
export function updateMemory(id, { text, category } = {}, reason = 'Edited.') {
  const existing = getDb().prepare('SELECT * FROM memories WHERE id = ?').get(id);
  if (!existing) throw new Error('That memory no longer exists.');
  const db = getDb();
  const ts = nowIso();
  const nextText = text !== undefined ? String(text).trim() : existing.text;
  const nextCategory = category !== undefined ? String(category).trim() || existing.category : existing.category;
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
    db.prepare('UPDATE memories SET text = ?, category = ?, updated_at = ? WHERE id = ?').run(nextText, nextCategory, ts, id);
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

/** Written the moment a candidate exists — crash-safe by construction, since it's just a normal committed SQLite row, not something held in process memory waiting to be flushed. */
export function createCandidate({ conversationId, sourceKind, sourceRef, category, text, conflictWith } = {}) {
  const trimmedText = String(text || '').trim();
  if (!trimmedText) return null;
  const db = getDb();
  const id = makeId('cand');
  db.prepare(
    'INSERT INTO memory_candidates (id, conversation_id, source_kind, source_ref, category, text, conflict_with, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)'
  ).run(id, conversationId || null, sourceKind || 'chat', sourceRef || null, category || 'Uncategorized', trimmedText, conflictWith || null, 'pending', nowIso());
  return getCandidate(id);
}

/** Approves a candidate into a real memory — `edits` (optional) lets the user change the text/category before it's saved, per the spec's "approve, reject, or edit" requirement. */
export function approveCandidate(id, edits = {}) {
  const candidate = getCandidate(id);
  if (!candidate) throw new Error('That memory suggestion is no longer pending.');
  const text = edits.text !== undefined ? edits.text : candidate.text;
  const category = edits.category !== undefined ? edits.category : candidate.category;

  const memory = createMemory({ category, text, sourceKind: candidate.sourceKind, sourceRef: candidate.sourceRef });
  if (candidate.category) approveCategory(candidate.category); // a category that was pending is approved the moment anything in it is approved
  getDb().prepare("UPDATE memory_candidates SET status = 'approved', resolved_at = ? WHERE id = ?").run(nowIso(), id);
  return memory;
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
    const updated = updateMemory(candidate.conflictWith, { text, category }, 'Updated from a conflicting memory suggestion.');
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

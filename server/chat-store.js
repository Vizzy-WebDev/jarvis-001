// Chat History persistence — every conversation's full transcript, on disk,
// surviving a restart. Built on db.js's SQLite connection; nothing else in
// the project touches SQLite directly for this data.
//
// A "conversation" here is the persisted record; server/conversation.js's
// in-memory session is the trimmed working copy the model actually sees
// (capped at 60 messages, same as before) — this store keeps everything,
// forever, so nothing is lost to that cap the way it used to be.
//
// Leaf module: imports only db.js (itself a leaf) — safe for anything under
// server/skills/ to reach transitively without tripping the
// loader/runner/scheduler circular-import invariant in root CLAUDE.md.

import { getDb } from './db.js';

const TITLE_MAX = 60;

function makeId() {
  return `c${Date.now().toString(36)}${Math.random().toString(36).slice(2, 8)}`;
}

function nowIso() {
  return new Date().toISOString();
}

function titleFromText(text) {
  const trimmed = String(text || '').trim().replace(/\s+/g, ' ');
  if (!trimmed) return 'New chat';
  return trimmed.length > TITLE_MAX ? `${trimmed.slice(0, TITLE_MAX - 1)}…` : trimmed;
}

function rowToConversation(row) {
  return {
    id: row.id,
    title: row.title,
    createdAt: row.created_at,
    updatedAt: row.updated_at,
    pinned: Boolean(row.pinned),
    archived: Boolean(row.archived),
    messageCount: row.message_count ?? undefined,
  };
}

/** Decodes one messages row back into a neutral message (conversation.js's shape). */
function rowToMessage(row) {
  const extra = row.payload ? JSON.parse(row.payload) : {};
  // createdAt comes from this row's own created_at column, not the JSON
  // payload — see conversation.js's push()/hydrate() for why it's kept out
  // of the payload (avoids storing the same timestamp twice).
  return { id: `m${row.id}`, role: row.role, text: row.text ?? undefined, createdAt: row.created_at, ...extra };
}

/**
 * FTS5 MATCH needs its query text quoted to be treated as a literal
 * phrase/token search rather than parsed as FTS query syntax (a raw
 * "C++ setup?" would otherwise throw a syntax error inside MATCH — quoting
 * makes it a literal phrase, not an operator sequence). Used by
 * listConversations() below, unchanged from before this file's other
 * search-related additions — this is a human typing into a search box
 * expecting a literal phrase/substring match, and changing that behavior
 * was explicitly out of scope here.
 */
function ftsPhrase(q) {
  return `"${q.replace(/"/g, '""')}"`;
}

/**
 * Builds an FTS5 query that matches messages containing ALL of `q`'s words,
 * in ANY order or position — unlike ftsPhrase() above, this does NOT
 * require them adjacent. Used only by searchMessages() below, for a
 * different kind of caller: a model handed a few keywords (per
 * server/tools/search_conversations.js), not a human typing a literal
 * phrase. Confirmed live (not assumed) that ftsPhrase()'s whole-string
 * phrase-quote — correct for a human's literal search box — returns ZERO
 * hits for a multi-keyword model query whenever the words aren't adjacent
 * in that exact order in the original message, which is the common case,
 * not the exception. The fix: quote each word individually (so a token
 * like "C++", an apostrophe, or the literal word "NOT" can never be
 * misread as FTS query syntax — confirmed live against exactly those
 * cases), then join with a plain space, which FTS5 treats as AND between
 * whole, already-literal tokens rather than a phrase requiring adjacency.
 */
function quotedTerms(q) {
  return String(q)
    .trim()
    .split(/\s+/)
    .filter(Boolean)
    .map((t) => `"${t.replace(/"/g, '""')}"`);
}

function ftsAndTerms(q) {
  return quotedTerms(q).join(' ');
}

/**
 * Same literal, injection-safe individual-term quoting as ftsAndTerms()
 * above, but OR'd instead of AND'd — bm25's own ranking still puts a
 * message matching MORE of the terms above one matching fewer, so this
 * isn't a loss of precision, just a widening of what's allowed to match at
 * all. Exists for searchMessages()'s fallback below: confirmed live that a
 * long, generic keyword reduction (research.js's toSearchQuery(), used when
 * a model hands over a whole question instead of a few keywords) routinely
 * includes words that were never actually IN the original message at all
 * ("switching" for a message that said "switched", "development" for
 * "dev") — requiring ALL of a 9-word reduction to literally match produces
 * a false negative on exactly the kind of question this fallback exists to
 * rescue. AND first (precise), OR only if AND finds nothing (recall) is the
 * same graceful degradation a normal search engine does.
 */
function ftsOrTerms(q) {
  return quotedTerms(q).join(' OR ');
}

/**
 * All conversations, newest-updated first (pinned always ahead of unpinned).
 * `query`, if given, full-text-searches message bodies (via FTS5) as well as
 * matching conversation titles — either match surfaces the conversation.
 * Archived conversations are excluded unless `includeArchived` is true.
 */
export function listConversations({ query, includeArchived = false } = {}) {
  const db = getDb();
  const q = String(query || '').trim();

  let rows;
  if (q) {
    rows = db
      .prepare(
        `SELECT c.*, (SELECT COUNT(*) FROM messages m WHERE m.conversation_id = c.id) AS message_count
         FROM conversations c
         WHERE (${includeArchived ? '1=1' : 'c.archived = 0'})
           AND (
             c.title LIKE ?
             OR c.id IN (
               SELECT m.conversation_id FROM messages m
               JOIN messages_fts f ON f.rowid = m.id
               WHERE f.text MATCH ?
             )
           )
         ORDER BY c.pinned DESC, c.updated_at DESC`
      )
      .all(`%${q}%`, ftsPhrase(q));
  } else {
    rows = db
      .prepare(
        `SELECT c.*, (SELECT COUNT(*) FROM messages m WHERE m.conversation_id = c.id) AS message_count
         FROM conversations c
         WHERE (${includeArchived ? '1=1' : 'c.archived = 0'})
         ORDER BY c.pinned DESC, c.updated_at DESC`
      )
      .all();
  }
  return rows.map(rowToConversation);
}

export function getConversation(id) {
  const row = getDb().prepare('SELECT * FROM conversations WHERE id = ?').get(id);
  return row ? rowToConversation(row) : null;
}

export function isConversation(id) {
  return Boolean(getDb().prepare('SELECT 1 FROM conversations WHERE id = ?').get(id));
}

/** Every message in a conversation, oldest first. */
export function getMessages(id) {
  const rows = getDb().prepare('SELECT * FROM messages WHERE conversation_id = ? ORDER BY seq ASC').all(id);
  return rows.map(rowToMessage);
}

/**
 * Messages strictly after `afterSeq`, each carrying its own `seq` — used by
 * server/memory/memory-review.js so a checkpoint only ever analyses NEW
 * conversation content since the last one, never the whole transcript again.
 */
export function getMessagesSince(id, afterSeq = 0) {
  const rows = getDb()
    .prepare('SELECT * FROM messages WHERE conversation_id = ? AND seq > ? ORDER BY seq ASC')
    .all(id, afterSeq);
  return rows.map((row) => ({ ...rowToMessage(row), seq: row.seq }));
}

/**
 * Full-text search over EVERY conversation's messages (not just one — see
 * getMessages()/getMessagesSince() for that), ranked by FTS5's own bm25()
 * relevance score. Used by server/tools/search_conversations.js so the
 * model can answer "what did we decide about X" from past conversations
 * instead of only ever seeing the current one. A new function rather than
 * a change to listConversations() above — that function's job (browsing
 * the Chat History screen) is unaffected by this one existing.
 *
 * `excludeConversationId` leaves out the conversation the caller is
 * already in — its content is already in the model's context window, so
 * surfacing it again here would waste tokens and read as a non sequitur.
 * `includeArchived` defaults to false, matching listConversations()'s
 * default — an archived conversation is "put away", not gone, but a
 * background search shouldn't dredge it up unless asked to.
 */
function runMessageSearch(ftsQuery, { limit, excludeConversationId, includeArchived }) {
  const params = [ftsQuery];
  let excludeClause = '';
  if (excludeConversationId) {
    excludeClause = 'AND c.id != ?';
    params.push(excludeConversationId);
  }
  params.push(limit);

  const rows = getDb()
    .prepare(
      `SELECT m.id, m.conversation_id, m.role, m.created_at, c.title,
              snippet(messages_fts, 0, '', '', '…', 16) AS excerpt,
              bm25(messages_fts) AS rank
       FROM messages m
       JOIN messages_fts f ON f.rowid = m.id
       JOIN conversations c ON c.id = m.conversation_id
       WHERE f.text MATCH ?
         AND (${includeArchived ? '1=1' : 'c.archived = 0'})
         ${excludeClause}
       ORDER BY rank
       LIMIT ?`
    )
    .all(...params);

  return rows.map((row) => ({
    conversationId: row.conversation_id,
    title: row.title,
    role: row.role,
    createdAt: row.created_at,
    excerpt: row.excerpt,
  }));
}

export function searchMessages(query, { limit = 8, excludeConversationId = null, includeArchived = false } = {}) {
  const q = String(query || '').trim();
  if (!q) return [];

  const opts = { limit, excludeConversationId, includeArchived };
  const andQuery = ftsAndTerms(q);
  if (!andQuery) return [];

  const precise = runMessageSearch(andQuery, opts);
  if (precise.length) return precise;

  // Nothing matched every term literally — most often a long, generic
  // keyword reduction of a full question (see ftsOrTerms()'s comment).
  // Widening to OR only when the strict pass came back empty keeps a
  // genuinely well-targeted multi-keyword query at its original precision.
  return runMessageSearch(ftsOrTerms(q), opts);
}

export function createConversation() {
  const db = getDb();
  const id = makeId();
  const ts = nowIso();
  db.prepare('INSERT INTO conversations (id, title, created_at, updated_at, pinned, archived) VALUES (?, ?, ?, ?, 0, 0)').run(
    id,
    'New chat',
    ts,
    ts
  );
  return getConversation(id);
}

/**
 * Appends one neutral message to a conversation. `message` is
 * conversation.js's shape: {role, text?, toolCalls?, toolResults?, modelId?,
 * raw?, media?} — everything but role/text rides in `payload` as JSON.
 * Renames the conversation from its "New chat" placeholder the first time a
 * user message with text arrives, so titles need no separate model call.
 * The insert and the conversation's updated_at (and title) touch happen in
 * one transaction, so a crash mid-write can never leave the index pointing
 * at a conversation whose row count disagrees with what's really on disk.
 */
export function appendMessage(id, message) {
  const db = getDb();
  const { role, text, ...rest } = message;
  const hasExtra = Object.values(rest).some((v) => v !== undefined);
  const payload = hasExtra ? JSON.stringify(rest) : null;
  const ts = nowIso();

  db.exec('BEGIN');
  try {
    const { seq: lastSeq } =
      db.prepare('SELECT COALESCE(MAX(seq), 0) AS seq FROM messages WHERE conversation_id = ?').get(id) || { seq: 0 };
    const nextSeq = lastSeq + 1;

    db.prepare(
      'INSERT INTO messages (conversation_id, seq, role, text, payload, created_at) VALUES (?, ?, ?, ?, ?, ?)'
    ).run(id, nextSeq, role, text ?? null, payload, ts);

    const conv = db.prepare('SELECT title FROM conversations WHERE id = ?').get(id);
    const shouldTitle = conv && conv.title === 'New chat' && role === 'user' && text;
    if (shouldTitle) {
      db.prepare('UPDATE conversations SET updated_at = ?, title = ? WHERE id = ?').run(ts, titleFromText(text), id);
    } else {
      db.prepare('UPDATE conversations SET updated_at = ? WHERE id = ?').run(ts, id);
    }
    db.exec('COMMIT');
  } catch (err) {
    db.exec('ROLLBACK');
    throw err;
  }

  return getConversation(id);
}

/**
 * Merges `patch` into the payload of the most recent assistant message in a
 * conversation — used only for interrupt correctness (see conversation.js's
 * markLastAssistantInterrupted()), which needs to retroactively mark a
 * message `interrupted`/`spokenText` after it's already been persisted. A
 * genuine UPDATE, not an append — this is the one place chat history is
 * ever edited after the fact, and the reason is specifically that the
 * FULL generated text stays in `text` (nothing is ever deleted — a user
 * might still want to see what the model would have said), while
 * `spokenText` in payload is what the conversation history sent to the
 * model on the NEXT turn actually uses (see each adapter's message mapping)
 * — the model should believe it said only what was truly heard, not
 * everything it happened to finish generating after being cut off.
 * No-op (returns false) if there's no assistant message to patch — a race
 * where the interrupt POST arrives before pushAssistantText ever ran is
 * possible and is conversation.js's problem to handle, not this function's.
 */
export function updateLastAssistantMessage(id, patch) {
  const db = getDb();
  const row = db
    .prepare("SELECT id, payload FROM messages WHERE conversation_id = ? AND role = 'assistant' ORDER BY seq DESC LIMIT 1")
    .get(id);
  if (!row) return false;
  const existing = row.payload ? JSON.parse(row.payload) : {};
  const merged = { ...existing, ...patch };
  db.prepare('UPDATE messages SET payload = ? WHERE id = ?').run(JSON.stringify(merged), row.id);
  return true;
}

// Deliberately does not touch updated_at — renaming shouldn't bump a
// conversation to the top of the recency sort the way actually talking in
// it does.
export function renameConversation(id, title) {
  const trimmed = String(title || '').trim();
  if (!trimmed) throw new Error('A conversation needs a title.');
  getDb().prepare('UPDATE conversations SET title = ? WHERE id = ?').run(trimmed.slice(0, 200), id);
  return getConversation(id);
}

export function setPinned(id, pinned) {
  getDb().prepare('UPDATE conversations SET pinned = ? WHERE id = ?').run(pinned ? 1 : 0, id);
  return getConversation(id);
}

export function setArchived(id, archived) {
  getDb().prepare('UPDATE conversations SET archived = ? WHERE id = ?').run(archived ? 1 : 0, id);
  return getConversation(id);
}

/** Deletes a conversation and (via ON DELETE CASCADE) all of its messages. */
export function deleteConversation(id) {
  getDb().prepare('DELETE FROM conversations WHERE id = ?').run(id);
}

const ACTIVE_KEY = 'active_conversation_id';

export function getActiveId() {
  const row = getDb().prepare('SELECT value FROM app_state WHERE key = ?').get(ACTIVE_KEY);
  return row?.value || null;
}

export function setActiveId(id) {
  getDb().prepare('INSERT INTO app_state (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value').run(
    ACTIVE_KEY,
    id
  );
}

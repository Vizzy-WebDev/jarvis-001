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
  return { id: `m${row.id}`, role: row.role, text: row.text ?? undefined, ...extra };
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
    // FTS5 MATCH needs its query text quoted to be treated as a literal
    // phrase/token search rather than parsed as FTS query syntax (a title
    // like "C++ setup?" would otherwise throw a syntax error).
    const ftsQuery = `"${q.replace(/"/g, '""')}"`;
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
      .all(`%${q}%`, ftsQuery);
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

// The single SQLite connection for both Chat History and Memory — the only
// module in the project that knows SQLite exists. Everything else goes
// through chat-store.js or memory/memory-store.js, never through this
// file's DatabaseSync handle directly.
//
// Node 24's built-in `node:sqlite` is used deliberately over a plain JSON
// file for BOTH stores: conversations are append-heavy and searched, which
// a JSON file handles by rewriting the whole thing on every message and
// linear-scanning every file to search; Memory needs real transactions for
// its approve/reject/conflict-resolution writes and is the one store a
// future self-improvement system will write into, where being able to
// audit it precisely matters more than JSON's hand-editability. SQLite
// gives real full-text search (FTS5) and atomic multi-row transactions for
// free, with zero install — confirmed live on this machine (FTS5 + WAL
// both work) before committing to this design. See server/memory/CLAUDE.md
// for Memory's own module breakdown.
//
// Leaf module: imports only node:sqlite and store.js's dataDir(). Nothing
// under server/skills/ may reach the loader/runner through this file (see
// root CLAUDE.md's circular-import invariant) — chat-store.js and
// memory/memory-store.js both sit on top of this the same way
// task-store.js sits on top of store.js.

import path from 'node:path';
import { DatabaseSync } from 'node:sqlite';
import { dataDir, readJson } from './store.js';

let db = null;

// Ordered migration steps, keyed by PRAGMA user_version. Each function runs
// once, in order, inside its own transaction — a later Stage 2 migration
// (Memory's tables) is simply appended here; nothing about this file needs
// to change shape when that lands.
const MIGRATIONS = [
  // 1: conversations + messages + full-text search + a small key/value table
  // for app-wide state (currently just the active conversation id).
  (conn) => {
    conn.exec(`
      CREATE TABLE conversations (
        id         TEXT PRIMARY KEY,
        title      TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        pinned     INTEGER NOT NULL DEFAULT 0,
        archived   INTEGER NOT NULL DEFAULT 0
      );

      CREATE TABLE messages (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
        seq             INTEGER NOT NULL,
        role            TEXT NOT NULL,
        text            TEXT,
        payload         TEXT,
        created_at      TEXT NOT NULL
      );
      CREATE INDEX idx_messages_conv ON messages(conversation_id, seq);

      CREATE VIRTUAL TABLE messages_fts USING fts5(
        text, content='messages', content_rowid='id'
      );

      CREATE TRIGGER messages_ai AFTER INSERT ON messages BEGIN
        INSERT INTO messages_fts(rowid, text) VALUES (new.id, new.text);
      END;
      CREATE TRIGGER messages_ad AFTER DELETE ON messages BEGIN
        INSERT INTO messages_fts(messages_fts, rowid, text) VALUES ('delete', old.id, old.text);
      END;
      CREATE TRIGGER messages_au AFTER UPDATE ON messages BEGIN
        INSERT INTO messages_fts(messages_fts, rowid, text) VALUES ('delete', old.id, old.text);
        INSERT INTO messages_fts(rowid, text) VALUES (new.id, new.text);
      END;

      CREATE TABLE app_state (
        key   TEXT PRIMARY KEY,
        value TEXT
      );
    `);
  },

  // 2: Memory (Stage 2) — a small, curated, approval-first fact store,
  // independent of conversations.js/chat-store.js by design (see root
  // CLAUDE.md's Memory section — deleting a conversation must never delete
  // an approved memory, and deleting a memory must never touch a
  // conversation). That independence is enforced by this schema, not by
  // remembering it: memory_candidates.conversation_id cascades on delete
  // (a draft dies with the conversation it came from); `memories` has NO
  // foreign key to conversations at all, so an approved memory structurally
  // cannot be cascaded away.
  (conn) => {
    conn.exec(`
      CREATE TABLE memories (
        id         TEXT PRIMARY KEY,
        category   TEXT NOT NULL,
        text       TEXT NOT NULL,
        source_kind TEXT,
        source_ref  TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        archived   INTEGER NOT NULL DEFAULT 0
      );

      -- One row per PAST state of a memory, written right before an edit
      -- overwrites it — so "why did this change?" has a real answer months
      -- later, not just the current text with the old version gone.
      CREATE TABLE memory_versions (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        memory_id  TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
        text       TEXT NOT NULL,
        category   TEXT NOT NULL,
        changed_at TEXT NOT NULL,
        reason     TEXT
      );
      CREATE INDEX idx_memory_versions_memory ON memory_versions(memory_id);

      -- A category's existence is itself approval-gated: a model proposing
      -- a new one inserts it here as 'pending' (rides in the same review
      -- batch as its candidates) — only the user turns it 'approved'.
      CREATE TABLE memory_categories (
        name   TEXT PRIMARY KEY,
        status TEXT NOT NULL DEFAULT 'approved'
      );

      -- A candidate is a DRAFT, not a memory — nothing here is consulted by
      -- the assistant until a human approves it into the memories table.
      CREATE TABLE memory_candidates (
        id              TEXT PRIMARY KEY,
        conversation_id TEXT REFERENCES conversations(id) ON DELETE CASCADE,
        source_kind     TEXT NOT NULL,
        source_ref      TEXT,
        category        TEXT NOT NULL,
        text            TEXT NOT NULL,
        conflict_with   TEXT REFERENCES memories(id) ON DELETE SET NULL,
        status          TEXT NOT NULL DEFAULT 'pending',
        created_at      TEXT NOT NULL,
        resolved_at     TEXT
      );
      CREATE INDEX idx_memory_candidates_status ON memory_candidates(status);

      -- How far into each conversation extraction has already looked, so a
      -- checkpoint only ever analyses NEW messages, never re-reads the whole
      -- transcript (and never re-proposes the same candidate twice).
      CREATE TABLE memory_checkpoints (
        conversation_id TEXT PRIMARY KEY REFERENCES conversations(id) ON DELETE CASCADE,
        last_seq        INTEGER NOT NULL DEFAULT 0,
        checked_at      TEXT
      );
    `);

    const seedCategories = ['Preferences', 'Projects', 'Work', 'Learning', 'People', 'Long-term Goals', 'About You', 'Uncategorized'];
    const insertCategory = conn.prepare('INSERT OR IGNORE INTO memory_categories (name, status) VALUES (?, ?)');
    for (const name of seedCategories) insertCategory.run(name, 'approved');

    // One-time migration of the old "About You" store (data/profile.json,
    // remember_about_me's previous home) into Memory. Tied to this
    // migration step rather than a startup check specifically so it runs
    // exactly once, ever — the same guarantee PRAGMA user_version already
    // gives every other step, with no separate "have we migrated yet?" flag
    // to maintain. Each entry becomes both a memory AND its own first
    // version row, so its history reads as "migrated" rather than starting
    // blank.
    const legacyProfile = readJson('profile', { entries: [] });
    if (legacyProfile?.entries?.length) {
      const insertMemory = conn.prepare(
        'INSERT INTO memories (id, category, text, source_kind, source_ref, created_at, updated_at, archived) VALUES (?, ?, ?, ?, ?, ?, ?, 0)'
      );
      const insertVersion = conn.prepare(
        'INSERT INTO memory_versions (memory_id, text, category, changed_at, reason) VALUES (?, ?, ?, ?, ?)'
      );
      for (const entry of legacyProfile.entries) {
        if (!entry?.text) continue;
        const id = `mem_legacy_${entry.id || Math.random().toString(36).slice(2)}`;
        const ts = entry.addedAt || new Date().toISOString();
        insertMemory.run(id, 'About You', entry.text, 'legacy', 'profile.json', ts, ts);
        insertVersion.run(id, entry.text, 'About You', ts, 'Migrated from the old About You notes.');
      }
    }
  },
];

function migrate(conn) {
  const currentVersion = conn.prepare('PRAGMA user_version').get().user_version;
  for (let v = currentVersion; v < MIGRATIONS.length; v++) {
    conn.exec('BEGIN');
    try {
      MIGRATIONS[v](conn);
      conn.exec(`PRAGMA user_version = ${v + 1}`);
      conn.exec('COMMIT');
    } catch (err) {
      conn.exec('ROLLBACK');
      throw err;
    }
  }
}

/** The shared DatabaseSync connection, opened (and migrated) on first use. */
export function getDb() {
  if (db) return db;
  const file = path.join(dataDir(), 'jarvis.db');
  db = new DatabaseSync(file);
  db.exec('PRAGMA journal_mode = WAL');
  db.exec('PRAGMA foreign_keys = ON');
  migrate(db);
  return db;
}

/**
 * Test-only escape hatch: closes and forgets the cached connection so a
 * fresh JARVIS_DATA_DIR takes effect on the next getDb() call. Production
 * code never calls this — the connection lives for the process lifetime.
 */
export function _resetForTests() {
  try {
    db?.close();
  } catch {
    // already closed / never opened — fine either way
  }
  db = null;
}

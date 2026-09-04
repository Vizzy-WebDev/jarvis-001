// Where Jarvis's own generated outputs land — real files under
// data/artifacts/, id-addressed, mirroring uploads.js's own proven design
// (the id IS the sanitized filename, collision-proof via its timestamp
// prefix, no in-memory index to keep in sync, resolvable across a
// restart). Metadata (mime type, size, verification result) lives in the
// `artifacts` table (db.js migration 19) — uploads.js needs none of that,
// but a generated artifact does, so this is genuinely a superset of that
// design, not identical to it.
//
// Leaf-adjacent: imports store.js's dataDir() (a leaf) and db.js (a leaf)
// — safe for server/tools/create_artifact.js to import directly.

import fs from 'node:fs';
import path from 'node:path';
import { dataDir } from '../store.js';
import { getDb } from '../db.js';

function artifactDir() {
  const dir = path.join(dataDir(), 'artifacts');
  if (!fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true });
  return dir;
}

/** Same sanitization discipline as uploads.js's safeName() — the name may come from a model's own tool-call argument, never trusted as a raw path component. */
function safeName(name) {
  const base = path.basename(String(name || 'artifact'));
  const cleaned = base.replace(/[^a-zA-Z0-9._-]/g, '_').replace(/^\.+/, '').slice(-120) || 'artifact';
  return `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}-${cleaned}`;
}

function rowToArtifact(row) {
  return {
    id: row.id,
    name: row.name,
    mimeType: row.mime_type,
    size: row.size,
    sessionId: row.session_id,
    createdAt: row.created_at,
    verified: row.verified === null ? null : Boolean(row.verified),
    verificationDetail: row.verification_detail,
  };
}

/**
 * Writes `content` (a Buffer, or a string for text formats) to
 * data/artifacts/<id> and records its metadata. Returns the full artifact
 * record. Throws on empty content, same as uploads.js's saveUpload() — a
 * zero-byte "artifact" would just confuse whatever opens it later.
 */
export function saveArtifact({ name, mimeType, content, sessionId = null }) {
  const buffer = Buffer.isBuffer(content) ? content : Buffer.from(String(content ?? ''), 'utf8');
  if (!buffer.length) throw new Error('An artifact needs real content — this would have been an empty file.');
  if (!mimeType) throw new Error('An artifact needs a mime type.');

  const id = safeName(name);
  const target = path.join(artifactDir(), id);
  fs.writeFileSync(target, buffer);

  const ts = new Date().toISOString();
  getDb()
    .prepare('INSERT INTO artifacts (id, name, mime_type, size, session_id, created_at) VALUES (?, ?, ?, ?, ?, ?)')
    .run(id, path.basename(String(name || 'artifact')), mimeType, buffer.length, sessionId, ts);

  return getArtifact(id);
}

/** Resolves an id back to its metadata row, or null — same untrusted-input re-validation discipline as uploads.js's getUpload(). */
export function getArtifact(id) {
  const clean = String(id || '');
  if (!clean || !/^[a-zA-Z0-9._-]+$/.test(clean) || clean.includes('..')) return null;
  const row = getDb().prepare('SELECT * FROM artifacts WHERE id = ?').get(clean);
  return row ? rowToArtifact(row) : null;
}

/** The real filesystem path for an id — null unless it resolves to a real, still-existing file INSIDE the artifacts folder (belt-and-braces path check, same as uploads.js/connectors/files.js). */
export function artifactFilePath(id) {
  const record = getArtifact(id);
  if (!record) return null;
  const full = path.join(artifactDir(), record.id);
  if (path.relative(artifactDir(), full).startsWith('..')) return null;
  if (!fs.existsSync(full)) return null;
  return full;
}

export function readArtifactContent(id) {
  const full = artifactFilePath(id);
  return full ? fs.readFileSync(full) : null;
}

export function listArtifacts({ sessionId, limit = 50 } = {}) {
  const rows = sessionId
    ? getDb().prepare('SELECT * FROM artifacts WHERE session_id = ? ORDER BY created_at DESC LIMIT ?').all(sessionId, limit)
    : getDb().prepare('SELECT * FROM artifacts ORDER BY created_at DESC LIMIT ?').all(limit);
  return rows.map(rowToArtifact);
}

/** Records a real verification outcome — see server/ops/verify.js. Never called with a guess; `verified` is null until this actually runs once. */
export function recordVerification(id, { verified, detail = null }) {
  getDb().prepare('UPDATE artifacts SET verified = ?, verification_detail = ? WHERE id = ?').run(verified ? 1 : 0, detail, id);
}

export function artifactsDirPath() {
  return artifactDir();
}

/** Removes an artifact's file and metadata row — used when a just-generated file fails its own mechanical verification (create_artifact.js never leaves a known-broken file behind pretending to be a real deliverable). */
export function deleteArtifact(id) {
  const full = artifactFilePath(id);
  if (full) {
    try {
      fs.unlinkSync(full);
    } catch {
      // Already gone — nothing to do.
    }
  }
  getDb().prepare('DELETE FROM artifacts WHERE id = ?').run(id);
}

// Where files attached in conversation land.
//
// Uploads arrive as a raw request body rather than a multipart form, which
// is why there's no multer or busboy here: this project runs on four
// dependencies and adding a parser for a single one-file-at-a-time upload
// isn't a fair trade. `express.raw()` hands over a Buffer; this writes it
// out and returns a path the rest of the pipeline treats exactly like a file
// the user typed the path to.
//
// Files are kept (not deleted straight after reading) so re-asking about a
// video doesn't mean re-uploading it, and pruned by age and count so the
// folder can't grow forever — the same approach control/screenshot-store.js
// takes for screenshots.
//
// A leaf module: imports nothing from server/ at all.

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
// Sits under the same overridable data directory as everything else, so a
// test run never writes into the user's real folder (see store.js).
const DATA_DIR = process.env.JARVIS_DATA_DIR || path.join(__dirname, '..', 'data');
const UPLOAD_DIR = path.join(DATA_DIR, 'uploads');

const MAX_FILES = 30;
const MAX_AGE_MS = 7 * 24 * 60 * 60 * 1000;

function ensureDir() {
  if (!fs.existsSync(UPLOAD_DIR)) fs.mkdirSync(UPLOAD_DIR, { recursive: true });
}

/**
 * Strips a client-supplied filename down to something safe to write.
 *
 * The name comes from the browser and is therefore untrusted: without this,
 * a name like `..\..\.env` would let an upload escape the uploads folder and
 * overwrite something important. Only the basename is kept, anything outside
 * a conservative character set is replaced, and a timestamp prefix makes
 * collisions impossible.
 */
function safeName(name) {
  const base = path.basename(String(name || 'upload'));
  const cleaned = base.replace(/[^a-zA-Z0-9._-]/g, '_').replace(/^\.+/, '').slice(-80) || 'upload';
  return `${Date.now().toString(36)}-${cleaned}`;
}

/** Deletes the oldest uploads once there are too many, or any that are simply old. Failures are ignored — tidying up must never break an upload. */
function prune() {
  try {
    const entries = fs
      .readdirSync(UPLOAD_DIR)
      .map((f) => {
        const full = path.join(UPLOAD_DIR, f);
        try {
          return { full, mtime: fs.statSync(full).mtimeMs };
        } catch {
          return null;
        }
      })
      .filter(Boolean)
      .sort((a, b) => b.mtime - a.mtime);

    const now = Date.now();
    entries.forEach((entry, index) => {
      if (index < MAX_FILES && now - entry.mtime < MAX_AGE_MS) return;
      try {
        fs.unlinkSync(entry.full);
      } catch {
        // Already gone, or in use — nothing to do.
      }
    });
  } catch {
    // No uploads folder yet.
  }
}

/**
 * Writes an uploaded body to disk and returns a record describing it.
 *
 * The `id` IS the stored filename — already sanitized by safeName() and
 * already collision-proof via its timestamp prefix. That means no in-memory
 * index to keep in sync, and an id stays resolvable across a server restart,
 * which matters because the browser holds ids between attaching a file and
 * sending the message.
 *
 * Throws if the body is empty, so an empty POST fails loudly rather than
 * producing a zero-byte file that later confuses whatever reads it.
 */
export function saveUpload(buffer, originalName) {
  if (!buffer || !buffer.length) throw new Error('That upload was empty.');
  ensureDir();
  const id = safeName(originalName);
  const target = path.join(UPLOAD_DIR, id);
  fs.writeFileSync(target, buffer);
  prune();
  return { id, path: target, name: path.basename(String(originalName || 'upload')), size: buffer.length };
}

/**
 * Resolves an id back to its file, or null.
 *
 * The id arrives from the browser and is therefore untrusted, exactly like
 * the filename safeName() guards. Re-validating the character set here means
 * an id such as `../../.env` can never be turned into a path outside the
 * uploads folder, no matter how it reached us.
 */
export function getUpload(id) {
  const clean = String(id || '');
  if (!clean || !/^[a-zA-Z0-9._-]+$/.test(clean) || clean.includes('..')) return null;

  const full = path.join(UPLOAD_DIR, clean);
  // Belt and braces: confirm the resolved path really is inside UPLOAD_DIR
  // before touching it (the same rule connectors/files.js applies).
  if (path.relative(UPLOAD_DIR, full).startsWith('..')) return null;
  if (!fs.existsSync(full)) return null;

  let size = 0;
  try {
    size = fs.statSync(full).size;
  } catch {
    return null;
  }
  // The timestamp prefix safeName() added isn't part of the user's filename.
  return { id: clean, path: full, name: clean.replace(/^[a-z0-9]+-/, ''), size };
}

export function uploadDir() {
  return UPLOAD_DIR;
}

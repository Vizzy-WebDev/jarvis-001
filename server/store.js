// Small helper for reading/writing the structured JSON files under data/
// (models, tasks, briefing config, profile, ...). Kept separate from
// config.js on purpose: config.js's .env format is a flat string map that
// silently drops falsy values (see its writeEnvFile) — fine for API keys,
// unusable for arrays/objects. This is the general-purpose version.
//
// Dependency-free, matching the rest of the project's style. Writes are
// atomic (temp file + rename) so a crash or power loss mid-write can never
// leave a half-written, corrupt JSON file behind.

import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
// Overridable so a test server can point at a completely separate,
// disposable data directory instead of the user's real one — the same
// reasoning as server.js's PORT override (see CLAUDE.md): never touch a
// copy the user already has open while testing changes.
const DATA_DIR = process.env.JARVIS_DATA_DIR || path.join(__dirname, '..', 'data');

function ensureDataDir() {
  if (!fs.existsSync(DATA_DIR)) {
    fs.mkdirSync(DATA_DIR, { recursive: true });
  }
}

function filePath(name) {
  return path.join(DATA_DIR, `${name}.json`);
}

// In-memory only, per-process, reset on restart — a THIS-PROCESS record of
// "I legitimately wrote this exact content just now," never persisted.
// This is what lets server/ops/diagnostics's config-integrity check tell
// apart a change this app itself made through a real route from one that
// happened some other way, without store.js needing to import anything at
// all (this file is deliberately dependency-free — see its own header
// comment). Records the WRITTEN CONTENT's own hash, not just a timestamp —
// a time-window heuristic was tried first and found genuinely wrong during
// this build's own verification: a legitimate write's timestamp stays
// "recent" long enough to wrongly excuse a LATER, unrelated external
// change that happens to land inside the same window. Comparing hashes
// instead means a legitimate write only ever explains the exact content it
// actually produced, no matter how much time has passed.
const lastWrites = new Map();

/** `{ts, hash}` (sha256 hex of the exact bytes) of THIS process's last writeJson() call for data/<name>.json — null if never, this process. */
export function lastWriteAt(name) {
  return lastWrites.get(name) || null;
}

/** Reads data/<name>.json, returning `fallback` if the file doesn't exist or is corrupt. */
export function readJson(name, fallback) {
  try {
    const raw = fs.readFileSync(filePath(name), 'utf8');
    return JSON.parse(raw);
  } catch {
    return fallback;
  }
}

/** Atomically writes `value` to data/<name>.json (temp file + rename). */
export function writeJson(name, value) {
  ensureDataDir();
  const target = filePath(name);
  const contents = JSON.stringify(value, null, 2);
  const tmp = `${target}.${process.pid}.${Date.now()}.tmp`;
  fs.writeFileSync(tmp, contents, 'utf8');
  fs.renameSync(tmp, target);
  lastWrites.set(name, { ts: Date.now(), hash: crypto.createHash('sha256').update(contents).digest('hex') });
}

/** True if data/<name>.json exists on disk. */
export function exists(name) {
  return fs.existsSync(filePath(name));
}

/**
 * The absolute path to data/<name>.json — for the rare caller that needs to
 * hand the path itself to something outside this module (e.g.
 * control/overlay-bridge.js passing the control-status file to the
 * PowerShell overlay process, which polls it directly rather than going
 * through readJson/writeJson). Ensures the data dir exists first, same as
 * writeJson, so a caller writing straight to this path doesn't need its own
 * mkdir logic.
 */
export function dataFilePath(name) {
  ensureDataDir();
  return filePath(name);
}

/**
 * The data directory itself (not a specific file inside it) — for a caller
 * that needs its own subdirectory there rather than a single JSON file (e.g.
 * connectors/browser.js's dedicated Chrome profile folder). Respects
 * JARVIS_DATA_DIR exactly like every other function here, so a caller using
 * this for test isolation gets it for free instead of hardcoding its own
 * path relative to its own source file (a real bug found the hard way — a
 * connector wrote a 145MB browser profile into the real data/ directory
 * during a scratch test run because it never went through this override).
 */
export function dataDir() {
  ensureDataDir();
  return DATA_DIR;
}

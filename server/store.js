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
  const tmp = `${target}.${process.pid}.${Date.now()}.tmp`;
  fs.writeFileSync(tmp, JSON.stringify(value, null, 2), 'utf8');
  fs.renameSync(tmp, target);
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

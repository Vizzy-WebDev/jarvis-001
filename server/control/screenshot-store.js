// Where screenshots taken during a control session live on disk, and how
// they expire. Per the user's explicit choice during design ("keep recent
// ones, auto-delete"): kept locally so a session can be reviewed afterward,
// pruned automatically so the folder never grows without bound. Retention
// numbers come from safety.js's `screenshotRetention` (default: last 50 /
// 24 hours) so they live alongside the rest of the safety configuration the
// user can already see and change.

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { getSafetyConfig } from './safety.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
// Sits under server/control/ rather than data/ — these are binary image
// files, not the small structured JSON store.js manages, and don't need
// JARVIS_DATA_DIR's test-isolation override (a screenshot folder filling up
// during a throwaway test run is harmless and gets deleted with the rest of
// the scratch directory).
const SHOTS_DIR = process.env.JARVIS_SCREENSHOTS_DIR || path.join(__dirname, '..', '..', 'data', 'screenshots');

function ensureDir() {
  if (!fs.existsSync(SHOTS_DIR)) fs.mkdirSync(SHOTS_DIR, { recursive: true });
}

function prune() {
  ensureDir();
  const { maxCount, maxAgeHours } = getSafetyConfig().screenshotRetention;
  const cutoff = Date.now() - maxAgeHours * 60 * 60 * 1000;
  const entries = fs
    .readdirSync(SHOTS_DIR)
    .filter((f) => f.endsWith('.png'))
    .map((f) => {
      const full = path.join(SHOTS_DIR, f);
      const stat = fs.statSync(full);
      return { file: f, full, mtime: stat.mtimeMs };
    })
    .sort((a, b) => b.mtime - a.mtime); // newest first

  entries.forEach((entry, i) => {
    if (i >= maxCount || entry.mtime < cutoff) {
      try {
        fs.unlinkSync(entry.full);
      } catch {
        // Already gone — fine, nothing to clean up.
      }
    }
  });
}

/** Saves one base64 PNG, pruning old ones per the current retention settings first. Returns the saved file's name (not full path — the browser fetches it through /api/control/screenshots/<name>). */
export function saveScreenshot(base64, { label } = {}) {
  ensureDir();
  prune();
  const stamp = new Date().toISOString().replace(/[:.]/g, '-');
  const safeLabel = (label || 'shot').replace(/[^a-zA-Z0-9_-]/g, '').slice(0, 40) || 'shot';
  const file = `${stamp}_${safeLabel}.png`;
  fs.writeFileSync(path.join(SHOTS_DIR, file), Buffer.from(base64, 'base64'));
  return file;
}

/** Every currently-kept screenshot, newest first — for the "recent screenshots" view. */
export function listScreenshots() {
  ensureDir();
  return fs
    .readdirSync(SHOTS_DIR)
    .filter((f) => f.endsWith('.png'))
    .map((f) => {
      const stat = fs.statSync(path.join(SHOTS_DIR, f));
      return { file: f, takenAt: stat.mtime.toISOString() };
    })
    .sort((a, b) => new Date(b.takenAt) - new Date(a.takenAt));
}

export function screenshotPath(file) {
  // Reject anything that isn't a plain filename we generated ourselves —
  // this is served over HTTP by filename (see server.js), so a path with
  // separators must never be allowed through to fs.readFileSync.
  if (!file || file.includes('/') || file.includes('\\') || file.includes('..')) return null;
  const full = path.join(SHOTS_DIR, file);
  return fs.existsSync(full) ? full : null;
}

/** Deletes every kept screenshot immediately — the "clear now" button. */
export function clearScreenshots() {
  ensureDir();
  for (const f of fs.readdirSync(SHOTS_DIR)) {
    if (!f.endsWith('.png')) continue;
    try {
      fs.unlinkSync(path.join(SHOTS_DIR, f));
    } catch {
      // Fine — goal is "gone", not "gone via this exact call".
    }
  }
}

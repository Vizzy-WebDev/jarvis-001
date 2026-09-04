// Where finished screen recordings live on disk, and how they expire — same
// shape as screenshot-store.js, just for .mp4 files and a much lower default
// retention count (safety.js's recordingRetention), since a video file is
// far larger than a screenshot.

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { getSafetyConfig } from './safety.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
// Same JARVIS_SCREENSHOTS_DIR-style override convention as screenshot-store.js
// — a scratch test run filling this up is harmless, cleaned up with the rest
// of the scratch directory.
const RECORDINGS_DIR = process.env.JARVIS_RECORDINGS_DIR || path.join(__dirname, '..', '..', 'data', 'recordings');

function ensureDir() {
  if (!fs.existsSync(RECORDINGS_DIR)) fs.mkdirSync(RECORDINGS_DIR, { recursive: true });
}

function prune() {
  ensureDir();
  const { maxCount, maxAgeHours } = getSafetyConfig().recordingRetention;
  const cutoff = Date.now() - maxAgeHours * 60 * 60 * 1000;
  const entries = fs
    .readdirSync(RECORDINGS_DIR)
    .filter((f) => f.endsWith('.mp4'))
    .map((f) => {
      const full = path.join(RECORDINGS_DIR, f);
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

/** Reserves a fresh, unique .mp4 path for a NEW recording to write to — called at start_screen_recording time, before ffmpeg is even spawned, so the recorder module never has to invent a filename itself. Prunes old recordings first so a long recording session never gets pruned mid-write by its own future siblings. */
export function reserveRecordingPath({ label } = {}) {
  ensureDir();
  prune();
  const stamp = new Date().toISOString().replace(/[:.]/g, '-');
  const safeLabel = (label || 'recording').replace(/[^a-zA-Z0-9_-]/g, '').slice(0, 40) || 'recording';
  const file = `${stamp}_${safeLabel}.mp4`;
  return { file, fullPath: path.join(RECORDINGS_DIR, file) };
}

/** Every currently-kept recording, newest first. */
export function listRecordings() {
  ensureDir();
  return fs
    .readdirSync(RECORDINGS_DIR)
    .filter((f) => f.endsWith('.mp4'))
    .map((f) => {
      const stat = fs.statSync(path.join(RECORDINGS_DIR, f));
      return { file: f, takenAt: stat.mtime.toISOString(), sizeBytes: stat.size };
    })
    .sort((a, b) => new Date(b.takenAt) - new Date(a.takenAt));
}

export function recordingPath(file) {
  // Same path-safety rule as screenshot-store.js's screenshotPath() — this
  // is served over HTTP by filename, so a path with separators must never
  // be allowed through to fs.
  if (!file || file.includes('/') || file.includes('\\') || file.includes('..')) return null;
  const full = path.join(RECORDINGS_DIR, file);
  return fs.existsSync(full) ? full : null;
}

/** Deletes every kept recording immediately. */
export function clearRecordings() {
  ensureDir();
  for (const f of fs.readdirSync(RECORDINGS_DIR)) {
    if (!f.endsWith('.mp4')) continue;
    try {
      fs.unlinkSync(path.join(RECORDINGS_DIR, f));
    } catch {
      // Fine — goal is "gone", not "gone via this exact call".
    }
  }
}

// Rolling system-load history (db.js migration 17) — a leaf module, same
// discipline as every other *-store.js in this project. sampler.js is the
// only writer; baseline.js is the only reader that matters for anomaly
// detection, but anything (check_environment.js included) may read this
// directly for a plain "what does load look like right now" answer.

import { getDb } from '../../db.js';

const RETENTION_MS = 7 * 24 * 60 * 60 * 1000;

function nowIso() {
  return new Date().toISOString();
}

function rowToSample(row) {
  return { id: row.id, ts: row.ts, cpuPct: row.cpu_pct, memFreePct: row.mem_free_pct, rssBytes: row.rss_bytes };
}

export function recordSample({ cpuPct = null, memFreePct = null, rssBytes = null }) {
  getDb()
    .prepare('INSERT INTO env_samples (ts, cpu_pct, mem_free_pct, rss_bytes) VALUES (?, ?, ?, ?)')
    .run(nowIso(), cpuPct, memFreePct, rssBytes);
}

export function listRecentSamples({ sinceIso, limit = 500 } = {}) {
  const rows = sinceIso
    ? getDb().prepare('SELECT * FROM env_samples WHERE ts >= ? ORDER BY ts ASC LIMIT ?').all(sinceIso, limit)
    : getDb().prepare('SELECT * FROM env_samples ORDER BY ts DESC LIMIT ?').all(limit);
  return rows.map(rowToSample);
}

export function latestSample() {
  const row = getDb().prepare('SELECT * FROM env_samples ORDER BY ts DESC LIMIT 1').get();
  return row ? rowToSample(row) : null;
}

/** Deletes anything older than the retention window — an operational health signal, never a billing log, so unbounded history was never the goal. */
export function pruneOld() {
  const cutoff = new Date(Date.now() - RETENTION_MS).toISOString();
  getDb().prepare('DELETE FROM env_samples WHERE ts < ?').run(cutoff);
}

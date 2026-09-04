// Real system-load sampling — CPU, memory, and Jarvis's own process memory.
// Deliberately does NOT use os.loadavg(): it returns [0, 0, 0] on Windows
// unconditionally (a documented Node/libuv limitation, not a bug this
// project could work around), which would make every reading on this
// project's own target platform silently useless. Real CPU usage instead
// comes from sampling os.cpus() TWICE and comparing the delta between idle
// and total time across the interval — a single os.cpus() snapshot has no
// usable percentage in it at all (it's a cumulative counter since boot).

import os from 'node:os';
import { recordSample, pruneOld } from './env-store.js';

const SAMPLE_INTERVAL_MS = 30 * 1000;
const PRUNE_EVERY_N_SAMPLES = 200; // roughly once per ~100 minutes at the interval above

function cpuTimesSnapshot() {
  return os.cpus().map((c) => ({ idle: c.times.idle, total: Object.values(c.times).reduce((a, b) => a + b, 0) }));
}

/** Real CPU busy percentage over the interval between `prev` and a fresh snapshot — null if the core count changed (a real, if rare, case: e.g. Windows power-plan core parking) rather than a misleading number. */
function cpuPercentSince(prev) {
  const now = cpuTimesSnapshot();
  if (!prev || prev.length !== now.length) return { pct: null, snapshot: now };
  let idleDelta = 0;
  let totalDelta = 0;
  for (let i = 0; i < now.length; i++) {
    idleDelta += now[i].idle - prev[i].idle;
    totalDelta += now[i].total - prev[i].total;
  }
  if (totalDelta <= 0) return { pct: null, snapshot: now };
  const pct = Math.max(0, Math.min(100, (1 - idleDelta / totalDelta) * 100));
  return { pct, snapshot: now };
}

let lastCpuSnapshot = null;
let sampleCount = 0;
let timer = null;

function takeSample() {
  const { pct: cpuPct, snapshot } = cpuPercentSince(lastCpuSnapshot);
  lastCpuSnapshot = snapshot;

  const freeBytes = os.freemem();
  const totalBytes = os.totalmem();
  const memFreePct = totalBytes > 0 ? (freeBytes / totalBytes) * 100 : null;

  const rssBytes = process.memoryUsage().rss;

  recordSample({ cpuPct, memFreePct, rssBytes });

  sampleCount++;
  if (sampleCount % PRUNE_EVERY_N_SAMPLES === 0) {
    try {
      pruneOld();
    } catch (err) {
      console.error('[ops] env_samples prune failed:', err);
    }
  }
}

/** Starts periodic sampling — called once from server.js. Idempotent. The very first sample has cpuPct:null (no prior snapshot to diff against yet) — genuinely unknown, not zero. */
export function startSampling() {
  if (timer) return;
  takeSample();
  timer = setInterval(takeSample, SAMPLE_INTERVAL_MS);
  timer.unref?.();
}

/** A fresh reading right now, bypassing the sample interval — check_environment.js's own "how loaded are things RIGHT NOW" path. Does not itself write a row (avoids double-counting against the regular cadence) unless `record:true` is passed. */
export function sampleNow({ record = false } = {}) {
  const { pct: cpuPct, snapshot } = cpuPercentSince(lastCpuSnapshot);
  if (record) lastCpuSnapshot = snapshot;
  const freeBytes = os.freemem();
  const totalBytes = os.totalmem();
  const memFreePct = totalBytes > 0 ? (freeBytes / totalBytes) * 100 : null;
  const rssBytes = process.memoryUsage().rss;
  const reading = { cpuPct, memFreePct, rssBytes, freeBytes, totalBytes };
  if (record) recordSample(reading);
  return reading;
}

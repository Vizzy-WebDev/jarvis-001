// "Unusually high, or climbing without a clear cause" — defined honestly,
// not asserted. A rolling median over real recent history (env_samples),
// plus a SUSTAINED-duration requirement so one genuine but brief spike
// never fires a finding — the same "don't fire on a single blip" discipline
// heartbeat/commitments-source.js's own notifiedApproaching/notifiedOverdue
// flags already established for a different kind of noisy signal.
//
// Leaf module: imports only env-store.js (itself a leaf) — safe for
// server/tools/check_environment.js.

import { listRecentSamples } from './env-store.js';

// A window long enough to establish a real baseline without reacting to
// this build's own startup transient (the very first sample has no CPU
// delta at all — see sampler.js).
const BASELINE_WINDOW_MS = 60 * 60 * 1000; // 1 hour
// How far above the baseline median counts as "unusually high" at all.
const CPU_ELEVATED_THRESHOLD_PCT = 1.5; // 1.5x the recent median
const MEM_LOW_FREE_THRESHOLD_PCT = 10; // below 10% free is elevated concern regardless of history
// A spike must hold for this long, continuously, before it's a finding —
// not a single 30s sample.
const SUSTAINED_DURATION_MS = 5 * 60 * 1000; // 5 minutes

function median(values) {
  if (!values.length) return null;
  const sorted = [...values].sort((a, b) => a - b);
  const mid = Math.floor(sorted.length / 2);
  return sorted.length % 2 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2;
}

/**
 * Checks whether CPU (or low free memory) has been sustained-elevated for
 * at least SUSTAINED_DURATION_MS, judged against this machine's OWN recent
 * median rather than a fixed number that would mean something different on
 * every machine this ever runs on. Returns null when nothing is unusual, or
 * a plain-language finding when it is. Never fires on fewer than a handful
 * of real samples — "unusually high" is meaningless without a real
 * baseline to compare against.
 */
export function checkForAnomaly() {
  const since = new Date(Date.now() - BASELINE_WINDOW_MS).toISOString();
  const samples = listRecentSamples({ sinceIso: since, limit: 1000 });
  const withCpu = samples.filter((s) => typeof s.cpuPct === 'number');
  if (withCpu.length < 5) return null; // not enough real history yet

  const cpuMedian = median(withCpu.map((s) => s.cpuPct));
  const cpuThreshold = cpuMedian * CPU_ELEVATED_THRESHOLD_PCT;

  // Walk backward from the most recent sample; a sustained window means
  // every sample within SUSTAINED_DURATION_MS of "now" was elevated, with
  // no gap that would mean it actually settled and rose again separately.
  const sortedDesc = [...withCpu].sort((a, b) => new Date(b.ts) - new Date(a.ts));
  const sustainedCutoff = Date.now() - SUSTAINED_DURATION_MS;
  const recentWindow = sortedDesc.filter((s) => new Date(s.ts).getTime() >= sustainedCutoff);
  const oldestInWindow = recentWindow[recentWindow.length - 1];
  const windowSpansFullDuration = oldestInWindow && new Date(oldestInWindow.ts).getTime() <= sustainedCutoff + 60_000; // real coverage, not a gap
  const allElevated = recentWindow.length > 0 && recentWindow.every((s) => s.cpuPct >= cpuThreshold);

  if (allElevated && windowSpansFullDuration && cpuThreshold > 5) {
    // cpuThreshold > 5 guards a near-idle machine (median ~1%, threshold
    // ~1.5%) from reporting trivial noise as "unusually high."
    return {
      summary: `CPU load has been sustained-elevated for the last ${Math.round(SUSTAINED_DURATION_MS / 60000)} minutes — currently around ${Math.round(recentWindow[0].cpuPct)}%, versus a recent typical median of ${Math.round(cpuMedian)}%.`,
      detail: JSON.stringify({ kind: 'cpu', current: recentWindow[0].cpuPct, median: cpuMedian, sustainedMs: SUSTAINED_DURATION_MS }),
    };
  }

  const latest = sortedDesc[0];
  if (latest && typeof latest.memFreePct === 'number' && latest.memFreePct < MEM_LOW_FREE_THRESHOLD_PCT) {
    const memRecentWindow = sortedDesc.filter((s) => new Date(s.ts).getTime() >= sustainedCutoff && typeof s.memFreePct === 'number');
    const memSustained = memRecentWindow.length > 0 && memRecentWindow.every((s) => s.memFreePct < MEM_LOW_FREE_THRESHOLD_PCT);
    if (memSustained) {
      return {
        summary: `Free memory has been below ${MEM_LOW_FREE_THRESHOLD_PCT}% for the last ${Math.round(SUSTAINED_DURATION_MS / 60000)} minutes — currently around ${Math.round(latest.memFreePct)}% free.`,
        detail: JSON.stringify({ kind: 'memory', current: latest.memFreePct, sustainedMs: SUSTAINED_DURATION_MS }),
      };
    }
  }

  return null;
}

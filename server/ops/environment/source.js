// Heartbeat source: "is system load unusually high or climbing without a
// clear cause" (root CLAUDE.md's Operational Awareness item 5). Leaf
// module — only baseline.js (a leaf) plus schedule-store.js for its own
// prior checkState, same shape as heartbeat/sources/jobs-source.js.
//
// **Own-checkState dedup, same discipline jobs-source.js already
// established and heartbeat/CLAUDE.md documents at length — required here
// for the identical reason.** A sustained-high-load condition can easily
// hold true across many ticks; without remembering "I already reported
// THIS ongoing situation," every tick would produce a fresh finding, a
// fresh spent decision.js model call, and a fresh notification for the
// exact same still-true condition. `check()` only returns a Finding when
// the reported KIND changes (nothing -> cpu, cpu -> memory, memory ->
// nothing, ...) — a genuinely new situation, never the same one repeated.

import { checkForAnomaly } from './baseline.js';
import { getItem as getScheduleItem } from '../../heartbeat/schedule-store.js';

export const SOURCE_ID = 'environment';
const CHECK_INTERVAL_MS = 5 * 60 * 1000; // cheap SQLite read only — no model call itself

/** One fixed item — system load isn't per-entity the way a job or a memory commitment is. */
export async function listItems() {
  return [{ itemKey: 'system-load', intervalMs: CHECK_INTERVAL_MS }];
}

export async function check(itemKey) {
  const anomaly = checkForAnomaly();
  const kind = anomaly ? JSON.parse(anomaly.detail).kind : null;

  const prior = getScheduleItem(SOURCE_ID, itemKey)?.checkState;
  if (!anomaly) {
    if (prior?.reportedKind) return { finding: null, checkState: null }; // it settled — clear the memory of the past report
    return { finding: null };
  }

  if (prior?.reportedKind === kind) {
    return { finding: null }; // the same still-ongoing condition — already reported
  }

  return {
    finding: { summary: anomaly.summary, detail: anomaly.detail },
    checkState: { reportedKind: kind, reportedAt: new Date().toISOString() },
  };
}

export const source = { id: SOURCE_ID, defaultIntervalMs: CHECK_INTERVAL_MS, listItems, check };

// Heartbeat source #1 (day one): background Job status. Leaf module — only
// jobs/job-store.js and heartbeat/outbox-store.js (both leaves), plus
// schedule-store.js for its own prior checkState.
//
// This does NOT duplicate Jobs' own completion/failure notifications —
// those already fire directly via addNotification() in worker.js/
// orchestrator.js the instant they happen. What this closes is the real gap
// the whole build exists for: a Tier 1 job outbox row (a confirm-gated
// action escalated, or "OK to start controlling your computer?") today sits
// completely silent — invisible to the user — until they happen to start a
// new conversation themselves, at which point prompt.js's jobsSection()
// finally surfaces it. This source turns "sitting silently in the outbox"
// into a real Heartbeat finding, eligible for the SAME urgency-decision step
// (server/heartbeat/decision.js) every other finding goes through — which is
// what can turn it into genuine proactive speech when the user is around.
//
// **Own-checkState dedup, found necessary by live testing, not assumed up
// front.** engine.js's routeFinding() dedups against an UNDELIVERED outbox
// row — real for a Tier 1/2 verdict, but a Tier 3 verdict never creates one
// at all, so a job that keeps sitting in the exact same "still waiting"
// state produced a fresh finding, a fresh (spent) decision.js model call,
// AND a fresh notification on every single tick forever — confirmed live,
// not hypothetical: the same unresolved job re-notified every ~3 minutes
// with no end. `check()` now remembers the specific underlying outbox
// entry's own id it last reported (via schedule-store.js's checkState, the
// same mechanism commitments-source.js already uses) and only returns a
// Finding again once that id actually changes — a genuinely NEW ask, not
// the same still-unanswered one.

import * as jobStore from '../../jobs/job-store.js';
import { getForJob } from '../outbox-store.js';
import { getItem as getScheduleItem } from '../schedule-store.js';

export const SOURCE_ID = 'jobs';
const CHECK_INTERVAL_MS = 3 * 60 * 1000; // cheap DB read only — no model call itself, safe to check often

/**
 * One item per job currently holding an undelivered Tier 1 outbox row.
 * itemKey is the JOB id (not the outbox row id) — a job can only ever hold
 * one live Tier 1 ask at a time in practice, and keying on the job keeps a
 * fresh ask for the same job from spawning a second, parallel schedule row.
 */
export async function listItems() {
  const active = jobStore.listJobs({ status: ['queued', 'planning', 'running', 'awaiting_decision', 'orphaned'] });
  const items = [];
  for (const job of active) {
    const pendingTier1 = getForJob(job.id).some((e) => e.tier === 1 && !e.deliveredAt);
    if (pendingTier1) items.push({ itemKey: job.id, intervalMs: CHECK_INTERVAL_MS });
  }
  return items;
}

/**
 * Re-reads the job's own outbox — if it's since been delivered (the user
 * already started a turn and saw it, or resolved it directly) there's
 * nothing left to surface. Otherwise returns a Finding, but ONLY the first
 * time this exact outbox entry is seen — see this file's own header
 * comment on why that check is needed at all.
 */
export async function check(itemKey) {
  const jobId = itemKey;
  const job = jobStore.getJob(jobId);
  if (!job) return { finding: null };
  const pending = getForJob(jobId).filter((e) => e.tier === 1 && !e.deliveredAt);
  if (!pending.length) return { finding: null, checkState: null }; // nothing outstanding any more — clear any stale memory of a past ask
  const entry = pending[0];

  const prior = getScheduleItem(SOURCE_ID, itemKey)?.checkState;
  if (prior?.lastReportedOutboxId === entry.id) {
    return { finding: null }; // the exact same still-unanswered ask — already reported, don't re-spend a decision call on it
  }

  return {
    finding: {
      summary: `Background work waiting on you: "${job.title}" — ${entry.summary}`,
      detail: JSON.stringify({ jobId, outboxId: entry.id }),
    },
    checkState: { lastReportedOutboxId: entry.id },
  };
}

export const source = { id: SOURCE_ID, defaultIntervalMs: CHECK_INTERVAL_MS, listItems, check };

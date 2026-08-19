// Tracks which models are temporarily misbehaving so the router and runner
// skip them automatically — and, just as important, stops skipping them
// once the cooldown expires, so a preferred model comes back on its own
// without the user having to do anything. In-memory only, same as the old
// per-provider conversation history: resets on server restart, which is
// fine — a fresh boot deserves a fresh chance.
//
// Cooldown length is tiered by *why* the model failed (see
// models/error-kind.js's classifyError) — a rate limit clears in minutes,
// a revoked/wrong key won't clear until the user fixes it, so there's no
// point hammering it every 5 minutes in the meantime.
//
// That automatic recovery is already correct on the functional side — the
// next call to isHealthy() just sees the model as healthy again. What used
// to be stale was the Model Settings screen: it only ever fetched a snapshot
// on render, so a recovered model kept showing "temporarily unavailable"
// until the user reloaded or clicked Test/Check all. broadcast() below (over
// the existing /api/events SSE bus, see server/events.js) closes that gap —
// pushed only on a genuine unhealthy<->healthy transition, not on every
// successful call, since markHealthy() already runs after every successful
// turn/job regardless of prior state.

import { broadcast } from '../events.js';

const unhealthy = new Map(); // modelId -> { until: timestamp, reason, kind }

const COOLDOWNS_MS = {
  network: 1 * 60 * 1000,
  quota: 30 * 60 * 1000,
  no_access: 6 * 60 * 60 * 1000,
  auth: 6 * 60 * 60 * 1000,
  other: 5 * 60 * 1000,
};

export function isHealthy(modelId) {
  const entry = unhealthy.get(modelId);
  if (!entry) return true;
  if (Date.now() >= entry.until) {
    unhealthy.delete(modelId);
    return true;
  }
  return false;
}

export function markUnhealthy(modelId, reason, kind = 'other') {
  const wasHealthy = !unhealthy.has(modelId);
  const cooldown = COOLDOWNS_MS[kind] ?? COOLDOWNS_MS.other;
  unhealthy.set(modelId, { until: Date.now() + cooldown, reason, kind });
  // Only a genuinely new cooldown is news — a failure that just refreshes an
  // already-benched model's cooldown isn't a state change worth a push.
  if (wasHealthy) broadcast({ type: 'model_health', modelId, state: 'unhealthy', reason, kind });
}

export function markHealthy(modelId) {
  const wasUnhealthy = unhealthy.has(modelId);
  unhealthy.delete(modelId);
  // markHealthy() runs after every successful turn/job regardless of prior
  // state — broadcast only the actual recoveries, not one event per healthy
  // turn.
  if (wasUnhealthy) broadcast({ type: 'model_health', modelId, state: 'healthy' });
}

/** For a status display: which models are currently benched and why. */
export function getHealthStatus() {
  const now = Date.now();
  const out = {};
  for (const [id, { until, reason, kind }] of unhealthy) {
    if (now < until) out[id] = { reason, retryInMs: until - now, kind };
    else unhealthy.delete(id);
  }
  return out;
}

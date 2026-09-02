// The Heartbeat tick — mirrors scheduler.js's/improvement/cycle.js's own
// shape. Reconciles every registered source's current item list against the
// persisted schedule, then processes whatever's due SEQUENTIALLY, one item
// after another, up to a per-tick cap. That sequential, capped processing
// is what turns a big catch-up (a long-closed period, many overdue items at
// once) into several ticks of steady work instead of one burst — and is
// what makes the `running` overlap guard meaningful in the first place.
//
// `routeFinding()` is the ONE place a finding becomes a notification, an
// outbox row, and (Tier 1, available, not quiet-hours-blocked) real
// proactive speech — both the tick below and triggers.js's event-driven
// path call this exact same function, so there is only ever one pipeline.

import * as scheduleStore from './schedule-store.js';
import { listSources } from './sources/registry.js';
import * as outboxStore from './outbox-store.js';
import { decideAttention } from './decision.js';
import { isQuietNow } from './quiet-hours.js';
import * as presence from './presence.js';
import { speakNow } from './speak.js';
import { addNotification } from '../notifications.js';

const TICK_MS = 60 * 1000;
const PER_TICK_CAP = 20;

let tickHandle = null;

function nowIso() {
  return new Date().toISOString();
}

/**
 * Reconciles one source's CURRENT item list into the persisted schedule —
 * new items get a row (checked soon), items no longer listed get pruned.
 * Wrapped by the caller in try/catch: one broken source's listItems() must
 * never stop another source from being reconciled this tick.
 */
async function reconcileSource(source) {
  const items = await source.listItems();
  for (const { itemKey, intervalMs } of items) {
    scheduleStore.upsertItem(source.id, itemKey, intervalMs ?? source.defaultIntervalMs);
  }
  scheduleStore.pruneRemoved(source.id, items.map((i) => i.itemKey));
}

/**
 * The one pipeline every finding — Heartbeat tick or Trigger event alike —
 * runs through. `sourceId`/`itemKey` together form the dedup key
 * (`outbox-store.js`'s sourceRef) so the same undelivered finding never
 * parks a second outbox row while the first is still waiting.
 */
export async function routeFinding(sourceId, itemKey, finding) {
  const sourceRef = `${sourceId}:${itemKey}`;
  if (outboxStore.getPendingForSourceRef('heartbeat', sourceRef).length) return; // already parked and undelivered — don't duplicate

  let verdict;
  try {
    verdict = await decideAttention(finding);
  } catch (err) {
    console.error('[heartbeat] the urgency-decision step itself failed — defaulting to a quiet record:', err);
    verdict = { tier: 3, reason: 'The urgency check failed, so this was only recorded.', emergency: false, emergencyReason: null };
  }

  // The durable record — always written, regardless of tier, before any
  // delivery attempt is even considered. This is what makes "never lose a
  // notice because I wasn't there" true structurally, not just usually.
  addNotification({
    kind: 'heartbeat',
    level: verdict.tier === 1 ? 'warning' : 'info',
    title: finding.summary,
    body: verdict.reason,
    action: { label: 'View', section: 'notifications' },
    meta: { sourceId, itemKey, tier: verdict.tier },
  });

  if (verdict.tier === 3) return;

  const entry = outboxStore.addEntry({
    source: 'heartbeat',
    sourceRef,
    tier: verdict.tier,
    reason: 'notice',
    summary: finding.summary,
    detail: finding.detail || null,
  });

  if (verdict.tier !== 1) return; // Tier 2: drains into the next turn like any other outbox row, never spoken live

  // Quiet hours gate LIVE SPEECH only — the notification and outbox row
  // above are unaffected either way (they take effect only once the user
  // is already engaging, at which point quiet hours has nothing left to
  // protect). An emergency verdict skips the busy dampener too, the same
  // way it already skips quiet hours itself.
  const quiet = isQuietNow();
  const canSpeakNow = quiet ? verdict.emergency && presence.isReachable() : await presence.isAvailable();
  if (canSpeakNow) {
    speakNow(finding.summary, { outboxId: entry.id, reason: verdict.reason });
  }
  // else: stays parked, drains via prompt.js's next-turn injection, or
  // acknowledge_notice.js once actually mentioned in a live turn.
}

async function processDueItem(item) {
  const source = listSources().find((s) => s.id === item.sourceId);
  scheduleStore.markRunning(item.id);
  let checkStateResult;
  try {
    if (!source) return; // the source that created this row is no longer registered — nothing to check, just let it advance/prune next reconcile
    const result = await source.check(item.itemKey);
    checkStateResult = result?.checkState;
    if (result?.finding) await routeFinding(source.id, item.itemKey, result.finding);
  } catch (err) {
    console.error(`[heartbeat] source "${item.sourceId}" check failed for item "${item.itemKey}":`, err);
  } finally {
    scheduleStore.markDone(item.id, checkStateResult !== undefined ? { checkState: checkStateResult } : undefined);
  }
}

async function tick() {
  for (const source of listSources()) {
    try {
      await reconcileSource(source);
    } catch (err) {
      console.error(`[heartbeat] source "${source.id}" failed to list its own items — skipped this tick:`, err);
    }
  }

  const due = scheduleStore.listDue(nowIso(), PER_TICK_CAP);
  for (const item of due) {
    await processDueItem(item); // sequential, on purpose — see this file's own header comment
  }
}

/** Runs one tick immediately, outside the setInterval schedule — exported for direct testing (see root CLAUDE.md's own pure-logic-module testing pattern) and for anything that wants a manual "check now" with no server restart. */
export { tick as runTickOnce };

/** Starts the tick. Call once from heartbeat/index.js. */
export function startTick() {
  if (tickHandle) return;
  tick().catch((err) => console.error('[heartbeat] initial tick failed:', err));
  tickHandle = setInterval(() => {
    tick().catch((err) => console.error('[heartbeat] tick failed:', err));
  }, TICK_MS);
}

/** Test-only escape hatch. Production code never calls this. */
export function _stopForTests() {
  if (tickHandle) clearInterval(tickHandle);
  tickHandle = null;
}

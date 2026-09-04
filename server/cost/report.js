// The read side of cost tracking — what check_spending.js (and, later,
// self-model.js's own dimension work, if ever extended to cover cost) asks
// to answer "how much have I spent," "which model do I use most." Every
// answer is built from real cost_events rows plus whatever real prices/
// balances are on record — nothing here estimates or interpolates.
//
// Three labelled sections, always kept separate, per the owner's own
// explicit requirement (see cost-store.js's header comment):
//   - measured           — real counted usage, always available.
//   - providerReported    — a real balance/usage figure straight from a
//                           provider's own API, only where one exists.
//   - calculated          — measured × a known price, only where a price is
//                           on record; null (never a guess) otherwise.

import { listEventsSince, listBalances } from './cost-store.js';
import { calculate } from './prices.js';

function startOfMonthIso(now = new Date()) {
  return new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), 1)).toISOString();
}

function startOfDayIso(now = new Date()) {
  return new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate())).toISOString();
}

/**
 * The full measured+calculated breakdown since `sinceIso` — one row per
 * (provider, modelId, unitKind) group. `usdTotal` sums only the groups that
 * actually had a known price; `pricelessGroups` lists which groups
 * contributed no dollar figure, so a caller (or the model relaying this to
 * the user) can say plainly "X of this has no known price" instead of
 * silently under-reporting.
 */
export function usageBreakdown(sinceIso) {
  const events = listEventsSince(sinceIso);
  const groups = new Map(); // key: provider|modelId|unitKind

  for (const ev of events) {
    const key = `${ev.provider}|${ev.modelId || ''}|${ev.unitKind}`;
    const g = groups.get(key) || {
      provider: ev.provider,
      modelId: ev.modelId,
      unitKind: ev.unitKind,
      unitsIn: 0,
      unitsOut: 0,
      cachedIn: 0,
      calls: 0,
    };
    g.unitsIn += ev.unitsIn || 0;
    g.unitsOut += ev.unitsOut || 0;
    g.cachedIn += ev.cachedIn || 0;
    g.calls += 1;
    groups.set(key, g);
  }

  const rows = [];
  let usdTotal = 0;
  let usdCurrency = null;
  const pricelessGroups = [];

  for (const g of groups.values()) {
    const calc = calculate({ provider: g.provider, modelId: g.modelId, unitKind: g.unitKind, unitsIn: g.unitsIn, unitsOut: g.unitsOut });
    if (calc) {
      usdTotal += calc.amount;
      usdCurrency = usdCurrency || calc.currency;
      rows.push({ ...g, calculated: { amount: calc.amount, currency: calc.currency, priceSource: calc.priceSource } });
    } else {
      pricelessGroups.push({ provider: g.provider, modelId: g.modelId, unitKind: g.unitKind });
      rows.push({ ...g, calculated: null });
    }
  }

  return {
    since: sinceIso,
    eventCount: events.length,
    measured: { groups: rows },
    calculated: rows.some((r) => r.calculated) ? { amount: usdTotal, currency: usdCurrency || 'USD' } : null,
    pricelessGroups,
  };
}

export function monthToDate(now = new Date()) {
  return usageBreakdown(startOfMonthIso(now));
}

export function today(now = new Date()) {
  return usageBreakdown(startOfDayIso(now));
}

/** Which model/provider has the most recorded turns since `sinceIso` — a real count, never inferred. */
export function mostUsed(sinceIso) {
  const { measured } = usageBreakdown(sinceIso);
  const sorted = [...measured.groups].sort((a, b) => b.calls - a.calls);
  return sorted[0] || null;
}

/** The last known real balance reading for every provider this build polls — see balances.js. Empty until refreshAllBalances() has run at least once. */
export function providerBalances() {
  return listBalances();
}

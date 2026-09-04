// Cost-at-decision-time (root CLAUDE.md's Operational Awareness item 6) —
// distinct from report.js's HISTORICAL spend tracking: this feeds a real,
// measured cost signal back into models/router.js's own ranking, which
// already weighs `entry.tier.cost` in every one of its scoring branches
// (background work is already the cost-heaviest, at -2x). What was missing
// wasn't the weighting — it's that `tier.cost` was always a 1-5 name-regex
// GUESS (catalog.js's guessFromName()), never a measured fact.
//
// Deliberately returns a value in the EXACT SAME 0-4 domain catalog.js's
// tier.cost already uses, rather than inventing a new scale or a raw dollar
// adjustment — router.js's scoreFor() was already built and tuned around a
// 0-4 tier.cost value multiplied by small fixed weights (-0.5x to -2x
// depending on branch), all comfortably inside the ±20
// AVAILABILITY_SCORE_BONUS spread router.js's own header comment documents
// as the thing nothing else may ever exceed. A same-domain replacement
// value can mathematically never break that bound — it was already proven
// safe for the exact range this returns.
//
// Returns null (never a guess of its own) when there isn't yet enough real
// data to correct the catalog's guess — router.js falls back to the
// catalog's own tier.cost in that case, exactly as it always has.

import { getPrice } from './cost-store.js';

// $ per 1K tokens (a blended, rough in/out average — this is a coarse
// bucket assignment, not a precise cost estimate; report.js's own
// usageBreakdown() is where a precise, real dollar figure lives) mapped
// onto catalog.js's existing 0-4 cost tier scale.
const BUCKETS = [
  { maxPer1k: 0.0005, cost: 0 },
  { maxPer1k: 0.002, cost: 1 },
  { maxPer1k: 0.01, cost: 2 },
  { maxPer1k: 0.03, cost: 3 },
  { maxPer1k: Infinity, cost: 4 },
];

function bucketFor(usdPer1k) {
  for (const b of BUCKETS) {
    if (usdPer1k <= b.maxPer1k) return b.cost;
  }
  return 4;
}

/**
 * A real-price-derived 0-4 cost tier for one (provider, modelId), or null
 * if no real price is on record yet — see this file's header comment for
 * why null (not a fallback number) is the honest thing to return here.
 */
export function observedCostTier(provider, modelId) {
  if (!provider || !modelId) return null;
  const price = getPrice(provider, modelId, 'tokens');
  if (!price) return null;
  const priceIn = typeof price.priceIn === 'number' ? price.priceIn : 0;
  const priceOut = typeof price.priceOut === 'number' ? price.priceOut : 0;
  // Both zero is a genuine free/local model, not "no data" — bucket 0 is
  // correct for it, not null.
  const blendedPer1k = ((priceIn + priceOut) / 2) * 1000;
  return bucketFor(blendedPer1k);
}

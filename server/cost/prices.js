// Price table — three sources, in write-time precedence order: a real,
// explicit user-set price always wins and is never overwritten by anything
// below it; a provider's own real reported numeric pricing (currently only
// OpenRouter's, via its public /v1/models pricing object — see
// adapters/openai-compatible.js's inferBillingFromPricing(), which parses
// this exact same data today and throws it away) comes next; a hand-
// maintained built-in guess is the last resort, and is seeded ONLY for the
// one case that's actually a certain fact rather than a guess — a local
// model genuinely costs nothing to run.
//
// Deliberately does NOT hardcode dollar figures for cloud models here. This
// project's model catalog (server/models/catalog.js) names models ahead of
// any publicly documented, verifiable pricing this build could honestly
// stand behind — inventing a plausible-looking number would violate the
// exact "never invented a number" requirement this whole subsystem exists
// to satisfy. A cloud model with no user-set price and no provider-reported
// price simply has none: report.js reports its usage only, and says so.
//
// Leaf-adjacent: imports db-backed cost-store.js (a leaf) and
// models/connections.js (a leaf — no path back to tools/index.js,
// capabilities.js, or runner.js) so this stays safe to import from
// server/tools/check_spending.js.

import * as costStore from './cost-store.js';
import { listConnections } from '../models/connections.js';
import { listModels } from '../models/registry.js';

/**
 * Seeds a $0 price for every currently-known local model — the one built-in
 * fact this file is willing to assert without a real external source. Never
 * overwrites an existing row (a user's own price, or a provider-reported
 * one, must never be silently downgraded back to a guess — even a correct
 * guess). Cheap and idempotent; safe to call on every server start.
 */
export function seedLocalModelPrices() {
  for (const m of listModels()) {
    const isLocal = m.kind === 'local' || m.billing === 'local';
    if (!isLocal) continue;
    const existing = costStore.getPrice(m.adapter || 'local', m.model, 'tokens');
    if (existing) continue; // never overwrite — see header comment
    costStore.setPrice({ provider: m.adapter || 'local', modelId: m.model, unitKind: 'tokens', priceIn: 0, priceOut: 0, currency: 'USD', source: 'built_in' });
  }
}

/**
 * An explicit, user-supplied price — always allowed, and always wins going
 * forward: refreshFromOpenRouter() below skips any row whose current
 * source is already 'user'.
 */
export function setUserPrice({ provider, modelId, unitKind = 'tokens', priceIn, priceOut, currency = 'USD' }) {
  costStore.setPrice({ provider, modelId, unitKind, priceIn, priceOut, currency, source: 'user' });
}

/**
 * Pulls OpenRouter's own real, public, numeric per-token pricing
 * (`GET /v1/models`, no key required — it's the same catalog page the
 * website itself reads) and records it for every model this connection
 * could plausibly route to. Skips a model whose existing price row is
 * already `source:'user'` — an explicit user price is never silently
 * replaced by a provider's own number. A network failure here is
 * swallowed, not thrown — this is a background refresh, not something a
 * caller should have to guard against failing.
 */
export async function refreshFromOpenRouter() {
  const hasOpenRouterConnection = listConnections().some((c) => c.provider === 'openrouter');
  if (!hasOpenRouterConnection) return { ok: true, updated: 0, reason: 'no OpenRouter connection configured' };

  let data;
  try {
    const res = await fetch('https://openrouter.ai/api/v1/models');
    if (!res.ok) return { ok: false, updated: 0, error: `OpenRouter's model catalog returned ${res.status}.` };
    data = await res.json();
  } catch (err) {
    return { ok: false, updated: 0, error: err?.message || 'Could not reach OpenRouter.' };
  }

  const models = Array.isArray(data?.data) ? data.data : [];
  let updated = 0;
  for (const m of models) {
    const pricing = m?.pricing;
    if (!pricing || typeof pricing !== 'object') continue;
    const priceIn = Number(pricing.prompt);
    const priceOut = Number(pricing.completion);
    if (!Number.isFinite(priceIn) && !Number.isFinite(priceOut)) continue;

    const existing = costStore.getPrice('openrouter', m.id, 'tokens');
    if (existing?.source === 'user') continue; // never overwrite an explicit user price

    // OpenRouter's pricing object is USD per single token, not per 1M —
    // stored exactly as given so calculate() below multiplies against a
    // raw token count with no unit-conversion guesswork of its own.
    costStore.setPrice({
      provider: 'openrouter',
      modelId: m.id,
      unitKind: 'tokens',
      priceIn: Number.isFinite(priceIn) ? priceIn : null,
      priceOut: Number.isFinite(priceOut) ? priceOut : null,
      currency: 'USD',
      source: 'provider_reported',
    });
    updated++;
  }
  return { ok: true, updated };
}

/**
 * Multiplies a real measured count against a known price — returns null
 * (never 0, never a guess) when no price is on record for this
 * (provider, modelId). This is the ONLY place a dollar figure is ever
 * calculated in this subsystem.
 */
export function calculate({ provider, modelId, unitKind = 'tokens', unitsIn = 0, unitsOut = 0 }) {
  const price = costStore.getPrice(provider, modelId, unitKind);
  if (!price) return null;
  const inCost = typeof price.priceIn === 'number' ? price.priceIn * (unitsIn || 0) : 0;
  const outCost = typeof price.priceOut === 'number' ? price.priceOut * (unitsOut || 0) : 0;
  return { amount: inCost + outCost, currency: price.currency, priceSource: price.source };
}

// A plain periodic timer, same posture as balances.js's own — no finding to
// judge, so no reason to route this through the Heartbeat.
const REFRESH_INTERVAL_MS = 24 * 60 * 60 * 1000;
let priceTimer = null;

/** Seeds local-model $0 prices once, then keeps OpenRouter's real numeric pricing fresh. Called once from server.js. Idempotent. */
export function startPriceMaintenance() {
  seedLocalModelPrices();
  if (priceTimer) return;
  refreshFromOpenRouter().catch((err) => console.error('[cost] initial OpenRouter price refresh failed:', err));
  priceTimer = setInterval(() => {
    refreshFromOpenRouter().catch((err) => console.error('[cost] periodic OpenRouter price refresh failed:', err));
  }, REFRESH_INTERVAL_MS);
  priceTimer.unref?.();
}

// Cost tracking's durable storage — three tables (db.js migration 16), one
// module, same discipline as jobs/job-store.js and heartbeat/outbox-store.js.
// Leaf module: imports only db.js — safe for server/tools/ (check_spending.js)
// and server/models/runner.js to import directly without tripping the
// loader/runner/scheduler circular-import invariant in root CLAUDE.md.
//
// Three separately-labelled kinds of number live here, per the owner's own
// explicit requirement, and this file never blends them:
//   - MEASURED  (cost_events)     — what we actually counted ourselves.
//   - REPORTED  (provider_balances) — what the provider's own API said.
//   - a price   (model_prices)    — used only to CALCULATE a dollar figure
//     from a measured count; a model with no row here has no calculated
//     figure, and report.js says so rather than inventing one.

import { getDb } from '../db.js';

function nowIso() {
  return new Date().toISOString();
}

function rowToEvent(row) {
  return {
    id: row.id,
    ts: row.ts,
    provider: row.provider,
    modelId: row.model_id,
    unitKind: row.unit_kind, // 'tokens' | 'characters' | 'seconds' | 'requests'
    unitsIn: row.units_in,
    unitsOut: row.units_out,
    cachedIn: row.cached_in,
    sessionId: row.session_id,
    background: Boolean(row.background),
  };
}

/**
 * Records one real unit of usage. Never throws on a missing optional field —
 * a TTS/STT call may have no unitsIn/unitsOut split, just a single count
 * (passed as unitsOut, "the thing produced"). `sessionId` is NOT a foreign
 * key (same discipline jobs.conversation_id already uses) — a cost event
 * must survive the conversation that generated it being deleted.
 */
export function recordEvent({ provider, modelId = null, unitKind, unitsIn = null, unitsOut = null, cachedIn = null, sessionId = null, background = false }) {
  if (!provider || !unitKind) throw new Error('A cost event needs a provider and a unitKind.');
  const db = getDb();
  db.prepare(
    `INSERT INTO cost_events (ts, provider, model_id, unit_kind, units_in, units_out, cached_in, session_id, background)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)`
  ).run(nowIso(), provider, modelId, unitKind, unitsIn, unitsOut, cachedIn, sessionId, background ? 1 : 0);
}

/** Every event since `sinceIso` (inclusive), oldest first — report.js's own raw material. */
export function listEventsSince(sinceIso) {
  const rows = getDb().prepare('SELECT * FROM cost_events WHERE ts >= ? ORDER BY ts ASC').all(sinceIso);
  return rows.map(rowToEvent);
}

/** Every event ever recorded for one (provider, modelId) — advisor.js's real-cost-per-turn derivation. `modelId` may be null to match non-model services. */
export function listEventsForModel(provider, modelId, { limit = 500 } = {}) {
  const rows = modelId
    ? getDb().prepare('SELECT * FROM cost_events WHERE provider = ? AND model_id = ? ORDER BY ts DESC LIMIT ?').all(provider, modelId, limit)
    : getDb().prepare('SELECT * FROM cost_events WHERE provider = ? AND model_id IS NULL ORDER BY ts DESC LIMIT ?').all(provider, limit);
  return rows.map(rowToEvent).reverse();
}

/** Overwrites this provider's last known real balance reading — one row per provider ref, no history kept (only the latest is ever meaningful). */
export function recordBalance(providerRef, detail) {
  if (!providerRef) throw new Error('A balance reading needs a providerRef.');
  const db = getDb();
  db.prepare(
    `INSERT INTO provider_balances (provider_ref, checked_at, detail_json)
     VALUES (?, ?, ?)
     ON CONFLICT(provider_ref) DO UPDATE SET checked_at = excluded.checked_at, detail_json = excluded.detail_json`
  ).run(providerRef, nowIso(), JSON.stringify(detail ?? {}));
}

export function getBalance(providerRef) {
  const row = getDb().prepare('SELECT * FROM provider_balances WHERE provider_ref = ?').get(providerRef);
  if (!row) return null;
  return { providerRef: row.provider_ref, checkedAt: row.checked_at, detail: JSON.parse(row.detail_json) };
}

export function listBalances() {
  const rows = getDb().prepare('SELECT * FROM provider_balances ORDER BY provider_ref ASC').all();
  return rows.map((row) => ({ providerRef: row.provider_ref, checkedAt: row.checked_at, detail: JSON.parse(row.detail_json) }));
}

/**
 * Sets a per-(provider, modelId, unitKind) price — `source` is
 * 'user' | 'provider_reported' | 'built_in', purely descriptive (never read
 * back for precedence — prices.js itself owns precedence ordering by
 * deciding WHICH source calls this for a given model).
 */
export function setPrice({ provider, modelId, unitKind, priceIn = null, priceOut = null, currency = 'USD', source = 'built_in' }) {
  if (!provider || !modelId || !unitKind) throw new Error('A price needs a provider, modelId, and unitKind.');
  const db = getDb();
  db.prepare(
    `INSERT INTO model_prices (provider, model_id, unit_kind, price_in, price_out, currency, source, updated_at)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?)
     ON CONFLICT(provider, model_id, unit_kind) DO UPDATE SET
       price_in = excluded.price_in, price_out = excluded.price_out, currency = excluded.currency,
       source = excluded.source, updated_at = excluded.updated_at`
  ).run(provider, modelId, unitKind, priceIn, priceOut, currency, source, nowIso());
}

export function getPrice(provider, modelId, unitKind) {
  const row = getDb().prepare('SELECT * FROM model_prices WHERE provider = ? AND model_id = ? AND unit_kind = ?').get(provider, modelId, unitKind);
  if (!row) return null;
  return { provider: row.provider, modelId: row.model_id, unitKind: row.unit_kind, priceIn: row.price_in, priceOut: row.price_out, currency: row.currency, source: row.source, updatedAt: row.updated_at };
}

export function listPrices() {
  const rows = getDb().prepare('SELECT * FROM model_prices ORDER BY provider ASC, model_id ASC').all();
  return rows.map((row) => ({ provider: row.provider, modelId: row.model_id, unitKind: row.unit_kind, priceIn: row.price_in, priceOut: row.price_out, currency: row.currency, source: row.source, updatedAt: row.updated_at }));
}

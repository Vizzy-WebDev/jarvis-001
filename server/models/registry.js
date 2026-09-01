// CRUD over the user's model list (data/models.json). A model entry no
// longer carries its own adapter/baseUrl/secretRef — those live on the
// connection it points at (server/models/connections.js), so several models
// discovered from one address+key share exactly one saved secret instead of
// each getting a duplicate copy. Secrets never live here at all — only a
// `connectionId`, and connections only ever hold a `secretRef` pointing
// into config.js's secret store.
//
// First-boot migration: if data/models.json doesn't exist yet, seed it from
// whatever the old single-provider .env setup already has, so an existing
// user's Gemini/Claude/OpenAI key keeps working with nothing to re-enter.
// A second, separate migration upgrades any old-shape model rows (from
// before connections existed) into the connection-referencing shape.

import { readJson, writeJson, exists } from '../store.js';
import { getConfiguredProviders, getActiveProvider, getSecret } from '../config.js';
import { getAdapter } from '../adapters/index.js';
import { getCatalogDefaults, inferBilling, withCapabilityDefaults } from './catalog.js';
import { friendlyMessage } from '../friendly-message.js';
import { redactSecrets } from './redact.js';
import { getProvider, providerForLegacy } from './providers.js';
import { probeEndpoint } from './probe.js';
import { getPrefs, setPrefs } from '../prefs.js';
import * as connections from './connections.js';

const {
  listConnections,
  getConnection,
  addConnection,
  updateConnection,
  removeConnection,
  defaultConnectionLabel,
} = connections;
export { listConnections, getConnection, addConnection, updateConnection, defaultConnectionLabel };

const FILE = 'models';

const LEGACY_PROVIDERS = {
  gemini: { adapter: 'gemini', model: 'gemini-3.5-flash', label: 'Gemini' },
  anthropic: { adapter: 'anthropic', model: 'claude-haiku-4-5', label: 'Claude' },
  openai: { adapter: 'openai-compatible', model: 'gpt-5.6-luna', label: 'OpenAI' },
};

function migrateFromLegacyEnv() {
  const configured = getConfiguredProviders();
  const active = getActiveProvider();

  const entries = configured.map((name) => {
    const legacy = LEGACY_PROVIDERS[name];
    const defaults = getCatalogDefaults(legacy.adapter, legacy.model);
    // One connection per legacy provider, reusing its provider name as both
    // the connection id AND its secretRef alias — getSecret('gemini') keeps
    // reading the exact same GEMINI_API_KEY as before (see config.js). The
    // id is forced to exactly `name` ('anthropic', not a label-derived
    // 'claude') so hydration can find it.
    connections.addConnection({ adapter: legacy.adapter, label: legacy.label, secretRef: name, id: name });
    return {
      id: name,
      label: legacy.label,
      model: legacy.model,
      connectionId: name,
      enabled: true,
      caps: defaults.caps,
      tier: defaults.tier,
      tags: defaults.tags || [],
      notes: '',
    };
  });

  writeJson(FILE, { entries });
  // A user who'd already picked a specific provider gets that as their
  // manual override, matching their existing choice rather than silently
  // switching them to "Auto" the first time they open the upgraded app.
  setPrefs({
    autoSelect: !configured.includes(active),
    manualModelId: configured.includes(active) ? active : null,
  });
  return { entries };
}

/** Upgrades any model rows saved before connections existed (adapter/baseUrl/secretRef inline, no connectionId) into the new shape. Idempotent — a no-op once every row has a connectionId. */
function migrateModelsToConnections(data) {
  const legacy = data.entries.filter((e) => !e.connectionId && e.adapter);
  if (!legacy.length) return data;

  const groups = new Map(); // "adapter|baseUrl|secretRef" -> connection id
  for (const entry of legacy) {
    const key = `${entry.adapter}|${entry.baseUrl || ''}|${entry.secretRef || ''}`;
    let connectionId = groups.get(key);
    if (!connectionId) {
      // Secret already lives under entry.secretRef on disk — pass it through
      // as the alias rather than minting (and duplicating) a fresh one.
      const conn = connections.addConnection({
        adapter: entry.adapter,
        baseUrl: entry.baseUrl,
        secretRef: entry.secretRef || undefined,
      });
      connectionId = conn.id;
      groups.set(key, connectionId);
    }
    entry.connectionId = connectionId;
    delete entry.adapter;
    delete entry.baseUrl;
    delete entry.secretRef;
  }

  writeJson(FILE, data);
  return data;
}

function load() {
  if (!exists(FILE)) return migrateFromLegacyEnv();
  return migrateModelsToConnections(readJson(FILE, { entries: [] }));
}

function save(data) {
  writeJson(FILE, data);
}

/**
 * Merges in the owning connection's adapter/baseUrl/secretRef/keyRequired,
 * plus its label for display. Falls back to any inline copy still on the
 * entry itself if the connection is missing — keeps a half-finished
 * migration harmless rather than producing an uncallable model.
 *
 * `kind` (`'first-party' | 'gateway' | 'local'`, from providers.js) is read
 * off the connection when saved there; a connection saved before the
 * provider catalog existed has none, so providerForLegacy() backfills it
 * at read time from the exact same adapter+host rule catalog.js's own
 * isLocalConnection()/isAggregatorConnection() used to apply inline — this
 * is what keeps every already-saved connection (the two OpenRouter ones,
 * the migrated Gemini one) classifying exactly as before, with no data
 * migration. `keyRequired` rides on the hydrated entry itself (not just
 * used here) because it's exactly what
 * adapters/openai-compatible.js's requireKeyIfNeeded() reads at call time.
 */
function hydrate(entry, byId) {
  const conn = entry.connectionId ? byId.get(entry.connectionId) : null;
  const adapter = conn?.adapter ?? entry.adapter ?? null;
  const baseUrl = conn?.baseUrl ?? entry.baseUrl ?? null;
  const kind = conn?.kind ?? (conn ? providerForLegacy(conn.adapter, conn.baseUrl).kind : null);
  return {
    ...entry,
    adapter,
    baseUrl,
    secretRef: conn?.secretRef ?? entry.secretRef ?? null,
    keyRequired: conn?.keyRequired ?? undefined,
    kind,
    connectionLabel: conn?.label ?? null,
    caps: withCapabilityDefaults(entry.caps, adapter, entry.model, baseUrl, kind),
  };
}

function connectionsById() {
  return new Map(listConnections().map((c) => [c.id, c]));
}

export function listModels() {
  const byId = connectionsById();
  return load().entries.map((e) => hydrate(e, byId));
}

export function getModel(id) {
  const entry = load().entries.find((e) => e.id === id);
  if (!entry) return null;
  return hydrate(entry, connectionsById());
}

function makeId(seed) {
  const base =
    String(seed || 'model')
      .toLowerCase()
      .trim()
      .replace(/[^a-z0-9]+/g, '-')
      .replace(/(^-|-$)/g, '') || 'model';
  const data = load();
  let id = base;
  let n = 2;
  while (data.entries.some((e) => e.id === id)) {
    id = `${base}-${n++}`;
  }
  return id;
}

// A model record's shape (informal — no schema library in this codebase):
//   { id, label, model, connectionId, enabled, caps, tier, tags, notes,
//     billing: 'free' | 'paid' | 'local' | 'unknown',
//     availability?: 'unknown' | 'ok' | 'unreachable' | ... }
// `billing` is set here (addModel/addModels/discoverModels, via
// catalog.js's inferBilling as the fallback whenever a real value wasn't
// supplied). `availability` is never written by this file — it's health.js/
// runner.js's field, tracking whether a model has recently worked — but it
// must stay patchable via MODEL_PATCH_KEYS below so that work isn't blocked
// on a change here.

/** Adds one model under an already-existing connection. */
export function addModel({ connectionId, model, label, caps, tier, tags, notes, enabled = true, billing }) {
  if (!connectionId || !model) throw new Error('A model needs a connection and a model name.');
  const conn = getConnection(connectionId);
  if (!conn) throw new Error(`Unknown connection: ${connectionId}`);

  const data = load();
  // Uniqueness is (connectionId, model), never model alone — the same model
  // name legitimately exists twice under two different connections (e.g.
  // two separate API keys for the same provider). This also self-protects
  // against the same model appearing twice within one addModels() batch,
  // since each call in that loop re-loads from disk after the previous
  // save().
  if (data.entries.some((e) => e.connectionId === connectionId && e.model === model)) {
    throw new Error(`"${model}" is already added under this connection.`);
  }
  const id = makeId(label || model);
  const kind = conn.kind ?? providerForLegacy(conn.adapter, conn.baseUrl).kind;
  const defaults = getCatalogDefaults(conn.adapter, model, conn.baseUrl, kind);
  const resolvedBilling = billing != null ? billing : inferBilling(conn.adapter, conn.baseUrl, model, kind);

  const entry = {
    id,
    label: label || model,
    model,
    connectionId,
    enabled,
    caps: { ...defaults.caps, ...(caps || {}) },
    tier: { ...defaults.tier, ...(tier || {}) },
    tags: tags?.length ? tags : defaults.tags,
    notes: notes || '',
    billing: resolvedBilling,
  };

  data.entries.push(entry);
  save(data);
  return hydrate(entry, connectionsById());
}

/** Adds several models under one connection in one call. `models` is a list of names or {model,label?,contextTokens?,billing?} objects (this is exactly the shape discoverModels() now returns). Never throws for an individual failure — collects it instead, so one bad name doesn't lose the rest of the batch. */
export function addModels(connectionId, models) {
  const added = [];
  const failed = [];
  for (const m of models || []) {
    const spec = typeof m === 'string' ? { model: m } : m;
    try {
      const caps = spec.contextTokens != null ? { contextTokens: spec.contextTokens } : undefined;
      added.push(
        addModel({ connectionId, model: spec.model, label: spec.label, billing: spec.billing, caps })
      );
    } catch (err) {
      failed.push({ model: spec.model, error: err?.message || 'Could not add this model.' });
    }
  }
  return { added, failed };
}

// Only these fields can be changed after a model is created — never
// adapter/baseUrl/secretRef/connectionId, and never secret/clearSecret
// (those belong to updateConnection now). Without this whitelist, PATCHing
// a model with a hand-crafted body could permanently desync it from its
// connection — see the plan's note on this exact risk.
const MODEL_PATCH_KEYS = ['label', 'model', 'enabled', 'caps', 'tier', 'tags', 'notes', 'billing', 'availability'];

export function updateModel(id, patch = {}) {
  const data = load();
  const idx = data.entries.findIndex((e) => e.id === id);
  if (idx === -1) throw new Error(`Unknown model: ${id}`);
  const entry = { ...data.entries[idx] };

  for (const key of MODEL_PATCH_KEYS) {
    if (key in patch) entry[key] = patch[key];
  }

  data.entries[idx] = entry;
  save(data);
  return hydrate(entry, connectionsById());
}

export function deleteModel(id) {
  const data = load();
  const existed = data.entries.some((e) => e.id === id);
  if (!existed) return;
  data.entries = data.entries.filter((e) => e.id !== id);
  save(data);

  // If this was the pinned manual model, fall back to auto rather than
  // leaving the user silently pointed at a model that no longer exists.
  const prefs = getPrefs();
  if (prefs.manualModelId === id) {
    setPrefs({ manualModelId: null, autoSelect: true });
  }
}

/** Deletes a connection and every model under it in one action, so removing a key can't leave orphaned, uncallable model rows behind. */
export function deleteConnection(id) {
  const removedModelIds = load()
    .entries.filter((e) => e.connectionId === id)
    .map((e) => e.id);
  for (const modelId of removedModelIds) deleteModel(modelId);
  removeConnection(id);
  return { removedModelIds };
}

/**
 * True if this (hydrated) entry has everything it needs to actually be
 * called right now.
 *
 * `entry.keyRequired` — the stored fact from the provider catalog / probe
 * (server/models/providers.js, probe.js) — is checked first and, when it's
 * a real boolean, decides this outright: `false` means ready regardless of
 * a saved secret, `true` means a saved secret is required, full stop. This
 * is what closes the OmniRoute-class gap where any non-openai.com baseUrl
 * used to be assumed keyless and reported "ready" even when the server
 * actually rejected the request for lacking one. Only when `keyRequired`
 * is undefined (any connection saved before this field existed) does this
 * fall back to the original host-regex guess, unchanged — so nothing
 * already saved changes behavior without being re-saved.
 */
export function isReady(entry) {
  if (typeof entry.keyRequired === 'boolean') {
    if (!entry.keyRequired) return true;
    return Boolean(entry.secretRef && getSecret(entry.secretRef));
  }
  // Local/self-hosted servers (Ollama, LM Studio, ...) typically need no
  // key at all — the adapter itself decides whether a given host requires
  // one; here we only need a quick, good-enough signal for filtering.
  if (entry.adapter === 'openai-compatible' && entry.baseUrl && !/openai\.com/i.test(entry.baseUrl)) {
    return true;
  }
  return Boolean(entry.secretRef && getSecret(entry.secretRef));
}

/**
 * Tests a not-yet-saved (or already-saved) connection+model combo before
 * committing to it. On failure, `detail` carries the adapter's own raw
 * error text (redacted) alongside the friendly `error` sentence — surfaced
 * by the UI as a collapsible "Technical details" line, since "That
 * connection didn't work" was previously the ENTIRE signal a user had to
 * diagnose an OmniRoute-class failure with.
 *
 * `keyRequired`, when the caller already knows it (server.js's per-model
 * "Test" button passes a fully hydrated model entry, which carries it),
 * is threaded straight into the adapter's own entry — otherwise a saved
 * connection's own stored fact would be silently dropped on THIS path
 * only, even though registry.js's isReady()/hydrate() already honor it.
 */
export async function testModelConnection({ adapter, model, baseUrl, secret, secretRef, keyRequired }) {
  const mod = getAdapter(adapter);
  const entry = {
    adapter,
    model,
    baseUrl,
    secretRef,
    secretValue: secret !== undefined ? secret : undefined,
    keyRequired: typeof keyRequired === 'boolean' ? keyRequired : undefined,
  };
  const result = await mod.testConnection(entry);
  if (result.ok || !result.error) return result;
  console.error(`[models] ${adapter} test failed (shown to user as a plain-language message): ${result.error}`);
  // `result.friendly` (set by an adapter that already produced a specific,
  // final sentence — e.g. openai-compatible.js's "server was reached but
  // needs an API key" case) is passed through verbatim instead of being
  // re-run through friendlyMessage(). Without this, that reclassification
  // step finds no matching error-kind pattern for text like "needs an API
  // key" (only "invalid key" text matches its auth pattern) and silently
  // overwrites an already-correct message with the generic fallback —
  // confirmed live against a stub gateway before this branch existed.
  if (result.friendly) return { ok: false, error: result.error };
  // Otherwise this was the adapter's own friendlyError() output, verbatim —
  // the provider's own wording (e.g. Gemini's "API key not valid. Please
  // pass a valid API key."), not plain language. Logged, never shown raw.
  return { ...result, error: friendlyMessage(result.error, "That connection didn't work."), detail: redactSecrets(result.error, [secret]) };
}

/**
 * Normalizes whatever an adapter's listModels() handed back into a
 * consistent `{model, label, contextTokens, billing}` shape: a plain string
 * (backward compat, in case some adapter still returns string[]) becomes a
 * full record; entries with no truthy `.model` are dropped; a missing/null
 * `.billing` is filled in via catalog.js's inferBilling (all three of our
 * adapters currently rely on this for at least some models — Gemini and
 * Anthropic always, openai-compatible only when the host has no pricing
 * field at all, e.g. a local Ollama server).
 */
function normalizeDiscovered(models, { adapter, baseUrl, kind }) {
  const out = [];
  for (const m of models || []) {
    const entry = typeof m === 'string' ? { model: m, label: m, contextTokens: null, billing: null } : { ...m };
    if (!entry.model) continue;
    if (entry.label == null) entry.label = entry.model;
    if (entry.contextTokens === undefined) entry.contextTokens = null;
    if (entry.billing == null) entry.billing = inferBilling(adapter, baseUrl, entry.model, kind);
    out.push(entry);
  }
  return out;
}

/**
 * Asks a local/self-hosted server (or a cloud provider's key) what models
 * it has available. Always resolves — `{models, error}` distinguishes
 * "reached it, found nothing" from "couldn't reach it at all", instead of
 * an empty list either way. `connectionId` lets a caller re-run discovery
 * against an already-saved connection without re-typing its key — when
 * given, models already added under that SAME connection are filtered out
 * of the result (addModel()'s (connectionId, model) uniqueness rule, so a
 * model already added never shows up again to be picked and re-added).
 * Scoped strictly to `connectionId`, never to `model` name alone: the same
 * model under a DIFFERENT connection is unaffected and still appears.
 */
export async function discoverModels({ adapter, baseUrl, secret, secretRef, connectionId }) {
  let resolved = { adapter, baseUrl, secretRef, kind: null };
  if (connectionId) {
    const conn = getConnection(connectionId);
    if (conn) {
      resolved = {
        adapter: conn.adapter,
        baseUrl: conn.baseUrl,
        secretRef: conn.secretRef,
        kind: conn.kind ?? providerForLegacy(conn.adapter, conn.baseUrl).kind,
      };
    }
  }
  const mod = getAdapter(resolved.adapter);
  const entry = { ...resolved, secretValue: secret !== undefined ? secret : undefined };
  try {
    const models = await mod.listModels(entry);
    let items = normalizeDiscovered(models, resolved);
    if (connectionId) {
      const already = new Set(listModels().filter((e) => e.connectionId === connectionId).map((e) => e.model));
      items = items.filter((item) => !already.has(item.model));
    }
    return { models: items, error: null };
  } catch (err) {
    const friendly = typeof mod.friendlyError === 'function' ? mod.friendlyError(err) : err?.message;
    console.error(`[models] ${resolved.adapter} discovery failed (shown to user as a plain-language message): ${friendly}`);
    // Classified from the RAW err (more accurate than the adapter's own
    // already-processed string) — friendlyMessage() accepts either.
    return {
      models: [],
      error: friendlyMessage(err, 'Could not discover models at that address.'),
      detail: redactSecrets(typeof friendly === 'string' ? friendly : err?.message, [secret]),
    };
  }
}

/**
 * The primary "Test and add" entry point: resolves which provider row this
 * is, tests the connection ONCE (using the first selected model — the
 * address+key is what's actually being validated, not each individual
 * model name), then, only on success, saves the connection and every
 * selected model. Writes nothing at all on failure — no orphan connection,
 * no orphan secret.
 *
 * `provider` (a providers.js id, e.g. 'openai'/'local'/'custom') is the
 * user-facing selection now — `adapter`/`baseUrl` are still accepted
 * directly (a raw adapter name) so nothing already calling this breaks
 * mid-refactor, but a real `provider` id takes precedence when given.
 *
 * `provider:'custom'` has no fixed adapter/baseUrl/kind/keyRequired of its
 * own — probeEndpoint() (server/models/probe.js) resolves all four by
 * actually trying the address, and its `steps[]` (what it tried, in plain
 * language) rides along on both success and failure so the UI can show its
 * working instead of one generic sentence. This is the fix for the
 * OmniRoute-class failure: previously nothing here even attempted more
 * than one shape at one URL.
 *
 * `resolved` (optional `{adapter, kind, keyRequired}`) lets a caller that
 * ALREADY ran a successful probe (the UI's own "Find models at this
 * address" step, for `provider:'custom'`) hand the result straight through
 * instead of this function probing all over again from scratch. Confirmed
 * live against a real 115-model OmniRoute gateway: discovery's probe
 * succeeded, and a SECOND, independent probe run moments later at submit
 * time — a second full round-trip to the user's own LAN server for
 * information already in hand — failed on its own, rejecting a connection
 * that was actually fine. `resolved` is trusted only when the caller
 * supplies it; omitting it re-probes exactly as before (still the correct
 * fallback for any caller that never ran its own probe).
 */
export async function createConnectionWithModels({ provider, adapter, baseUrl, label, secret, models, resolved }) {
  const modelList = (models || []).filter(Boolean);
  if (!modelList.length) throw new Error('Pick at least one model to add.');

  let resolvedAdapter = adapter;
  let resolvedBaseUrl = baseUrl;
  let kind = null;
  let keyRequired = null;
  let steps;
  // True once real connectivity is already proven for THIS request — either
  // by an already-supplied `resolved` (the caller's own prior probe) or by
  // the probe run below (which validates by actually calling listModels(),
  // not by guessing). Skips the separate chat-completion test further down,
  // which otherwise validates against just ONE arbitrary model out of
  // however many were selected — a poor proxy for "does the connection
  // work" on a multi-provider aggregator, where the models LISTING can
  // succeed while one specific routed model's own upstream key/quota (on
  // the gateway's side, not the user's) fails. Confirmed live: a real,
  // working OmniRoute connection was rejected this way over exactly one
  // bad route among 115 good ones.
  let connectivityProven = false;

  if (provider) {
    const row = getProvider(provider);
    if (!row) throw new Error(`Unknown provider: ${provider}`);
    if (provider === 'custom') {
      if (resolved?.adapter && resolved?.baseUrl && typeof resolved.keyRequired === 'boolean') {
        // `resolved.baseUrl` — not the raw `baseUrl` param — since the
        // probe's own normalization (auto-appending `/v1`, etc.) is part
        // of what it resolved; using the un-normalized address here would
        // silently undo that.
        resolvedAdapter = resolved.adapter;
        resolvedBaseUrl = resolved.baseUrl;
        kind = resolved.kind ?? null;
        keyRequired = resolved.keyRequired;
        connectivityProven = true;
      } else {
        const probe = await probeEndpoint({ baseUrl, secret });
        steps = probe.steps;
        if (!probe.ok) {
          return { ok: false, error: probe.error, detail: redactSecrets((probe.steps || []).join(' '), [secret]), steps };
        }
        resolvedAdapter = probe.adapter;
        resolvedBaseUrl = probe.baseUrl;
        kind = probe.kind;
        keyRequired = probe.keyRequired;
        connectivityProven = true;
      }
    } else {
      resolvedAdapter = row.adapter;
      resolvedBaseUrl = row.urlEditable ? baseUrl || row.baseUrl : row.baseUrl;
      kind = row.kind;
      keyRequired = row.keyRequired;
    }
  }

  if (!connectivityProven) {
    const first = typeof modelList[0] === 'string' ? modelList[0] : modelList[0].model;
    const test = await testModelConnection({ adapter: resolvedAdapter, model: first, baseUrl: resolvedBaseUrl, secret, keyRequired });
    if (!test.ok) return { ok: false, error: test.error, detail: test.detail, steps };
  }

  const conn = addConnection({ adapter: resolvedAdapter, baseUrl: resolvedBaseUrl, label, secret, provider, kind, keyRequired });
  const { added, failed } = addModels(conn.id, modelList);
  return { ok: true, connection: conn, added, failed, steps };
}

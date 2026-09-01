// Known-model defaults, so adding a familiar model auto-fills sensible
// speed/quality/cost/context/tool-support metadata instead of leaving the
// user to guess every field by hand. Everything stays editable afterward in
// the Model Settings screen — this is only a starting point. A model we've
// never heard of (any local model, mostly) gets a name-based guess instead
// of a blank slate.

import { KEY_REQUIRED_HOSTS } from '../adapters/openai-compatible.js';
import { getCapabilities } from '../adapters/index.js';

const KNOWN = {
  'gemini-3.5-flash': {
    label: 'Gemini 3.5 Flash',
    caps: { tools: true, streaming: true, contextTokens: 1000000, vision: true },
    tier: { speed: 5, quality: 3, cost: 1 },
    tags: ['fast', 'cheap'],
  },
  'gemini-3.6-flash': {
    label: 'Gemini 3.6 Flash',
    caps: { tools: true, streaming: true, contextTokens: 1000000, vision: true },
    tier: { speed: 5, quality: 3, cost: 1 },
    tags: ['fast', 'cheap'],
  },
  'gemini-3-pro': {
    label: 'Gemini 3 Pro',
    caps: { tools: true, streaming: true, contextTokens: 1000000, vision: true },
    tier: { speed: 3, quality: 5, cost: 3 },
    tags: ['reasoning'],
  },
  'claude-haiku-4-5': {
    label: 'Claude Haiku 4.5',
    caps: { tools: true, streaming: true, contextTokens: 200000, vision: true },
    tier: { speed: 5, quality: 3, cost: 1 },
    tags: ['fast', 'cheap'],
  },
  'claude-sonnet-5': {
    label: 'Claude Sonnet 5',
    caps: { tools: true, streaming: true, contextTokens: 200000, vision: true },
    tier: { speed: 3, quality: 4, cost: 2 },
    tags: ['balanced'],
  },
  'claude-opus-5': {
    label: 'Claude Opus 5',
    caps: { tools: true, streaming: true, contextTokens: 200000, vision: true },
    tier: { speed: 2, quality: 5, cost: 4 },
    tags: ['reasoning', 'writing'],
  },
  'gpt-5.6-luna': {
    label: 'GPT-5.6 Luna',
    caps: { tools: true, streaming: true, contextTokens: 200000, vision: true },
    tier: { speed: 4, quality: 4, cost: 2 },
    tags: ['balanced'],
  },
  'llama3.1': {
    label: 'Llama 3.1',
    caps: { tools: true, streaming: true, contextTokens: 128000, vision: false },
    tier: { speed: 3, quality: 3, cost: 0 },
    tags: ['local', 'free'],
  },
  mistral: {
    label: 'Mistral',
    caps: { tools: true, streaming: true, contextTokens: 32000, vision: false },
    tier: { speed: 4, quality: 3, cost: 0 },
    tags: ['local', 'free'],
  },
};

const FAST_HINTS = /flash|haiku|mini|nano|lite|small|luna/i;
const QUALITY_HINTS = /opus|ultra|pro\b|large|max|reasoning/i;
// Local model names that are known to actually understand images — most
// Ollama-style local models are text-only unless they're one of these
// vision-tuned variants.
const VISION_HINTS = /vision|vl\b|llava|multimodal|pixtral|moondream/i;

/**
 * True only for a genuinely local/self-hosted openai-compatible server
 * (Ollama, LM Studio, ...) — an adapter of 'openai-compatible' alone is NOT
 * enough, since that's also what OpenAI itself, OpenRouter, Groq, and
 * Together all go through by pointing baseUrl elsewhere.
 *
 * `kind` — the stored fact from the provider catalog / probe
 * (server/models/providers.js, probe.js) — decides this outright when
 * present ('local' or not), since it was captured once from an actual
 * probe result rather than re-guessed from the URL every time. Only when
 * `kind` is null/undefined (a connection saved before the provider catalog
 * existed) does this fall back to the original host-regex guess, the exact
 * same rule the adapter itself uses to decide whether a key is required —
 * kept in sync with KEY_REQUIRED_HOSTS on purpose so the two never drift
 * apart for a legacy connection.
 */
function isLocalConnection(adapter, baseUrl, kind) {
  if (kind) return kind === 'local';
  return adapter === 'openai-compatible' && Boolean(baseUrl) && !KEY_REQUIRED_HOSTS.test(baseUrl);
}

// A multi-vendor aggregator: one key, one baseUrl, a catalogue of other
// people's models. Unlike a first-party host (OpenAI), what's behind it is a
// grab-bag — `google/gemma-4-26b-a4b-it:free` and `nvidia/nemotron-3-ultra`
// sit in the same list as genuinely multimodal models, and nothing in the
// wire format distinguishes them.
const AGGREGATOR_HOSTS = /openrouter\.ai|groq\.com|together\.(ai|xyz)/i;

// `kind` overrides the host-regex guess the same way isLocalConnection()
// does, for the same reason — see that function's comment.
function isAggregatorConnection(adapter, baseUrl, kind) {
  if (kind) return kind === 'gateway';
  return adapter === 'openai-compatible' && Boolean(baseUrl) && AGGREGATOR_HOSTS.test(baseUrl);
}

/**
 * Whether to assume an unrecognized model can see images.
 *
 * Optimistic for a first-party cloud host (most frontier models are
 * multimodal now). Pessimistic — name hints only — for a local server AND for
 * an aggregator, because both serve mostly text-only models under one
 * connection. Measured cost of getting this wrong: with 17 OpenRouter models
 * all claiming vision, `pickModel({need:{vision:true}})` ranked a music model
 * above Gemini and every image attachment failed before reaching a provider.
 */
function assumesVision(adapter, model, baseUrl, kind) {
  if (VISION_HINTS.test(model || '')) return true;
  return !isLocalConnection(adapter, baseUrl, kind) && !isAggregatorConnection(adapter, baseUrl, kind);
}

function isOpenAIHost(baseUrl) {
  return !baseUrl || /openai\.com/i.test(baseUrl);
}

function guessFromName(adapter, model, baseUrl, kind) {
  const isLocal = isLocalConnection(adapter, baseUrl, kind);
  const fast = FAST_HINTS.test(model);
  const strong = QUALITY_HINTS.test(model);
  // See assumesVision() — router.js's `control` profile and ai.js's
  // meetsNeed() both filter on this, so an over-claimed flag doesn't just
  // waste an upload, it hides the model that could actually have done it.
  const vision = assumesVision(adapter, model, baseUrl, kind);

  return {
    label: model,
    caps: { tools: true, streaming: true, contextTokens: isLocal ? 32000 : 128000, vision },
    tier: {
      speed: fast ? 5 : strong ? 2 : 3,
      quality: strong ? 5 : fast ? 2 : 3,
      cost: isLocal ? 0 : strong ? 3 : fast ? 1 : 2,
    },
    tags: isLocal ? ['local'] : [],
  };
}

/**
 * Best-effort defaults for a model: known models get curated values,
 * anything else gets a name-based guess. `baseUrl` only matters for adapter
 * 'openai-compatible' — it's what tells a local server (free) apart from a
 * cloud one (paid) sharing the same adapter. `kind`, when the caller has
 * it (registry.js's hydrate()/addModel() always do, from the connection),
 * overrides the baseUrl-regex guess with the stored fact instead — see
 * isLocalConnection()'s comment.
 */
export function getCatalogDefaults(adapter, model, baseUrl, kind) {
  return KNOWN[model] || guessFromName(adapter, model, baseUrl, kind);
}

/**
 * Fills in capability flags a saved model record doesn't have yet.
 *
 * `vision` has been stored on models since computer control shipped, but
 * `video`/`audio`/`webSearch` arrived later with Content Analysis — so every
 * model already in the user's data/models.json predates them. Rather than
 * rewriting everyone's file with a migration, registry.js calls this from
 * hydrate() and the flags simply appear at read time. A value the user has
 * explicitly set in Model Settings always wins; only genuinely-absent flags
 * get filled.
 *
 * The value filled in is the adapter's ceiling: if the wire format can carry
 * video at all, a model under it is assumed to be able to until told
 * otherwise (the user can untick it, and a job that fails will mark the
 * model unhealthy on its own). Guessing optimistically here is the right way
 * round — guessing `false` would hide a capable model from Content Analysis
 * with no visible reason why.
 */
export function withCapabilityDefaults(caps, adapter, model, baseUrl, kind) {
  const ceiling = getCapabilities(adapter);
  const out = { ...(caps || {}) };
  for (const key of ['video', 'audio', 'vision', 'webSearch']) {
    if (out[key] === undefined) out[key] = Boolean(ceiling[key]);
  }
  // Local and aggregator-hosted models are the cases worth overriding: the
  // name is the only signal available, and handing a video to Ollama's llama3
  // — or an image to `google/gemma-4-26b-a4b-it:free` — wastes a long upload
  // to get a confused answer, or fails outright. See assumesVision().
  //
  // This runs at read time (registry.js's hydrate calls it), so models saved
  // before this rule existed pick it up with no data migration.
  if (!assumesVision(adapter, model, baseUrl, kind)) {
    if (caps?.video === undefined) out.video = false;
    if (caps?.vision === undefined) out.vision = false;
  }
  return out;
}

// Coarse billing rules for models we recognize by name but that expose no
// pricing field of their own (Gemini, Anthropic — see inferBilling below).
// `requireHost`, when present, must also pass before the rule applies (used
// so the openai-compatible 'gpt-' rule only fires against the real OpenAI
// host, not some other OpenAI-shaped server that happens to reuse gpt-*
// names).
export const BILLING = {
  gemini: [
    { pattern: /flash/i, billing: 'free' },
    { pattern: /pro/i, billing: 'paid' },
  ],
  anthropic: [{ pattern: /^claude-/i, billing: 'paid' }],
  'openai-compatible': [{ pattern: /^gpt-/i, billing: 'paid', requireHost: isOpenAIHost }],
};

/**
 * Best-effort billing tier for a model with no pricing data of its own
 * (Gemini and Anthropic's APIs expose none at all; a local Ollama/LM Studio
 * model has none to report either). Pure function, no I/O — 'local' beats
 * everything else since a self-hosted server is free by construction
 * regardless of what its model is named.
 */
export function inferBilling(adapter, baseUrl, model, kind) {
  if (!model) return 'unknown';
  if (isLocalConnection(adapter, baseUrl, kind)) return 'local';
  if (model.endsWith(':free')) return 'free';

  const rules = BILLING[adapter] || [];
  for (const rule of rules) {
    if (rule.requireHost && !rule.requireHost(baseUrl)) continue;
    if (rule.pattern.test(model)) return rule.billing;
  }
  return 'unknown';
}

// Per-provider suggestion lists now live on each row in
// server/models/providers.js (PROVIDERS[i].suggestions) — the user picks a
// provider by name now, not an adapter, so the curated list has to key off
// the same thing the UI shows. This used to live here, keyed by adapter.

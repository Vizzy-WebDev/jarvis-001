// Works out how to talk to a Custom endpoint the user just typed an address
// for — what OmniRoute-class connections needed and never had. Shows its
// working (`steps[]`) rather than silently guessing, per the design
// decision in the Provider System Refactor plan: a Custom connection is
// probed, not auto-detected in the dark.
//
// Bounded cascade, at most ~4 cheap /models-style requests, never a real
// generation call (see CLAUDE.md's free-tier-quota constraint — every model
// call is usually rate-limited, so discovery-shaped requests only).

import * as openaiCompatible from '../adapters/openai-compatible.js';
import * as anthropic from '../adapters/anthropic.js';
import * as gemini from '../adapters/gemini.js';
import { classifyError } from './error-kind.js';
import { inferBilling } from './catalog.js';

// Mirrors registry.js's normalizeDiscovered() billing-fill step — an
// adapter's listModels() sometimes already knows billing (openai-compatible
// reads it off a host's own pricing field when present), sometimes never
// does (Anthropic/Gemini expose no pricing field at all) — either way, a
// still-null billing is filled in from the name/host heuristic so the
// checklist the UI renders never shows "unknown" when a better guess exists.
function fillBilling(models, adapter, baseUrl, kind) {
  return (models || []).map((m) => ({ ...m, billing: m.billing ?? inferBilling(adapter, baseUrl, m.model, kind) }));
}

const LOCAL_HOST = /^(localhost|127\.0\.0\.1|::1|0\.0\.0\.0)$/i;
// RFC 1918 private ranges plus .local mDNS names — a self-hosted gateway on
// a home LAN address should still classify as 'local', not 'gateway'.
const PRIVATE_HOST = /^(10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.)|\.local$/i;

function isLocalHost(host) {
  return LOCAL_HOST.test(host) || PRIVATE_HOST.test(host);
}

/** Strips a trailing slash and a trailing /chat/completions the user may have pasted in by copying an example curl command rather than the bare base URL. */
function normalizeBaseUrl(raw) {
  let url = String(raw || '').trim();
  url = url.replace(/\/+$/, '');
  url = url.replace(/\/chat\/completions$/i, '');
  return url;
}

function candidateUrls(base) {
  if (/\/v1$/i.test(base) || /\/api\/v1$/i.test(base)) return [base];
  return [base, `${base}/v1`];
}

function hostOf(baseUrl) {
  try {
    return new URL(baseUrl).hostname;
  } catch {
    return '';
  }
}

/**
 * Probes a not-yet-saved Custom address and works out adapter/baseUrl/kind/
 * keyRequired for it, narrating each attempt into `steps[]` so a failure is
 * explainable instead of one generic sentence. Always resolves — never
 * throws — same "always resolves" discipline as registry.js's
 * discoverModels().
 */
export async function probeEndpoint({ baseUrl, secret }) {
  const steps = [];
  const base = normalizeBaseUrl(baseUrl);
  if (!base) {
    return { ok: false, error: 'Please enter a server address.', steps };
  }

  let lastAuthCandidate = null;

  // ---- OpenAI chat-completions shape (covers the overwhelming majority of
  // gateways and self-hosted servers: OmniRoute, LiteLLM, vLLM, Ollama, LM
  // Studio, text-generation-webui, ...) ----
  for (const candidate of candidateUrls(base)) {
    steps.push(`Trying ${candidate}/models …`);
    try {
      // listModels() never calls requireKeyIfNeeded() itself — it just
      // builds a client with whatever key is available (or the SDK's
      // placeholder) and lets the server's own response say whether that
      // was enough, which is exactly the signal this probe wants.
      const entry = { baseUrl: candidate, secretValue: secret !== undefined ? secret : undefined };
      const rawModels = await openaiCompatible.listModels(entry);
      steps.push(`Found an OpenAI-style server — ${rawModels.length} model(s) available.`);
      const kind = isLocalHost(hostOf(candidate)) ? 'local' : 'gateway';
      return {
        ok: true,
        adapter: 'openai-compatible',
        baseUrl: candidate,
        kind,
        // A successful request with NO key proves the server is genuinely
        // keyless. A successful request WITH one doesn't prove the key was
        // actually needed — but re-testing without it just to find out is
        // an extra request for no real benefit, so a supplied key that
        // worked is kept as the safer assumption going forward.
        keyRequired: Boolean(secret),
        models: fillBilling(rawModels, 'openai-compatible', candidate, kind),
        steps,
      };
    } catch (err) {
      const kind = classifyError(err);
      if (kind === 'auth' || kind === 'no_access') {
        lastAuthCandidate = candidate;
        steps.push(secret ? 'Reached it, but that key was rejected.' : 'Reached it, but it needs an API key.');
        // An auth rejection means this candidate URL IS the right shape —
        // stop trying other shapes, but keep going only if a later /v1
        // candidate hasn't been tried yet (a bare host can 401 while its
        // /v1 sibling would too; no value in trying non-OpenAI shapes next).
        continue;
      }
      steps.push('Nothing there.');
    }
  }

  if (lastAuthCandidate) {
    return {
      ok: false,
      adapter: 'openai-compatible',
      baseUrl: lastAuthCandidate,
      kind: isLocalHost(hostOf(lastAuthCandidate)) ? 'local' : 'gateway',
      keyRequired: true,
      models: [],
      steps,
      error: secret ? 'That server was reached but the key was rejected.' : 'That server was reached but needs an API key.',
    };
  }

  // ---- Anthropic Messages-API shape ---- (both this and the Gemini shape
  // below require SOME key to even attempt — client() throws NO_API_KEY
  // before any network call otherwise, which would misreport as "nothing
  // there" rather than the real reason no request was even sent).
  if (!secret) {
    steps.push('Not an Anthropic- or Gemini-style server (no key was given to try one).');
    return { ok: false, error: "Couldn't find a working address there — double-check it's running and reachable, or add a key if this server needs one.", steps };
  }
  steps.push(`Trying ${base} as an Anthropic-style server …`);
  try {
    const entry = { baseUrl: base, secretValue: secret !== undefined ? secret : undefined };
    const rawModels = await anthropic.listModels(entry);
    steps.push(`Found an Anthropic-style server — ${rawModels.length} model(s) available.`);
    const kind = isLocalHost(hostOf(base)) ? 'local' : 'gateway';
    return { ok: true, adapter: 'anthropic', baseUrl: base, kind, keyRequired: true, models: fillBilling(rawModels, 'anthropic', base, kind), steps };
  } catch (err) {
    const kind = classifyError(err);
    if (kind === 'auth' || kind === 'no_access') {
      steps.push(secret ? 'Reached it, but that key was rejected.' : 'Reached it, but it needs an API key.');
      return {
        ok: false,
        adapter: 'anthropic',
        baseUrl: base,
        kind: isLocalHost(hostOf(base)) ? 'local' : 'gateway',
        keyRequired: true,
        models: [],
        steps,
        error: secret ? 'That server was reached but the key was rejected.' : 'That server was reached but needs an API key.',
      };
    }
    steps.push('Nothing there.');
  }

  // ---- Gemini shape ----
  steps.push(`Trying ${base} as a Gemini-style server …`);
  try {
    const entry = { baseUrl: base, secretValue: secret !== undefined ? secret : undefined };
    const rawModels = await gemini.listModels(entry);
    steps.push(`Found a Gemini-style server — ${rawModels.length} model(s) available.`);
    const kind = isLocalHost(hostOf(base)) ? 'local' : 'gateway';
    return { ok: true, adapter: 'gemini', baseUrl: base, kind, keyRequired: true, models: fillBilling(rawModels, 'gemini', base, kind), steps };
  } catch (err) {
    const kind = classifyError(err);
    if (kind === 'auth' || kind === 'no_access') {
      steps.push(secret ? 'Reached it, but that key was rejected.' : 'Reached it, but it needs an API key.');
      return {
        ok: false,
        adapter: 'gemini',
        baseUrl: base,
        kind: isLocalHost(hostOf(base)) ? 'local' : 'gateway',
        keyRequired: true,
        models: [],
        steps,
        error: secret ? 'That server was reached but the key was rejected.' : 'That server was reached but needs an API key.',
      };
    }
    steps.push('Nothing there.');
  }

  return { ok: false, error: "Couldn't find a working address there — double-check it's running and reachable.", steps };
}

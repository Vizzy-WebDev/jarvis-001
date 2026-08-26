// Provider-agnostic seam for server-side text-to-speech — one registry, one
// contract, so a new paid provider (OpenAI TTS, ...) is one new file here
// plus a key, never a change to anything that calls this.
//
// Every provider exports the same shape:
//   isConfigured() -> boolean            (has whatever key/setup it needs)
//   stream(text, {voice}) -> async generator yielding {buffer, mimeType}
//   matchesRef(ref) -> boolean           (does this configured external-service ref belong to me?)
//   testKey(key) -> Promise<{ok, error?}>  (live "does this key work" check, no synthesis cost)
// `stream()` is an async generator on every provider, even a non-streaming
// one (which yields exactly one chunk). This is deliberate: a genuinely
// streaming provider added later needs no change to any caller — the
// consumer (server.js's /api/tts route) already iterates chunks either way.
//
// Providers are matched to CONFIGURED SERVICES (server/external-services.js
// — the generic, user-named key store) via matchesRef(), not by a fixed id
// this file knows in advance — this file never hardcodes a provider's name
// or ref anywhere; it only asks each registered adapter module "is this
// yours?" and defers entirely to its answer. See tts/elevenlabs.js's header
// comment for why an exact-string ref match isn't good enough on its own
// (the user can type any label) and why that recognition logic belongs
// inside each provider's own adapter, not here.
//
// "browser" (the free, offline speechSynthesis voice) is deliberately NOT a
// provider here — it has no server component at all (the browser talks to
// nothing, same as browser STT in stt/index.js), so it's handled entirely
// client-side (public/browser-speaker.js), exactly as it already was before
// this seam existed.

import * as elevenlabs from './elevenlabs.js';
import { getPrefs } from '../prefs.js';
import * as externalServices from '../external-services.js';

// One entry per real, working adapter — extend this (and nothing else) when
// a new provider's real backend integration is actually built.
const ADAPTERS = [elevenlabs];

/** Every configured external service that a real adapter recognizes — for the voice-output picker. Browser speech is listed separately (client-only, see browser-speaker.js). */
export function listProviders() {
  const services = externalServices.listServices();
  return ADAPTERS.map((adapter) => {
    const service = services.find((s) => adapter.matchesRef(s.ref));
    if (!service) return null;
    return { id: service.ref, label: service.label, configured: service.configured };
  }).filter(Boolean);
}

function resolveAdapter(id) {
  if (id) return ADAPTERS.find((a) => a.matchesRef(id)) || null;
  const pref = getPrefs().ttsProvider;
  if (pref) return ADAPTERS.find((a) => a.matchesRef(pref)) || null;
  return null;
}

/** True if the given (or the user's default) provider has what it needs to actually run. */
export function isConfigured(id) {
  const adapter = resolveAdapter(id);
  return adapter ? adapter.isConfigured() : false;
}

/**
 * Synthesizes speech for `text`, yielding {buffer, mimeType} chunks in
 * order. `opts.provider` overrides the user's saved default for this one
 * call; `opts.voice` is passed straight through to the provider (each
 * provider's own voice-name space — no cross-provider mapping attempted).
 */
export async function* stream(text, { provider, voice } = {}) {
  const adapter = resolveAdapter(provider);
  if (!adapter) {
    const err = new Error('No TTS provider is configured.');
    err.code = 'NO_API_KEY';
    throw err;
  }
  yield* adapter.stream(text, { voice });
}

/** One live "test this key" function per adapter, dispatched by ref via matchesRef() — used by server.js's generic /api/external-services/:ref/test route. Returns null if no adapter recognizes this ref (that route already handles "no live test available" for that case). */
export function testerFor(ref) {
  const adapter = ADAPTERS.find((a) => a.matchesRef(ref));
  return adapter ? adapter.testKey : null;
}

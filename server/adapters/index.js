// Resolves a model registry entry's `adapter` field to an actual module.
// Only three exist, because that's how many distinct wire formats there
// are — 'openai-compatible' alone covers OpenAI, Ollama, LM Studio,
// OpenRouter, Groq and anything else OpenAI-shaped (see its file for why).
// Every adapter implements the same shape:
//   stream(entry, messages, opts) -> AsyncGenerator of
//     {type:'chunk', text} | {type:'call', calls, raw?} | {type:'final', text, raw?}
//   testConnection(entry) -> Promise<{ok, error?}>
//   listModels(entry) -> Promise<string[]>, or throws if discovery isn't
//     possible/reachable — server/models/registry.js's discoverModels() is
//     what catches that and turns it into {models, error} for callers
//   friendlyError(err) -> string
//   CAPABILITIES -> {video, audio, vision, webSearch} — what this wire
//     format can express at all, the ceiling for every model saved under it.
//     Optional per-adapter extras behind those flags:
//       searchGrounded(entry, query) -> {answer, sources[]}  (webSearch)
//       uploadFile(entry, path, mimeType) -> {uri, mimeType, cleanup()} (video/audio)
//     server/ai.js checks the flag before ever calling one, so an adapter
//     that declares a capability false never needs the function at all.

import * as gemini from './gemini.js';
import * as anthropic from './anthropic.js';
import * as openaiCompatible from './openai-compatible.js';

const ADAPTERS = {
  gemini,
  anthropic,
  'openai-compatible': openaiCompatible,
};

export const ADAPTER_NAMES = Object.keys(ADAPTERS);

export function getAdapter(name) {
  const mod = ADAPTERS[name];
  if (!mod) throw new Error(`Unknown adapter: ${name}`);
  return mod;
}

// Nothing declared means nothing supported — a new adapter that forgets to
// export CAPABILITIES is treated as text-only rather than silently assumed
// to handle video.
const NO_CAPABILITIES = { video: false, audio: false, vision: false, webSearch: false };

/** What a given adapter's wire format can do. Unknown adapter -> everything false, never a throw (callers use this to filter, not to dispatch). */
export function getCapabilities(name) {
  return ADAPTERS[name]?.CAPABILITIES || NO_CAPABILITIES;
}

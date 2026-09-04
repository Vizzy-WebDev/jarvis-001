// Is the voice pipeline's SERVER-SIDE half responsive? Deliberately scoped
// — the server can only see whether TTS/STT providers are configured and
// Gemini Live has a resolvable key; the BROWSER half (mic capture, actual
// playback) is invisible from here with no tab open, same honest limit
// reachability.js's own header comment documents for the general case.
// This checks CONFIGURATION reachability, not a live connection attempt —
// a real per-provider network probe on every tick would be wasteful; this
// is what "is the pipeline capable of responding at all right now" means
// without spending a real request every few minutes.

import * as tts from '../../../tts/index.js';
import { isConfigured as deepgramConfigured } from '../../../stt/deepgram.js';
import { hasGeminiKey } from '../../../gemini-key.js';

export const id = 'voice-pipeline';

export async function probe() {
  // tts.isConfigured() with NO argument resolves to prefs.ttsProvider
  // (tts/index.js's resolveAdapter()) — and nothing in this codebase ever
  // WRITES that pref (a last-resort fallback only; the real, per-call
  // provider always rides on the client's own request — see prefs.js's own
  // comment on it). Calling it bare here made this check structurally
  // unable to ever pass, regardless of what the user actually has
  // configured — confirmed live: it reported "no TTS provider configured"
  // on every tick, which is exactly the sentence Jarvis then relayed to the
  // user as fact even with a real, working voice service saved. The real
  // question is "does at least one configured external service actually
  // work" — tts.listProviders() already answers that per-service via each
  // adapter's own matchesRef()/isConfigured(), so read THAT instead of
  // re-deriving it from a pref nothing sets.
  const providers = tts.listProviders();
  const ttsOk = providers.some((p) => p.configured);
  // Deepgram not being configured is NOT a finding on its own — the
  // browser's own free SpeechRecognition fallback is always available with
  // no key at all (see root CLAUDE.md's TTS provider system section). Only
  // a genuinely BROKEN state is worth surfacing: TTS is the one path with
  // no automatic no-key fallback (voiceOutput can be set to a real provider
  // with no working key behind it), so that's the one real finding here.
  if (!ttsOk) {
    return { ok: false, detail: 'No TTS provider is currently configured and working — voice replies may silently fail depending on the current voiceOutput setting.' };
  }
  return { ok: true, detail: `Deepgram STT ${deepgramConfigured() ? 'configured' : 'not configured (browser fallback available)'}; Gemini Live key ${hasGeminiKey() ? 'resolvable' : 'not resolvable'}.` };
}

// No remedy() — a missing/invalid provider key needs the owner to add one;
// nothing here can supply a real credential on its own.

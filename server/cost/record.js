// The write side of cost tracking — a thin, deliberately trivial seam over
// cost-store.js so every real call site (runner.js's model turns,
// tts/index.js's synthesis calls, stt/deepgram.js's connection time) has one
// obvious function to call rather than reaching into cost-store.js's raw
// table shape directly. Zero model calls, ever — same "leaf-safe, cheap,
// never on the critical path" discipline as self/self-capture.js.
//
// Every function here is wrapped in its own try/catch and never rethrows —
// a cost-recording failure must never break the turn/call it's measuring,
// the same principle self-capture.js's recordAttempt() already established
// for the Self-Model's own recorder.

import { recordEvent } from './cost-store.js';

/** A model turn's token usage — called from models/runner.js on a 'usage' adapter event. */
export function recordModelUsage({ provider, modelId, unitsIn, unitsOut, cachedIn, sessionId, background }) {
  try {
    recordEvent({ provider, modelId, unitKind: 'tokens', unitsIn, unitsOut, cachedIn, sessionId, background });
  } catch (err) {
    console.error('[cost] recordModelUsage failed — a real cost event was NOT recorded:', err);
  }
}

/** A TTS synthesis call's character count. */
export function recordTtsUsage({ provider, characters, sessionId, background = false }) {
  try {
    recordEvent({ provider, unitKind: 'characters', unitsOut: characters, sessionId, background });
  } catch (err) {
    console.error('[cost] recordTtsUsage failed — a real cost event was NOT recorded:', err);
  }
}

/** An STT session's real connected duration, in seconds. */
export function recordSttUsage({ provider, seconds, sessionId, background = false }) {
  try {
    recordEvent({ provider, unitKind: 'seconds', unitsOut: seconds, sessionId, background });
  } catch (err) {
    console.error('[cost] recordSttUsage failed — a real cost event was NOT recorded:', err);
  }
}

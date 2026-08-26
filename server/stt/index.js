// Provider-agnostic seam for real-time speech-to-text — mirrors tts/index.js.
// Currently one real provider (Deepgram); "browser" (Chrome's built-in
// SpeechRecognition) is deliberately NOT a module here — it talks directly
// to Google from the browser with no server involvement at all, so it has
// nothing to proxy. server/duplex.js tells the client which mode is active
// (isConfigured() below decides), and the browser-STT fallback path never
// touches this file or duplex.js's WebSocket at all — see duplex.js's header
// comment.

import * as deepgram from './deepgram.js';

/** Whether a real-time server-proxied STT provider is available right now. False means the client falls back to its own local SpeechRecognition entirely. */
export function isConfigured() {
  return deepgram.isConfigured();
}

export { connect, testKey } from './deepgram.js';

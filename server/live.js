// WebSocket proxy to Gemini Live — Engine B's server half. Browsers never
// talk to Google directly (that would expose the API key); they connect to
// OUR WebSocket at /api/live, and this file relays audio + tool calls
// between that connection and a real ai.live.connect() session.
//
// Verified directly against the installed @google/genai SDK source (not
// just docs, which described a surface this SDK version didn't actually
// expose): ai.live.connect() is real, and onmessage delivers a fully-parsed
// LiveServerMessage object (confirmed by reading handleWebSocketMessage in
// the SDK itself) — .toolCall, .serverContent, .data/.text getters all
// work exactly like the rest of this codebase's Gemini calls.
//
// NOT LIVE-VERIFIED END-TO-END: the shared test key's Gemini quota was
// exhausted for the day before this could be tried against a real session.
// The shapes below are confirmed correct from SDK source; an actual voice
// round-trip still needs to be tried with a working key.

import { WebSocketServer } from 'ws';
import { GoogleGenAI } from '@google/genai';
import { getGeminiKey } from './gemini-key.js';
import { getToolDeclarations, invoke } from './capabilities.js';
import { systemInstructionParts } from './prompt.js';

const MODEL = 'gemini-live-2.5-flash-preview';

// Live mode always uses Gemini directly (like server/tts.js and
// server/turn-check.js) rather than going through the model registry — it's
// a fundamentally different, Gemini-specific real-time API, not a normal
// chat turn a different model could substitute for. Uses the same shared
// system instruction as every text-mode model (server/prompt.js), so the
// voice-clarity confirmation read-back behaves identically in both engines.

// noServer: true, not {server, path} — see server.js's header comment on
// attachUpgradeDispatcher() for why: a second WebSocketServer using
// {server, path} on the SAME httpServer (duplex.js's /api/duplex) breaks
// this one. Every {server,...} instance registers its own unconditional
// 'upgrade' listener, and ws's own path filter rejects a mismatched
// request by destroying the socket with a 400 — whichever instance was
// registered FIRST wins that race for every OTHER path, silently 400ing
// requests meant for the other socket before it ever gets a turn. Found
// live: this file (registered first) was 400ing every /api/duplex
// connection attempt, surfacing to the user as "Could not reach the
// Jarvis server" with no indication the actual cause was two WebSocket
// servers stepping on each other. This function no longer touches
// httpServer's upgrade handling at all — it just builds the WSS instance;
// server.js's single dispatcher calls handleUpgrade() on whichever
// instance matches the request path.
export function createLiveWss() {
  const wss = new WebSocketServer({ noServer: true });

  wss.on('connection', (browserWs) => {
    handleConnection(browserWs).catch((err) => {
      console.error('[live] connection handler error:', err);
      try {
        browserWs.close();
      } catch {
        // already closed
      }
    });
  });

  return wss;
}

async function handleConnection(browserWs) {
  const send = (obj) => {
    if (browserWs.readyState === browserWs.OPEN) browserWs.send(JSON.stringify(obj));
  };

  // getGeminiKey (not getProviderKey) so a key added through Model Settings
  // as a connection secret counts too — see gemini-key.js.
  const apiKey = getGeminiKey();
  if (!apiKey) {
    send({ type: 'error', error: 'No Gemini API key is set up yet.', code: 'NO_API_KEY' });
    browserWs.close();
    return;
  }

  const ai = new GoogleGenAI({ apiKey });
  // unlocked: true — Gemini Live sets its tool list once at connect() time
  // with no per-turn refresh, so it opts out of the core/find_capability
  // split entirely rather than being silently capped at ~11 tools with no
  // way to reach anything else (see getToolDeclarations()'s comment).
  const tools = [{ functionDeclarations: getToolDeclarations({ unlocked: true }) }];

  let liveSession = null;
  let clientClosed = false;

  try {
    liveSession = await ai.live.connect({
      model: MODEL,
      config: {
        responseModalities: ['AUDIO'],
        tools,
        // The `stable` half of systemInstructionParts() — this used to be the
        // bare SYSTEM_INSTRUCTION constant, which silently gave Live NONE of
        // the personality framework's two hard rules (never personal; distress
        // outranks directness), let alone memory/connectors. `volatile` (the
        // per-turn floors/timestamp) is deliberately left out: Live sets its
        // system instruction once at connect() with no per-turn refresh, so
        // there is no hook to re-inject a per-turn computed floor later —
        // Live gets the always-stated rules only, not per-turn floor detection
        // (see personality.js's header comment; a real, accepted gap, not
        // parity with the text/voice-pipeline path).
        systemInstruction: systemInstructionParts({}).stable,
        inputAudioTranscription: {},
        outputAudioTranscription: {},
      },
      callbacks: {
        onopen: () => send({ type: 'ready' }),
        onmessage: (message) => onGeminiMessage(message, liveSession, send),
        onerror: () => send({ type: 'error', error: 'Lost connection to Gemini Live.' }),
        onclose: () => {
          if (!clientClosed) send({ type: 'error', error: 'Gemini Live connection closed.' });
        },
      },
    });
  } catch (err) {
    console.error('[live] connect error:', err);
    send({ type: 'error', error: 'Could not connect to Gemini Live.' });
    browserWs.close();
    return;
  }

  browserWs.on('message', (raw) => {
    let msg;
    try {
      msg = JSON.parse(raw.toString());
    } catch {
      return;
    }

    if (msg.type === 'audio') {
      // Mic audio from the browser: raw 16-bit PCM, 16kHz, base64-encoded.
      liveSession.sendRealtimeInput({ media: { data: msg.data, mimeType: 'audio/pcm;rate=16000' } });
    } else if (msg.type === 'text') {
      liveSession.sendClientContent({ turns: msg.text, turnComplete: true });
    } else if (msg.type === 'end') {
      liveSession.sendRealtimeInput({ audioStreamEnd: true });
    }
  });

  browserWs.on('close', () => {
    clientClosed = true;
    try {
      liveSession?.close();
    } catch {
      // already closed
    }
  });
}

async function onGeminiMessage(message, liveSession, send) {
  // Tool calls: run the same capabilities.js seam used everywhere else in
  // the app, then report the result back to Gemini so it can keep talking.
  if (message.toolCall?.functionCalls?.length) {
    const functionResponses = [];
    for (const call of message.toolCall.functionCalls) {
      send({ type: 'tool_start', name: call.name });
      const result = await invoke(call.name, call.args);
      // Same shape as the pipeline path's tool_result event (server/models/runner.js)
      // so the UI's confirmation chips and voice-navigation handling work
      // identically regardless of which engine is active.
      send({
        type: 'tool_result',
        name: call.name,
        ok: result?.ok !== false,
        ui_action: result?.ui_action,
        needs_confirmation: result?.needs_confirmation,
        summary: result?.summary,
      });
      functionResponses.push({ id: call.id, name: call.name, response: result });
    }
    liveSession.sendToolResponse({ functionResponses });
    return;
  }

  const sc = message.serverContent;
  if (!sc) return;

  if (message.data) {
    // Base64 raw PCM audio, 24kHz — played directly by the browser's live-engine.
    send({ type: 'audio', data: message.data });
  }
  if (sc.inputTranscription?.text) {
    send({ type: 'transcript_in', text: sc.inputTranscription.text });
  }
  if (sc.outputTranscription?.text) {
    send({ type: 'transcript_out', text: sc.outputTranscription.text });
  }
  if (sc.interrupted) {
    send({ type: 'interrupted' });
  }
  if (sc.turnComplete) {
    send({ type: 'turn_complete' });
  }
}

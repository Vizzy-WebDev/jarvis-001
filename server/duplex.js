// WebSocket proxy for the duplex voice engine's speech-to-text half —
// browser mic audio in, Deepgram transcript events out. Mirrors live.js's
// role for Gemini Live: the browser never holds a Deepgram key, only this
// server does.
//
// Deliberately narrow — this is STT ONLY, not a fused pipeline. Reasoning
// (the model call) still goes through the existing, unchanged
// /api/chat/stream; text-to-speech goes through the existing (now
// provider-agnostic) /api/tts. Keeping this socket to "audio in, transcript
// events out" is what keeps the four layers (recognition, reasoning, tools,
// synthesis) genuinely separate and independently swappable, per the design
// goal — a fused audio-to-audio proxy would collapse that distinction right
// back down to what live.js already is for Gemini Live specifically.
//
// No key configured: this socket still accepts the connection (so the
// client doesn't need a separate "can I even try" round-trip), immediately
// reports `{type:'ready', mode:'browser'}`, and closes — the client falls
// back to its own local SpeechRecognition entirely, never sending audio over
// this socket at all in that mode. See duplex-engine.js.

import { WebSocketServer } from 'ws';
import * as stt from './stt/index.js';

// noServer: true, not {server, path} — see live.js's createLiveWss() and
// server.js's attachUpgradeDispatcher() for why this matters: this is the
// bug that made Full-duplex fail with "Could not reach the Jarvis server"
// on every real attempt. Two WebSocketServer instances both using
// {server, path} on the SAME httpServer don't coexist — the one registered
// first (live.js) 400s every request meant for the second one before it
// ever gets a turn. This function no longer touches httpServer's upgrade
// handling at all; server.js's single dispatcher owns that.
export function createDuplexWss() {
  const wss = new WebSocketServer({ noServer: true });

  wss.on('connection', (browserWs) => {
    handleConnection(browserWs).catch((err) => {
      console.error('[duplex] connection handler error:', err);
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

  if (!stt.isConfigured()) {
    send({ type: 'ready', mode: 'browser' });
    browserWs.close();
    return;
  }

  let session = null;
  let clientClosed = false;
  // Bounded to one server-side reconnect per actual disconnect, reset back
  // to false once a reconnected session is CONFIRMED healthy (its own
  // onOpen fires) — see openSession()'s onOpen below. Mirrors
  // duplex-engine.js's client-side _handleDeepgramDisconnect(), which
  // handles a DIFFERENT failure (the outer browser<->server socket itself
  // closing). This one handles the Deepgram session dying while that outer
  // socket stays fully open the whole time — found by tracing a real report
  // of "mic shows on, stops picking up speech" happening in normal
  // hands-free use, not tied to extended mute, which the outer-socket fix
  // doesn't reach at all: nothing on the browser side ever sees a close
  // event for this case, only an informational error message, so the
  // browser's mic just keeps sending frames into a dead, never-replaced
  // Deepgram session forever (sendAudio() silently no-ops on a closed
  // socket — see stt/deepgram.js).
  let reconnecting = false;

  const openSession = () =>
    stt.connect({
      onOpen: () => {
        reconnecting = false; // confirmed healthy — a LATER disconnect gets its own fresh attempt
        send({ type: 'ready', mode: 'deepgram' });
      },
      onTranscript: ({ text, isFinal, speechFinal, confidence }) =>
        send({ type: 'transcript', text, isFinal, speechFinal, confidence }),
      onUtteranceEnd: () => send({ type: 'utterance_end' }),
      onSpeechStarted: () => send({ type: 'speech_started' }),
      onError: (err) => {
        console.error('[duplex] Deepgram session error:', err);
        send({ type: 'error', error: err?.message || 'Lost connection to speech recognition.', code: err?.code });
      },
      onClose: () => {
        if (clientClosed) return; // the browser itself hung up — nothing to recover
        if (!reconnecting) {
          // Reopen a fresh Deepgram session on this SAME browser socket —
          // the browser's mic never stopped sending frames and never needs
          // to know this happened. A brief gap while the new session's own
          // handshake completes is expected (sendAudio() no-ops on the
          // not-yet-open new socket, same as it always does) — far shorter
          // than "dead for the rest of the session", the bug being fixed.
          reconnecting = true;
          try {
            session = openSession();
            return;
          } catch (err) {
            console.error('[duplex] Deepgram reconnect failed:', err);
          }
        }
        // Either a retry is already in flight and failed again in this
        // same tight window, or reopening threw synchronously — give up
        // and let the browser side know. Closing browserWs (rather than
        // leaving it open with nothing behind it) hands off to
        // duplex-engine.js's own client-side reconnect as a second,
        // independent layer, same "bounded retry, then an actionable
        // message" shape, one level up.
        send({ type: 'error', error: 'Speech recognition connection closed.' });
        try {
          browserWs.close();
        } catch {
          // already closed
        }
      },
    });

  try {
    session = openSession();
  } catch (err) {
    console.error('[duplex] could not start Deepgram session:', err);
    send({ type: 'error', error: err?.message || 'Could not start speech recognition.', code: err?.code });
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

    if (msg.type === 'audio' && typeof msg.data === 'string') {
      // Raw 16-bit PCM, 16kHz mono, base64-encoded — see mic-stream.js.
      session.sendAudio(Buffer.from(msg.data, 'base64'));
    } else if (msg.type === 'end') {
      session.close();
    }
  });

  browserWs.on('close', () => {
    clientClosed = true;
    try {
      session.close();
    } catch {
      // already closed
    }
  });
}

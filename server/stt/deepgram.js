// Deepgram streaming speech-to-text — one real-time session per connect().
//
// VERIFIED LIVE against the real endpoint (2026-08-23, real key, real
// wss://api.deepgram.com/v1/listen connection) before writing any of this —
// per CLAUDE.md's explicit warning that a live API's docs can describe a
// surface that doesn't match the installed/actual behavior (this project
// already got burned once, on Gemini's "Interactions API"). Confirmed live:
//   - Auth: `Authorization: Token <key>` header on the WS upgrade request.
//   - Success: connection opens, `Results` messages arrive shaped
//     {type, is_final, speech_final, channel:{alternatives:[{transcript,
//     confidence, words}]}, ...}, and a final `Metadata` message on close.
//   - Bad key: NOT a WebSocket 'error' — the upgrade itself is rejected with
//     HTTP 401 and JSON body {err_code, err_msg, request_id}; the `ws`
//     library surfaces this via 'unexpected-response', not 'error'. Missing
//     that event means an invalid key silently looks like "connected fine,
//     just never hears anything."
//   - Bad query param: same 'unexpected-response' shape, HTTP 400.
//   - Close after sending {type:'CloseStream'}: code 1000 (normal).
// UtteranceEnd/SpeechStarted (vad_events) were NOT observed directly in that
// test (only silence was sent — nothing triggered them), but Results'
// is_final/speech_final fields — the ones turn-taking actually depends
// on — were confirmed on every message, live.

import WebSocket from 'ws';
import { getSecret } from '../config.js';

const DEEPGRAM_URL = 'wss://api.deepgram.com/v1/listen';

export function isConfigured() {
  return Boolean(getSecret('deepgram'));
}

/**
 * Opens one real-time Deepgram session. `callbacks`:
 *   onOpen()
 *   onTranscript({text, isFinal, speechFinal, confidence})
 *   onUtteranceEnd()      — Deepgram's own turn-taking signal; see duplex.js
 *   onSpeechStarted()     — fires as soon as Deepgram's VAD hears speech begin
 *   onError(err)
 *   onClose(code, reason)
 * Returns {sendAudio(buffer), close()}. Throws synchronously (before ever
 * opening a socket) if no key is configured — callers should check
 * isConfigured() first rather than relying on this throw as the primary path.
 */
export function connect({ onOpen, onTranscript, onUtteranceEnd, onSpeechStarted, onError, onClose } = {}) {
  const key = getSecret('deepgram');
  if (!key) {
    const err = new Error('No Deepgram API key configured.');
    err.code = 'NO_API_KEY';
    throw err;
  }

  // encoding/sample_rate/channels must match exactly what mic-stream.js
  // actually sends (raw PCM16, 16kHz, mono) — a mismatch here doesn't error,
  // it just produces garbage transcripts, since Deepgram has no way to know
  // the params it was told don't match the bytes arriving.
  const params = new URLSearchParams({
    encoding: 'linear16',
    sample_rate: '16000',
    channels: '1',
    interim_results: 'true',
    // How long Deepgram waits in silence before marking a result
    // speech_final — this REPLACES public/turn-detector.js's fixed
    // computeWaitMs() countdown; Deepgram's own endpointing IS the
    // turn-taking signal now, not a client-side timer guessing from the
    // trailing word.
    endpointing: '300',
    utterance_end_ms: '1000',
    vad_events: 'true',
    smart_format: 'true',
    model: 'nova-3',
  });

  const ws = new WebSocket(`${DEEPGRAM_URL}?${params.toString()}`, {
    headers: { Authorization: `Token ${key}` },
  });

  // Set by close() below — an error/close firing AFTER we ourselves asked to
  // close is expected, not a real problem, and must never reach onError.
  // Found live: closing while the handshake is still in flight (a real,
  // reachable case — the user stops almost immediately after starting) has
  // `ws` itself emit an 'error' event reading "WebSocket was closed before
  // the connection was established" — without this flag that surfaced to
  // the app as a genuine failure notification for something the app itself
  // asked to happen.
  let closingIntentionally = false;

  ws.on('open', () => onOpen?.());

  // See this file's header comment — a rejected upgrade (bad key, bad
  // params) surfaces here, never via 'error'.
  ws.on('unexpected-response', (req, res) => {
    let body = '';
    res.on('data', (c) => (body += c));
    res.on('end', () => {
      let parsed = null;
      try {
        parsed = JSON.parse(body);
      } catch {
        // non-JSON body — fall through to the generic message below
      }
      const message =
        parsed?.err_msg ||
        (res.statusCode === 401
          ? 'Deepgram rejected the API key.'
          : `Deepgram rejected the connection (${res.statusCode}).`);
      const err = new Error(message);
      err.code = res.statusCode === 401 ? 'NO_API_KEY' : 'DEEPGRAM_REJECTED';
      onError?.(err);
    });
  });

  ws.on('message', (data) => {
    let msg;
    try {
      msg = JSON.parse(data.toString());
    } catch {
      return;
    }
    if (msg.type === 'Results') {
      const alt = msg.channel?.alternatives?.[0];
      const text = alt?.transcript || '';
      // Deepgram sends Results messages on pure silence too (confirmed
      // live — empty transcript, confidence 0) — nothing worth surfacing.
      if (!text) return;
      onTranscript?.({
        text,
        isFinal: Boolean(msg.is_final),
        speechFinal: Boolean(msg.speech_final),
        confidence: typeof alt.confidence === 'number' ? alt.confidence : null,
      });
    } else if (msg.type === 'UtteranceEnd') {
      onUtteranceEnd?.();
    } else if (msg.type === 'SpeechStarted') {
      onSpeechStarted?.();
    }
    // 'Metadata' (sent once, right before close) carries nothing this app needs.
  });

  // Only 'error' is suppressed on an intentional close — that's the
  // confusing "closed before established" message ws itself emits as a
  // reaction to our own close() call (see closingIntentionally's comment
  // above). 'close' must still fire regardless of who initiated it — the
  // caller needs the close confirmation either way to know the session is
  // actually done (duplex.js relays nothing further once this fires).
  // Conflating the two was a real bug caught here: an earlier version
  // suppressed both, which silently broke the NORMAL graceful-close path
  // too — close() was called, CloseStream was sent, but the caller never
  // found out the session had actually ended.
  ws.on('error', (err) => {
    if (closingIntentionally) return;
    onError?.(err);
  });
  ws.on('close', (code, reason) => onClose?.(code, reason?.toString() || ''));

  return {
    sendAudio(buffer) {
      if (ws.readyState === WebSocket.OPEN) ws.send(buffer);
    },
    /**
     * Graceful stop — CloseStream lets Deepgram flush a final result before
     * the socket actually closes, rather than just cutting it. If the
     * handshake hasn't finished yet (readyState CONNECTING), there's no open
     * connection to send CloseStream on or gracefully drain — terminate()
     * immediately instead of close(), which skips the closing handshake
     * ws would otherwise wait on for a socket that was never really open.
     */
    close() {
      closingIntentionally = true;
      if (ws.readyState === WebSocket.CONNECTING) {
        ws.terminate();
        return;
      }
      if (ws.readyState === WebSocket.OPEN) {
        try {
          ws.send(JSON.stringify({ type: 'CloseStream' }));
        } catch {
          // fall through to the raw close below regardless
        }
      }
      ws.close();
    },
  };
}

/**
 * Tests a Deepgram key directly — independent of whatever (if anything) is
 * currently saved via config.js, and never touching it. Used by the in-app
 * key-management UI's "test this key" action, same shape as every model
 * adapter's testConnection(entry) (see e.g. adapters/gemini.js): resolves
 * {ok:true} or {ok:false, error}, never throws. Reuses the exact
 * unexpected-response/error handling this file's header comment already
 * documents as verified live against the real endpoint — just the
 * connection handshake, immediately terminated, no audio ever sent, so this
 * costs nothing against Deepgram's usage-based pricing.
 */
export function testKey(key) {
  return new Promise((resolve) => {
    const trimmed = String(key || '').trim();
    if (!trimmed) {
      resolve({ ok: false, error: 'No key provided.' });
      return;
    }

    const params = new URLSearchParams({ encoding: 'linear16', sample_rate: '16000', channels: '1' });
    const ws = new WebSocket(`${DEEPGRAM_URL}?${params.toString()}`, {
      headers: { Authorization: `Token ${trimmed}` },
    });

    let settled = false;
    const settle = (result) => {
      if (settled) return;
      settled = true;
      clearTimeout(timeout);
      resolve(result);
      try {
        ws.terminate(); // never a real session — no CloseStream handshake to run, same as close()'s own CONNECTING branch
      } catch {
        // already closed
      }
    };
    const timeout = setTimeout(() => settle({ ok: false, error: 'Deepgram did not respond in time.' }), 8000);

    ws.on('open', () => settle({ ok: true }));

    // Same shape as connect()'s identical handler above — a rejected
    // upgrade (bad key, bad params) surfaces here, never via 'error'.
    ws.on('unexpected-response', (req, res) => {
      let body = '';
      res.on('data', (c) => (body += c));
      res.on('end', () => {
        let parsed = null;
        try {
          parsed = JSON.parse(body);
        } catch {
          // non-JSON body — fall through to the generic message below
        }
        const message =
          parsed?.err_msg ||
          (res.statusCode === 401
            ? 'Deepgram rejected the API key.'
            : `Deepgram rejected the connection (${res.statusCode}).`);
        settle({ ok: false, error: message });
      });
    });

    ws.on('error', () => {
      settle({ ok: false, error: 'Could not reach Deepgram.' });
    });
  });
}

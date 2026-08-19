// Plays Gemini TTS audio sentence-by-sentence as it becomes available,
// gaplessly, with instant stop for barge-in.
//
// Each sentence is fetched from /api/tts as soon as its text is ready — often
// while the PREVIOUS sentence is still playing — so playback doesn't stall
// waiting on generation. Slots are reserved in the queue synchronously
// (before the fetch resolves) so a slower-arriving earlier sentence can
// never be overtaken by a faster-arriving later one.

import { buildEnvelope, sampleEnvelope } from './voice-envelope.js';

const SENTENCE_END = /[^.!?]*[.!?]+(\s+|$)/;

// How long to wait before retrying a failed /api/tts fetch.
const TTS_RETRY_DELAY_MS = 400;

/**
 * Fetches one sentence's TTS audio, retrying once after a short delay before
 * giving up. Never throws — resolves null on final failure, same contract
 * enqueueText() always had. A transient network blip or a momentary Gemini
 * TTS rate limit (this project's free-tier quota is low — see CLAUDE.md) is
 * exactly the kind of failure a single retry fixes; before this, ANY failure
 * silently dropped that sentence, and several dropping in a row near the end
 * of a reply (the likely case, since sentences all start fetching close
 * together as the model streams the text) looked exactly like "Jarvis just
 * stopped talking" with the on-screen text still complete.
 *
 * Returns `{url, blob}` (or null) — the blob rides along so _advance() can
 * hand a SEPARATE copy of the same bytes to voice-envelope.js for the orb,
 * without the playback path itself changing at all (see that file's header
 * comment for why that separation matters).
 */
async function fetchTts(text, voice, attempt = 0) {
  try {
    const res = await fetch('/api/tts', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text, voice }),
    });
    if (!res.ok) throw new Error(`TTS request failed (${res.status})`);
    const blob = await res.blob();
    return { url: URL.createObjectURL(blob), blob };
  } catch (err) {
    if (attempt === 0) {
      await new Promise((r) => setTimeout(r, TTS_RETRY_DELAY_MS));
      return fetchTts(text, voice, attempt + 1);
    }
    console.warn('[audio-player] TTS request failed twice, skipping this sentence:', err);
    return null; // a missed sentence isn't worth breaking the rest of playback over
  }
}

// A Web Audio tap (AnalyserNode on every <audio> element, for the orb to
// react to Jarvis's real voice) used to live here. Removed: once an element
// is routed through createMediaElementSource, it plays ONLY via that graph —
// if the shared AudioContext was ever suspended (autoplay-policy territory)
// when a reply started, resume() is async and isn't something play() waits
// on, so the first moment of speech could come out clipped or silent. That's
// too much risk to the thing this project is actually for (a working voice
// assistant) for a cosmetic orb effect. `new Audio(url); audio.play()`
// below is still exactly that plain — getOutputLevel() instead reads an
// OFFLINE-decoded envelope of a separate copy of the same bytes (see
// voice-envelope.js), never touching this playback path.

export class AudioPlayer {
  constructor({ onStart, onIdle, voice } = {}) {
    this.onStart = onStart;
    this.onIdle = onIdle;
    this.voice = voice;
    this.queue = []; // Promise<{url,blob}|null> — see fetchTts()'s doc comment
    this.currentAudio = null;
    this.currentEnvelope = null; // for the orb — see getOutputLevel() below
    this.playing = false;
    this.pending = 0;
    this.streamEnded = false;
    this.stopped = false;
    this._buffer = '';
  }

  /** For the orb: Jarvis's own voice output energy while speaking, 0..1 — sampled from an offline-decoded envelope of whatever's currently playing (see voice-envelope.js). 0 if nothing's playing yet, or the envelope hasn't finished building/failed to build for this clip. */
  getOutputLevel() {
    if (!this.currentAudio || !this.currentEnvelope) return 0;
    return sampleEnvelope(this.currentEnvelope, this.currentAudio.currentTime);
  }

  /** Feeds streamed text in; complete sentences are queued for playback as soon as they're ready. */
  pushText(textChunk) {
    this._buffer += textChunk;
    let match;
    while ((match = SENTENCE_END.exec(this._buffer)) && match[0].trim()) {
      const sentence = match[0].trim();
      this._buffer = this._buffer.slice(match[0].length);
      this.enqueueText(sentence);
    }
  }

  /** Queues a sentence for playback; starts fetching (with retry — see fetchTts()) immediately. */
  enqueueText(text, { voice = this.voice } = {}) {
    if (this.stopped || !text) return;
    this.pending++;
    this.queue.push(fetchTts(text, voice));
    if (!this.playing) this._advance();
  }

  /** Call once the text stream is fully done — flushes any trailing partial sentence. */
  end() {
    const rest = this._buffer.trim();
    this._buffer = '';
    if (rest) this.enqueueText(rest);
    this.streamEnded = true;
    this._maybeIdle();
  }

  async _advance() {
    if (this.stopped) return;

    if (this.queue.length === 0) {
      this.playing = false;
      this._maybeIdle();
      return;
    }

    this.playing = true;
    const next = this.queue.shift();
    const result = await next;
    if (this.stopped) return; // stop() fired while we were waiting on this fetch
    this.pending--;

    if (!result) {
      this._advance();
      return;
    }
    const { url, blob } = result;

    if (this.onStart) this.onStart();

    const audio = new Audio(url);
    this.currentAudio = audio;
    this.currentEnvelope = null; // cleared until buildEnvelope() below resolves — getOutputLevel() reads 0 until then, same as "no envelope" on failure
    const done = () => {
      URL.revokeObjectURL(url);
      this._advance();
    };
    audio.onended = done;
    audio.onerror = done;
    audio.play().catch(done);

    // Decoded from a SEPARATE copy of the same bytes (see fetchTts()'s
    // return shape and this file's header comment) — never gates or touches
    // the `audio.play()` above in any way, including on failure.
    buildEnvelope(blob).then((envelope) => {
      if (this.currentAudio === audio) this.currentEnvelope = envelope;
    });
  }

  _maybeIdle() {
    if (this.streamEnded && this.pending <= 0 && !this.playing && this.onIdle) {
      this.onIdle();
    }
  }

  /** Instantly stops playback and drops anything queued — barge-in. */
  stop() {
    this.stopped = true;
    if (this.currentAudio) {
      this.currentAudio.pause();
      this.currentAudio = null;
    }
    this.currentEnvelope = null;
    this.queue = [];
    this.playing = false;
    this.pending = 0;
    this._buffer = '';
  }

  /** Prepares the player for a fresh reply after stop(). */
  reset() {
    this.stopped = false;
    this.streamEnded = false;
    this._buffer = '';
  }
}

// Plays TTS audio as the model's reply streams in, same gapless/pipelined
// approach as audio-player.js (a slot is reserved in the queue the instant
// text is ready, before the fetch resolves, so a slow-arriving earlier chunk
// can never be overtaken by a faster later one) — this file exists
// separately rather than reusing AudioPlayer directly because the duplex
// engine needs two things that file doesn't do:
//   1. Clause-boundary chunking, not only sentence-boundary — the first
//      audio should start at the first substantial clause, not wait for a
//      full sentence, for lower perceived latency. Gated by a minimum
//      length so it doesn't fire on "Hello," and sound choppy.
//   2. A running transcript of what has actually STARTED playing (not
//      merely been fetched) — read on interrupt and sent to the server as
//      the real spoken text, so the stored reply can be truncated to what
//      the user actually heard rather than what the model merely finished
//      generating. Sent as reconstructed TEXT, not a character offset into
//      the original stream — an offset would have to be sliced back out of
//      a string whose whitespace may not exactly match (chunk boundaries
//      trim differently than the model's own spacing); the exact spoken
//      text has no such ambiguity. See duplex-engine.js's interrupt() and
//      server.js's /api/chat/interrupt route.
//
// Never touches the live playback graph for orb reactivity, same reasoning
// as audio-player.js's header comment — reads an OFFLINE-decoded envelope of
// a separate copy of the same bytes instead.

import { buildEnvelope, sampleEnvelope } from '../voice-envelope.js';

const SENTENCE_END = /[^.!?]*[.!?]+(\s+|$)/;
// A clause boundary (comma/semicolon/colon/dash) is only taken as a chunk
// point once the pending buffer is already at least this long — otherwise
// "Hello," would become its own audio request, sounding choppy and
// multiplying TTS calls for no latency benefit (a short clause is already
// about to hit its sentence end anyway).
const CLAUSE_END = /[^.!?,;:—]*[,;:—]+(\s+|$)/;
const MIN_CLAUSE_CHARS = 24;

const TTS_RETRY_DELAY_MS = 400; // same reasoning as audio-player.js's identical constant

/** Finds the next speakable chunk in `buffer`, or null if nothing is ready yet. Prefers a full sentence; falls back to a sufficiently long clause. */
function nextChunk(buffer) {
  const sentenceMatch = SENTENCE_END.exec(buffer);
  if (sentenceMatch && sentenceMatch[0].trim()) {
    return { text: sentenceMatch[0].trim(), consumed: sentenceMatch[0].length };
  }
  const clauseMatch = CLAUSE_END.exec(buffer);
  if (clauseMatch && clauseMatch[0].length >= MIN_CLAUSE_CHARS && clauseMatch[0].trim()) {
    return { text: clauseMatch[0].trim(), consumed: clauseMatch[0].length };
  }
  return null;
}

/** Same retry-once-then-give-up contract as audio-player.js's fetchTts(), extended with `provider` (server/tts/index.js's registry). */
async function fetchTts(text, { voice, provider }, attempt = 0) {
  try {
    const res = await fetch('/api/tts', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text, voice, provider }),
    });
    if (!res.ok) throw new Error(`TTS request failed (${res.status})`);
    const blob = await res.blob();
    return { url: URL.createObjectURL(blob), blob };
  } catch (err) {
    if (attempt === 0) {
      await new Promise((r) => setTimeout(r, TTS_RETRY_DELAY_MS));
      return fetchTts(text, { voice, provider }, attempt + 1);
    }
    console.warn('[voice/playback] TTS request failed twice, skipping this chunk:', err);
    return null;
  }
}

export class Playback {
  constructor({ onStart, onIdle, voice, provider } = {}) {
    this.onStart = onStart;
    this.onIdle = onIdle;
    this.voice = voice;
    this.provider = provider;
    this.queue = []; // Promise<{url,blob,text}|null>
    this.currentAudio = null;
    this.currentEnvelope = null;
    this.playing = false;
    this.pending = 0;
    this.streamEnded = false;
    this.stopped = false;
    this._buffer = '';
    this._spokenText = ''; // reconstructed text of every chunk that has actually started playing
  }

  /** For the orb — see audio-player.js's identical method for the full reasoning. */
  getOutputLevel() {
    if (!this.currentAudio || !this.currentEnvelope) return 0;
    return sampleEnvelope(this.currentEnvelope, this.currentAudio.currentTime);
  }

  /** The text that has actually been heard so far — read on interrupt. Includes the chunk currently playing (already audible) but not queued/pending ones. */
  getSpokenText() {
    return this._spokenText;
  }

  /** Feeds streamed reply text in; speakable chunks are queued as soon as they're ready (see nextChunk()). */
  pushText(textChunk) {
    if (this.stopped) return;
    this._buffer += textChunk;
    let chunk;
    while ((chunk = nextChunk(this._buffer))) {
      this._buffer = this._buffer.slice(chunk.consumed);
      this._enqueue(chunk.text);
    }
  }

  _enqueue(text) {
    if (this.stopped || !text) return;
    this.pending++;
    this.queue.push(
      fetchTts(text, { voice: this.voice, provider: this.provider }).then((r) => (r ? { ...r, text } : null))
    );
    if (!this.playing) this._advance();
  }

  /**
   * Queues a real, pre-recorded sound clip (not TTS-fetched text) — for a
   * 'reaction' event (see server/personality.js's createReactionScanner()
   * and runner.js). Same reasoning and shape as audio-player.js's identical
   * method: reuses this SAME ordered queue so the clip plays at exactly the
   * right position relative to the surrounding speech. `isReaction: true`
   * is the one extra field _advance() checks, to skip updating
   * `_spokenText` for it — a sound effect isn't spoken words, and this
   * text is what a barge-in truncates the stored transcript to (see this
   * file's header comment), so it must never include anything but real
   * speech.
   */
  enqueueClip(url) {
    if (this.stopped || !url) return;
    this.pending++;
    this.queue.push(
      fetch(url)
        .then((r) => r.blob())
        .then((blob) => ({ url, blob, text: '', isReaction: true }))
        .catch(() => ({ url, blob: null, text: '', isReaction: true }))
    );
    if (!this.playing) this._advance();
  }

  /** Call once the text stream is fully done — flushes any trailing partial clause. */
  end() {
    const rest = this._buffer.trim();
    this._buffer = '';
    if (rest) this._enqueue(rest);
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
    if (this.stopped) return; // stop() fired while waiting on this fetch
    this.pending--;

    if (!result) {
      this._advance();
      return;
    }
    const { url, blob, text, isReaction } = result;

    if (this.onStart) this.onStart();
    // Appended the moment this chunk actually STARTS playing, not when it
    // was fetched or queued — pipelining means several chunks can be
    // in-flight ahead of what's audible right now. Skipped for a reaction
    // clip (see enqueueClip()) — it isn't spoken words, and this text is
    // exactly what a barge-in truncates the stored transcript to.
    if (!isReaction) {
      this._spokenText = this._spokenText ? `${this._spokenText} ${text}` : text;
    }

    const audio = new Audio(url);
    this.currentAudio = audio;
    this.currentEnvelope = null;

    // Watchdog against a stalled <audio> element that never fires 'ended'
    // or 'error' and never rejects play() — same mechanism as
    // browser-speaker.js's proven Chrome-dropped-utterance watchdog, ported
    // here (and into audio-player.js) because this path — server-side TTS,
    // this engine's own default voice output — had no equivalent at all
    // before, found during the state-machine audit: without it, `playing`
    // stays true forever and 'speaking' never exits. Sized per-chunk, not
    // per-reply, so a long multi-sentence reply never trips a watchdog
    // sized for the whole thing — a new chunk starting is itself proof of
    // progress. `_advance()`'s own `if (this.stopped) return;` guard above
    // (already relied on for the identical reason by the non-watchdog
    // path) is what makes a late-firing watchdog after stop() harmless —
    // it just calls back into a no-op.
    let settled = false;
    const settle = () => {
      if (settled) return;
      settled = true;
      clearTimeout(watchdog);
      URL.revokeObjectURL(url);
      this._advance();
    };
    const estimatedMs = (text.length / 12) * 1000; // same conservative speaking-rate estimate as browser-speaker.js
    const watchdog = setTimeout(() => {
      console.warn('[voice/playback] this chunk never confirmed it finished playing — continuing anyway.');
      settle();
    }, Math.min(30000, Math.max(6000, estimatedMs * 2.5)));

    audio.onended = settle;
    audio.onerror = settle;
    audio.play().catch(settle);

    // `blob` can be null for a clip whose envelope-copy fetch failed (see
    // enqueueClip()) — the orb just shows no reactivity for that one clip.
    if (blob) {
      buildEnvelope(blob).then((envelope) => {
        if (this.currentAudio === audio) this.currentEnvelope = envelope;
      });
    }
  }

  _maybeIdle() {
    if (this.streamEnded && this.pending <= 0 && !this.playing && this.onIdle) {
      this.onIdle();
    }
  }

  /** Instantly stops playback and drops anything queued — barge-in. `getSpokenText()` still reflects what was truly heard up to this point. */
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

  /**
   * Prepares the player for a fresh reply — used on a model-switch
   * 'restart'. Previously only reset flags, leaving the FAILED model's
   * `currentAudio`/queue fully intact underneath the replacement's — see
   * audio-player.js's identical fix/comment for the full reasoning (found
   * together during the state-machine audit; both files share the bug and
   * the fix). Also resets the spoken-text cursor, since it's per-turn.
   */
  reset() {
    this.stop();
    this.stopped = false;
    this.streamEnded = false;
    this._buffer = '';
    this._spokenText = '';
  }
}

// Plays server-side TTS audio (server/tts/index.js's provider seam — any
// configured provider, e.g. ElevenLabs; Gemini TTS was removed) sentence-
// by-sentence as it becomes available, gaplessly, with instant stop for
// barge-in.
//
// Each sentence is fetched from /api/tts as soon as its text is ready — often
// while the PREVIOUS sentence is still playing — so playback doesn't stall
// waiting on generation. Slots are reserved in the queue synchronously
// (before the fetch resolves) so a slower-arriving earlier sentence can
// never be overtaken by a faster-arriving later one.

import { buildEnvelope, sampleEnvelope } from './voice-envelope.js';

const SENTENCE_END = /[^.!?]*[.!?]+(\s+|$)/;

// A clause boundary (comma/semicolon/colon + whitespace) — a SECONDARY
// flush point used only for the OPENING clause of a reply (see
// pushText()'s _firstFlushDone), so the first /api/tts round trip doesn't
// wait for a full sentence when the model's opening sentence happens to be
// long. Only fires once the clause is already CLAUSE_MIN_CHARS or longer —
// flushing a bare "Sure," alone would sound choppy and cost an extra TTS
// round trip for almost no benefit. CLAUSE_FALLBACK_CHARS is the last
// resort for an opening sentence with no punctuation at all for a while —
// bounds how long first audio can be made to wait either way.
const CLAUSE_END = /[^,;:]*[,;:]+\s+/;
const CLAUSE_MIN_CHARS = 24;
const CLAUSE_FALLBACK_CHARS = 90;

// How long to wait before retrying a failed /api/tts fetch.
const TTS_RETRY_DELAY_MS = 400;

/**
 * Fetches one sentence's TTS audio, retrying once after a short delay before
 * giving up. Never throws — resolves null on final failure, same contract
 * enqueueText() always had. A transient network blip or a momentary TTS
 * provider rate limit is exactly the kind of failure a single retry fixes;
 * before this, ANY failure silently dropped that sentence, and several
 * dropping in a row near the end of a reply (the likely case, since
 * sentences all start fetching close together as the model streams the
 * text) looked exactly like "Jarvis just stopped talking" with the
 * on-screen text still complete.
 *
 * `provider` selects which server/tts/index.js adapter to use (same shape
 * voice/playback.js's identical fetchTts() already had — this file didn't,
 * a real gap: without it, this speaker could only ever use whatever the
 * server's own default provider happened to be, never a specific one the
 * user picked).
 *
 * Returns `{url, blob}` (or null) — the blob rides along so _advance() can
 * hand a SEPARATE copy of the same bytes to voice-envelope.js for the orb,
 * without the playback path itself changing at all (see that file's header
 * comment for why that separation matters).
 *
 * `onFailure(err)` fires once, only on the FINAL (post-retry) failure — never
 * on the first attempt, which is expected to fail sometimes and recovers on
 * its own. Before this existed, a dead/broken TTS provider (an expired key,
 * an account out of credit) failed every single sentence silently: a
 * console.warn nobody but a developer would ever see, and playback that just
 * never started, with the settings panel and the rest of the UI looking
 * completely normal. See app.js's createEngine() for where this is wired to
 * a real, visible notification.
 */
async function fetchTts(text, { voice, provider }, onFailure, attempt = 0) {
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
      return fetchTts(text, { voice, provider }, onFailure, attempt + 1);
    }
    console.warn('[audio-player] TTS request failed twice, skipping this sentence:', err);
    try {
      onFailure?.(err);
    } catch (cbErr) {
      console.error('[audio-player] onFailure callback threw:', cbErr);
    }
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
  constructor({ onStart, onIdle, onFailure, voice, provider } = {}) {
    this.onStart = onStart;
    this.onIdle = onIdle;
    this.onFailure = onFailure;
    this.voice = voice;
    this.provider = provider;
    this.queue = []; // Promise<{url,blob}|null> — see fetchTts()'s doc comment
    this.currentAudio = null;
    this.currentEnvelope = null; // for the orb — see getOutputLevel() below
    this.playing = false;
    this.pending = 0;
    this.streamEnded = false;
    this.stopped = false;
    this._buffer = '';
    // Fired at most once per REPLY (reset in reset(), never per sentence) —
    // several sentences in the same reply failing in a row (the likely case,
    // since a broken provider fails every sentence, not just one) must not
    // spam several identical notifications.
    this._failureNotified = false;
    // see pushText()'s clause-boundary fallback. Reset per reply (here and
    // in reset() below), not per sentence: only the OPENING of a reply is
    // latency-sensitive, everything after it plays while the listener is
    // already hearing audio.
    this._firstFlushDone = false;
  }

  /** For the orb: Jarvis's own voice output energy while speaking, 0..1 — sampled from an offline-decoded envelope of whatever's currently playing (see voice-envelope.js). 0 if nothing's playing yet, or the envelope hasn't finished building/failed to build for this clip. */
  getOutputLevel() {
    if (!this.currentAudio || !this.currentEnvelope) return 0;
    return sampleEnvelope(this.currentEnvelope, this.currentAudio.currentTime);
  }

  /** Feeds streamed text in; complete sentences are queued for playback as soon as they're ready. */
  pushText(textChunk) {
    this._buffer += textChunk;

    // Full sentences always win — best prosody, and the common case once a
    // reply is already under way.
    let match;
    while ((match = SENTENCE_END.exec(this._buffer)) && match[0].trim()) {
      const sentence = match[0].trim();
      this._buffer = this._buffer.slice(match[0].length);
      this.enqueueText(sentence);
      this._firstFlushDone = true;
    }

    // Before this reply's first sentence has gone out, don't make the
    // listener wait on a long opening sentence with no punctuation break
    // yet — flush an opening clause (or, past CLAUSE_FALLBACK_CHARS with
    // still no clause boundary, a bounded chunk of plain words) as soon as
    // there's enough of it to read as a real phrase rather than one word
    // at a time. Applies only once per reply — the rest still speaks in
    // full, natural sentences via the loop above.
    if (!this._firstFlushDone && this._buffer.trim()) {
      const clauseMatch = CLAUSE_END.exec(this._buffer);
      if (clauseMatch && clauseMatch[0].trim().length >= CLAUSE_MIN_CHARS) {
        const clause = clauseMatch[0].trim();
        this._buffer = this._buffer.slice(clauseMatch[0].length);
        this.enqueueText(clause);
        this._firstFlushDone = true;
      } else if (this._buffer.length >= CLAUSE_FALLBACK_CHARS) {
        const chunk = this._buffer.trim();
        this._buffer = '';
        this.enqueueText(chunk);
        this._firstFlushDone = true;
      }
    }
  }

  /**
   * Queues a real, pre-recorded sound clip (not TTS-fetched text) — for a
   * 'reaction' event (see server/personality.js's createReactionScanner()
   * and runner.js). Reuses this SAME ordered queue as spoken sentences so
   * the clip plays at exactly the right position relative to the
   * surrounding speech, rather than a second, parallel scheduling
   * mechanism. `url` is a static, cacheable asset path (e.g.
   * "/sounds/laugh.mp3"), not a per-request TTS result — nothing here owns
   * or must release its lifetime the way a fetchTts() blob URL does.
   */
  enqueueClip(url) {
    if (this.stopped || !url) return;
    this.pending++;
    this.queue.push(
      // A SEPARATE copy of the same bytes, purely for the orb's envelope —
      // never touches the actual <audio> element that plays `url` itself,
      // same reasoning as every other clip in this file's header comment.
      // Falls back to a null blob (no envelope, orb just shows no reactivity
      // for this clip) rather than dropping the clip entirely if this fetch
      // fails — the clip itself still plays via `url` regardless.
      fetch(url)
        .then((r) => r.blob())
        .then((blob) => ({ url, blob, text: '' }))
        .catch(() => ({ url, blob: null, text: '' }))
    );
    if (!this.playing) this._advance();
  }

  /** Queues a sentence for playback; starts fetching (with retry — see fetchTts()) immediately. */
  enqueueText(text, { voice = this.voice, provider = this.provider } = {}) {
    if (this.stopped || !text) return;
    this.pending++;
    const notifyFailure = () => {
      if (this._failureNotified) return;
      this._failureNotified = true;
      this.onFailure?.({ provider });
    };
    // `text` rides along to _advance() (same shape voice/playback.js's
    // identical queue already carries) so the per-utterance watchdog below
    // can size its timeout to how long this sentence should actually take.
    this.queue.push(fetchTts(text, { voice, provider }, notifyFailure).then((r) => (r ? { ...r, text } : null)));
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
    const { url, blob, text } = result;

    if (this.onStart) this.onStart();

    const audio = new Audio(url);
    this.currentAudio = audio;
    this.currentEnvelope = null; // cleared until buildEnvelope() below resolves — getOutputLevel() reads 0 until then, same as "no envelope" on failure

    // Watchdog against a stalled <audio> element that never fires 'ended'
    // or 'error' and never rejects play() — the exact class of bug
    // browser-speaker.js's identical watchdog exists for (Chrome silently
    // dropping an utterance), ported here because THIS path (Gemini TTS —
    // duplex's own default voice output) had no equivalent at all, found
    // during the state-machine audit: without it, `playing` stays true
    // forever and 'speaking' never exits. Sized per-sentence, like
    // browser-speaker.js's, not per-reply — a long multi-sentence reply
    // must never trip a watchdog sized for the whole thing; a new sentence
    // starting is itself proof of progress.
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
      console.warn('[audio-player] this sentence never confirmed it finished playing — continuing anyway.');
      settle();
    }, Math.min(30000, Math.max(6000, estimatedMs * 2.5)));

    audio.onended = settle;
    audio.onerror = settle;
    audio.play().catch(settle);

    // Decoded from a SEPARATE copy of the same bytes (see fetchTts()'s
    // return shape and this file's header comment) — never gates or touches
    // the `audio.play()` above in any way, including on failure. `blob` can
    // be null here for a clip whose envelope-copy fetch failed (see
    // enqueueClip()) — the orb just shows no reactivity for that one clip,
    // same as any other envelope-build failure.
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

  /**
   * Prepares the player for a fresh reply — used on a model-switch
   * 'restart' (voice-engine.js's documented contract: "clear any partial
   * reply shown/spoken; a fresh one follows"). Previously this only reset
   * flags, leaving the FAILED model's `currentAudio`/queue fully intact —
   * its audio kept playing underneath the replacement's, a real bug found
   * during the state-machine audit (not itself a stuck-state bug, but a
   * violation of this exact documented contract). Reuses stop()'s own halt
   * logic, then immediately un-does the one flag stop() sets that reset()
   * must NOT leave behind — `stopped` — so the replacement reply's own
   * pushText()/end() calls aren't silently dropped right after.
   */
  reset() {
    this.stop();
    this.stopped = false;
    this.streamEnded = false;
    this._buffer = '';
    this._firstFlushDone = false;
    this._failureNotified = false;
  }
}

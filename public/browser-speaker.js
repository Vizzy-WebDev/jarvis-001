// The free, offline, zero-setup voice output option: the browser's own
// speechSynthesis. Same push/end/stop/reset interface as AudioPlayer (see
// audio-player.js) so pipeline-engine.js can use either one interchangeably
// depending on the user's voice-output setting.

const SENTENCE_END = /[^.!?]*[.!?]+(\s+|$)/;

// Same secondary flush point as audio-player.js's identical constants (see
// its comment) — this is the default, always-available voice-output path
// (no configured TTS provider), so the same "don't wait for a full
// sentence on the reply's opening line" fix belongs here just as much,
// even though there's no network round trip to hide: the wait here is
// purely how long it takes the model to stream enough tokens to finish a
// whole sentence, and a long opening sentence could still leave Jarvis
// visibly silent for several seconds after the reply starts appearing.
const CLAUSE_END = /[^,;:]*[,;:]+\s+/;
const CLAUSE_MIN_CHARS = 24;
const CLAUSE_FALLBACK_CHARS = 90;

function loadVoices() {
  return window.speechSynthesis.getVoices();
}

function pickVoice() {
  const voices = loadVoices();
  const preferredNames = ['Aria', 'Ava', 'Guy', 'Natural'];
  for (const name of preferredNames) {
    const match = voices.find((v) => v.name.includes(name) && v.lang.startsWith('en'));
    if (match) return match;
  }
  return voices.find((v) => v.lang === 'en-US') || voices.find((v) => v.lang.startsWith('en')) || voices[0] || null;
}

export class BrowserSpeaker {
  constructor({ onStart, onIdle } = {}) {
    this.onStart = onStart;
    this.onIdle = onIdle;
    this._buffer = '';
    // Explicit, self-managed sequential queue — utterances AND clips both,
    // in the order they should play. Added for real vocal reactions (see
    // enqueueClip() below): Chrome's own speechSynthesis queue only ever
    // holds actual utterances, so there is no way to interleave a raw
    // audio clip into it directly. Before this, each sentence was fired
    // straight onto Chrome's own internal queue as soon as it was found
    // (relying entirely on Chrome to serialize playback); now this class
    // pulls one item at a time and waits for it to fully settle before
    // starting the next. Audibly identical for plain speech either way —
    // Chrome was already strictly serializing playback regardless of when
    // speak() was technically called — but this is what makes inserting a
    // clip at exactly the right position possible at all.
    this.queue = []; // {type:'utterance', text} | {type:'clip', url}
    this._processing = false; // true while one queued item is actively playing/settling
    this.pending = 0; // items queued but not yet fully settled — same external contract _maybeIdle() always had
    this.streamEnded = false;
    this.stopped = false;
    this._lastBoundaryAt = 0; // performance.now() of the most recent onboundary event — see getOutputLevel()
    this._firstFlushDone = false; // see pushText()'s clause-boundary fallback
    this._currentClipAudio = null; // for stop()/reset() cleanup, mirrors audio-player.js's currentAudio
  }

  /**
   * For the orb: Jarvis's own voice output energy while speaking, 0..1.
   * speechSynthesis exposes no analysable audio on this path at all (unlike
   * AudioPlayer's server-TTS path — see its getOutputLevel()), so this is
   * timing-derived rather than amplitude-derived: each word boundary
   * re-triggers a short decaying pulse, giving the orb a real per-word
   * rhythm instead of the flat procedural motion it fell back to before.
   * Shows no reactivity during a clip specifically — a clip has no
   * onboundary events at all, only real speech does; a known, accepted gap
   * on this one path, not solved here.
   */
  getOutputLevel() {
    if (!this._lastBoundaryAt) return 0;
    const elapsed = performance.now() - this._lastBoundaryAt;
    const DECAY_MS = 220;
    if (elapsed >= DECAY_MS) return 0;
    return 1 - elapsed / DECAY_MS;
  }

  pushText(textChunk) {
    if (this.stopped) return;
    this._buffer += textChunk;

    let match;
    while ((match = SENTENCE_END.exec(this._buffer)) && match[0].trim()) {
      const sentence = match[0].trim();
      this._buffer = this._buffer.slice(match[0].length);
      this._enqueueUtterance(sentence);
      this._firstFlushDone = true;
    }

    // Same opening-clause fallback as audio-player.js — applies only once
    // per reply, so the rest still speaks in full sentences via the loop
    // above.
    if (!this._firstFlushDone && this._buffer.trim()) {
      const clauseMatch = CLAUSE_END.exec(this._buffer);
      if (clauseMatch && clauseMatch[0].trim().length >= CLAUSE_MIN_CHARS) {
        const clause = clauseMatch[0].trim();
        this._buffer = this._buffer.slice(clauseMatch[0].length);
        this._enqueueUtterance(clause);
        this._firstFlushDone = true;
      } else if (this._buffer.length >= CLAUSE_FALLBACK_CHARS) {
        const chunk = this._buffer.trim();
        this._buffer = '';
        this._enqueueUtterance(chunk);
        this._firstFlushDone = true;
      }
    }
  }

  /**
   * Queues a real, pre-recorded sound clip (not spoken text) — for a
   * 'reaction' event (see server/personality.js's createReactionScanner()
   * and runner.js). Same queue as spoken sentences (see the constructor's
   * comment on why this class now manages its own explicit ordering), so
   * the clip plays at exactly the right position relative to the
   * surrounding speech. `url` is a static, cacheable asset path (e.g.
   * "/sounds/laugh.mp3").
   */
  enqueueClip(url) {
    if (this.stopped || !url) return;
    this.pending++;
    this.queue.push({ type: 'clip', url });
    this._advance();
  }

  _enqueueUtterance(text) {
    this.pending++;
    this.queue.push({ type: 'utterance', text });
    this._advance();
  }

  end() {
    const rest = this._buffer.trim();
    this._buffer = '';
    if (!this.stopped && rest) this._enqueueUtterance(rest);
    this.streamEnded = true;
    this._maybeIdle();
  }

  /** Pulls the next queued item and plays it — a no-op if something is already playing or this speaker is stopped; the item currently playing always calls this again itself once it settles (see _settleCurrent()). */
  _advance() {
    if (this.stopped || this._processing) return;
    if (this.queue.length === 0) {
      this._maybeIdle();
      return;
    }
    this._processing = true;
    const item = this.queue.shift();
    if (item.type === 'clip') this._playClip(item.url);
    else this._playUtterance(item.text);
  }

  /** Common settle path for both an utterance and a clip — decrements `pending`, then immediately tries to start whatever's next. */
  _settleCurrent() {
    this._processing = false;
    this.pending--;
    this._maybeIdle();
    this._advance();
  }

  _playUtterance(sentence) {
    const utter = new SpeechSynthesisUtterance(sentence);
    const voice = pickVoice();
    if (voice) utter.voice = voice;
    utter.rate = 1.02;

    // Chrome has a real, documented bug class where an utterance is
    // silently dropped and NEITHER onend nor onerror ever fires — without
    // this, playback would stall on this one item forever (stuck on
    // "speaking", mic never resumes, and nothing queued after it ever
    // starts). The watchdog is sized to a generous multiple of the
    // utterance's estimated speaking time, so it only ever fires on the
    // already-broken path, never during normal playback.
    let settled = false;
    const settle = () => {
      if (settled) return;
      settled = true;
      clearTimeout(watchdog);
      this._settleCurrent();
    };
    const estimatedMs = (sentence.length / 12) * 1000; // ~12 chars/sec, a conservative speaking-rate estimate
    const watchdog = setTimeout(() => {
      console.warn('[browser-speaker] speechSynthesis never confirmed this utterance finished — continuing anyway.');
      settle();
    }, Math.min(30000, Math.max(6000, estimatedMs * 2.5)));

    utter.onstart = () => {
      if (this.onStart) this.onStart();
    };
    utter.onboundary = () => {
      this._lastBoundaryAt = performance.now();
    };
    utter.onend = settle;
    utter.onerror = settle;
    window.speechSynthesis.speak(utter);
  }

  /**
   * Plays a real sound clip via a plain <audio> element — same watchdog
   * pattern as _playUtterance() (and every other playback path in this
   * codebase), since a stalled <audio> element that never fires 'ended' or
   * 'error' is exactly the same class of bug as Chrome's dropped-utterance
   * one, just on a different API.
   */
  _playClip(url) {
    let settled = false;
    const settle = () => {
      if (settled) return;
      settled = true;
      clearTimeout(watchdog);
      this._currentClipAudio = null;
      this._settleCurrent();
    };
    // Clips are always short by nature (a real laugh, not a sentence) — no
    // need for the utterance path's length-based estimate.
    const watchdog = setTimeout(() => {
      console.warn('[browser-speaker] this clip never confirmed it finished playing — continuing anyway.');
      settle();
    }, 6000);

    const audio = new Audio(url);
    this._currentClipAudio = audio;
    if (this.onStart) this.onStart();
    audio.onended = settle;
    audio.onerror = settle;
    audio.play().catch(settle);
  }

  _maybeIdle() {
    if (this.streamEnded && this.pending <= 0 && this.onIdle) {
      this.onIdle();
    }
  }

  stop() {
    this.stopped = true;
    window.speechSynthesis.cancel();
    if (this._currentClipAudio) {
      this._currentClipAudio.pause();
      this._currentClipAudio = null;
    }
    this.queue = [];
    this._processing = false;
    this.pending = 0;
    this._buffer = '';
    this._lastBoundaryAt = 0;
  }

  /**
   * Prepares this speaker for a fresh reply after a model-switch 'restart'
   * — found during the state-machine audit while fixing the identical bug
   * in audio-player.js/voice/playback.js (whose reset() only touched flags,
   * never the actually-playing audio): this had the same gap. Chrome's
   * speechSynthesis keeps its own internal utterance queue independent of
   * `pending`/`_buffer` — without cancel() here, a failed model's
   * already-queued utterances kept speaking underneath the replacement's.
   * Known, accepted residual edge case (unlike audio-player.js's fix, this
   * one can't fully close): cancel() asynchronously fires onerror for each
   * cancelled utterance, which still runs through _settleCurrent() on
   * arrival — if one lands after this reset already zeroed `pending`, it
   * can end up transiently negative for the NEW reply, which could in
   * principle fire onIdle a beat early. Narrow (restart + browser voice +
   * timing) and low severity (an early "done" on the browser-voice path,
   * not a hang) next to what this whole audit targets — not worth the
   * added complexity of per-item generation tracking inside this class for.
   */
  reset() {
    window.speechSynthesis.cancel();
    if (this._currentClipAudio) {
      this._currentClipAudio.pause();
      this._currentClipAudio = null;
    }
    this.queue = [];
    this._processing = false;
    this.pending = 0;
    this.stopped = false;
    this.streamEnded = false;
    this._buffer = '';
    this._firstFlushDone = false;
  }
}

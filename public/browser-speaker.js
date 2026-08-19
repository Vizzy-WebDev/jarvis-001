// The free, offline, zero-setup voice output option: the browser's own
// speechSynthesis. Same push/end/stop/reset interface as AudioPlayer (see
// audio-player.js) so pipeline-engine.js can use either one interchangeably
// depending on the user's voice-output setting.

const SENTENCE_END = /[^.!?]*[.!?]+(\s+|$)/;

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
    this.pending = 0;
    this.streamEnded = false;
    this.stopped = false;
    this._lastBoundaryAt = 0; // performance.now() of the most recent onboundary event — see getOutputLevel()
  }

  /**
   * For the orb: Jarvis's own voice output energy while speaking, 0..1.
   * speechSynthesis exposes no analysable audio on this path at all (unlike
   * AudioPlayer's Gemini-voice path — see its getOutputLevel()), so this is
   * timing-derived rather than amplitude-derived: each word boundary
   * re-triggers a short decaying pulse, giving the orb a real per-word
   * rhythm instead of the flat procedural motion it fell back to before.
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
      this._speak(sentence);
    }
  }

  end() {
    const rest = this._buffer.trim();
    this._buffer = '';
    if (!this.stopped && rest) this._speak(rest);
    this.streamEnded = true;
    this._maybeIdle();
  }

  _speak(sentence) {
    this.pending++;
    const utter = new SpeechSynthesisUtterance(sentence);
    const voice = pickVoice();
    if (voice) utter.voice = voice;
    utter.rate = 1.02;

    // Chrome has a real, documented bug class where an utterance is
    // silently dropped and NEITHER onend nor onerror ever fires — without
    // this, `pending` would never decrement and the turn hangs forever
    // (stuck on "speaking", mic never resumes). The watchdog is sized to a
    // generous multiple of the utterance's estimated speaking time, so it
    // only ever fires on the already-broken path, never during normal
    // playback.
    let settled = false;
    const settle = () => {
      if (settled) return;
      settled = true;
      clearTimeout(watchdog);
      this.pending--;
      this._maybeIdle();
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

  _maybeIdle() {
    if (this.streamEnded && this.pending <= 0 && this.onIdle) {
      this.onIdle();
    }
  }

  stop() {
    this.stopped = true;
    window.speechSynthesis.cancel();
    this.pending = 0;
    this._buffer = '';
    this._lastBoundaryAt = 0;
  }

  reset() {
    this.stopped = false;
    this.streamEnded = false;
    this._buffer = '';
  }
}

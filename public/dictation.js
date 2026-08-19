// The composer's own dictation mic — separate from the voice-control session
// in engines/pipeline-engine.js and engines/live-engine.js. This is purely
// "speak instead of typing": words appear live in the composer's textarea as
// you talk, and you either send them directly or stop and edit first. It
// never sends anything on its own and never talks to the server.
//
// Mutual exclusion with voice control matters: Chrome only reliably runs one
// SpeechRecognition session at a time, so app.js stops whichever one isn't
// being used before starting the other (see app.js's onMicButtonClick and
// the composer mic's click handler).

import { MicLevelMonitor } from './turn-detector.js';

const SpeechRecognitionCtor = window.SpeechRecognition || window.webkitSpeechRecognition;

export class Dictation {
  constructor({ onText, onState } = {}) {
    this.onText = onText; // (fullText) => void — called live as words arrive
    this.onState = onState; // ('idle'|'listening') => void
    this.recognition = null;
    this.stream = null;
    this.micMonitor = null;
    this._active = false;
    this.baseText = '';
    this.pendingFinal = '';
  }

  get active() {
    return this._active;
  }

  get supported() {
    return Boolean(SpeechRecognitionCtor);
  }

  /** getLevel() for the orb — 0..1, mic energy while dictating. */
  getLevel() {
    return this.micMonitor?.getLevel() ?? 0;
  }

  async start(baseText = '') {
    if (this._active || !this.supported) return;
    this.baseText = baseText ? `${baseText} ` : '';
    this.pendingFinal = '';

    try {
      this.stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      });
      this.micMonitor = new MicLevelMonitor(this.stream);
    } catch {
      // Dictation can still work without the level monitor (the orb just
      // won't react to it) — recognition itself opens its own capture.
      this.stream = null;
      this.micMonitor = null;
    }

    this._active = true;
    this._startRecognition();
    this.onState?.('listening');
  }

  stop() {
    if (!this._active) return;
    this._active = false;
    if (this.recognition) {
      this.recognition.onend = null; // don't auto-restart once we're intentionally stopping
      try {
        this.recognition.stop();
      } catch {
        // already stopped
      }
      this.recognition = null;
    }
    if (this.stream) {
      this.stream.getTracks().forEach((t) => t.stop());
      this.stream = null;
    }
    if (this.micMonitor) {
      this.micMonitor.close();
      this.micMonitor = null;
    }
    this.onState?.('idle');
  }

  _startRecognition() {
    const rec = new SpeechRecognitionCtor();
    rec.lang = 'en-US';
    rec.continuous = true;
    rec.interimResults = true;

    rec.onresult = (event) => {
      let interim = '';
      let final = '';
      for (let i = event.resultIndex; i < event.results.length; i++) {
        const t = event.results[i][0].transcript;
        if (event.results[i].isFinal) final += t;
        else interim += t;
      }
      if (final) this.pendingFinal += final;
      this.onText?.(`${this.baseText}${this.pendingFinal}${interim}`);
    };

    rec.onerror = (event) => {
      if (event.error === 'no-speech' || event.error === 'aborted') return; // expected in continuous mode
      this.stop();
    };

    rec.onend = () => {
      // Chrome ends a long continuous session on its own; restart seamlessly
      // if the user hasn't stopped dictating.
      if (this._active) {
        try {
          rec.start();
        } catch {
          // a start() is already pending — ignore
        }
      }
    };

    rec.start();
    this.recognition = rec;
  }
}

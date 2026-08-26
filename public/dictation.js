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
//
// Self-hearing guard: dictation and voice control share ONE physical
// speaker. app.js's click handler is the PRIMARY defense — it stops or
// interrupts whatever Jarvis is doing before ever calling start() here (see
// that file's dictation click handler) — but `isJarvisSpeaking` is a real
// backstop, not decoration: without it, a race (start() called a moment
// before playback actually stops, or Jarvis starting to speak for an
// unrelated reason — a scheduled task's own spoken notification — while
// dictation is already open) would transcribe Jarvis's own voice straight
// into the composer with nothing to catch it. Same shape as
// duplex-engine.js's browser-fallback path, which guards its own onresult
// the identical way for the identical reason (Chrome's SpeechRecognition
// opens its own capture no `echoCancellation` option ever reaches).
//
// No MicLevelMonitor here — dictation used to build its own purely to feed
// the orb (`getLevel()`), which coupled two unrelated things: what the orb
// shows should reflect Jarvis's own state, not whichever UI control happens
// to be focused. The composer mic button already has its own real,
// independent "I'm listening" indicator (`.dictating`'s CSS pulse
// animation, style.css) — nothing was lost by removing this.

const SpeechRecognitionCtor = window.SpeechRecognition || window.webkitSpeechRecognition;

export class Dictation {
  constructor({ onText, onState, isJarvisSpeaking } = {}) {
    this.onText = onText; // (fullText) => void — called live as words arrive
    this.onState = onState; // ('idle'|'listening') => void
    this.isJarvisSpeaking = isJarvisSpeaking || (() => false); // see this file's header comment
    this.recognition = null;
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

  start(baseText = '') {
    if (this._active || !this.supported) return;
    if (this.isJarvisSpeaking()) return; // see this file's header comment — the caller is expected to have already interrupted Jarvis; this is the backstop for when that hasn't actually taken effect yet
    this.baseText = baseText ? `${baseText} ` : '';
    this.pendingFinal = '';

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
    // Chrome's SpeechRecognition.stop() (unlike abort()) finalizes and
    // delivers whatever audio it already captured — a trailing onresult
    // predictably fires AFTER this method returns (the onresult guard
    // below is what actually drops it; this reset is belt-and-suspenders
    // so a future gap in that guard can't silently resurrect stale text
    // through baseText/pendingFinal surviving across sessions).
    this.baseText = '';
    this.pendingFinal = '';
    this.onState?.('idle');
  }

  /**
   * Re-bases dictation onto text the user just edited by hand — called from
   * app.js's own #text-input 'input' listener while dictation is active.
   * Setting textarea.value programmatically (this class's own onText
   * callback) never fires a real 'input' event in a browser, only genuine
   * user typing/pasting does — so this is only ever reached for an actual
   * manual edit, no re-entrancy flag needed to tell the two apart. Fresh
   * speech always lands after wherever the edit left off; an edit made in
   * the MIDDLE of the existing text doesn't retroactively get inserted
   * there — a disclosed trade-off, not a hidden one (see this file's header
   * comment / the project plan).
   */
  rebase(text) {
    if (!this._active) return;
    const trimmed = String(text || '').replace(/\s+$/, '');
    this.baseText = trimmed ? `${trimmed} ` : '';
    this.pendingFinal = '';
  }

  _startRecognition() {
    const rec = new SpeechRecognitionCtor();
    rec.lang = 'en-US';
    rec.continuous = true;
    rec.interimResults = true;

    rec.onresult = (event) => {
      // Chrome's stop() (as opposed to abort()) finalizes and delivers
      // whatever audio it already captured, so a trailing onresult reliably
      // arrives AFTER stop() has already run and _active is already false —
      // this guard is what actually drops it (stop()'s own baseText/
      // pendingFinal reset above is the belt-and-suspenders half). Without
      // this, the trailing result reconstructed the FULL accumulated
      // utterance and wrote it straight back into a textarea the user had
      // just watched clear on Send — a real, reported bug.
      if (!this._active) return;
      // See this file's header comment — the primary defense is app.js
      // never calling start() while Jarvis is speaking; this catches the
      // remaining case, Jarvis starting to speak for an unrelated reason
      // (e.g. a scheduled task's own notification) while dictation was
      // already open.
      if (this.isJarvisSpeaking()) return;
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

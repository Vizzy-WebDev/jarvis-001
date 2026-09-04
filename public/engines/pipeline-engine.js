// Engine A — works with any AI model (Gemini, Claude, or OpenAI — whichever
// is active on the server). Speech-to-text happens in the browser (Chrome's
// Web Speech API), the reply streams from /api/chat/stream, and speech
// output is either the browser's free built-in voice or Gemini TTS,
// depending on the user's setting. Turn-taking and barge-in are handled
// here (see ../turn-detector.js for the reasoning).
//
// Expected latency: roughly 0.8-1.5s to first words, since text has to
// finish streaming from the model before the (sentence-chunked) speech can
// start. See engines/live-engine.js (Phase 3) for the faster, Gemini-only
// alternative.

import { VoiceEngine } from './voice-engine.js';
import { computeWaitMs, isCompleteThought, MicLevelMonitor, SilenceWatcher } from '../turn-detector.js';
import { AudioPlayer } from '../audio-player.js';
import { BrowserSpeaker } from '../browser-speaker.js';
import { REACTION_SOUNDS } from '../reaction-sounds.js';

const SpeechRecognitionCtor = window.SpeechRecognition || window.webkitSpeechRecognition;

// How long after Jarvis's audio physically stops before recognition resumes.
// Chrome's cloud ASR runs roughly 300-800ms behind real time, so audio
// captured *during* playback can otherwise surface as a transcript *after*
// the speaking guard has already cleared — this margin absorbs that lag
// instead of it becoming a phantom "user" turn the instant Jarvis finishes.
const ECHO_TAIL_MS = 700;
// Barge-in sampling: read mic energy on a fixed clock (not "whenever ASR
// happens to emit a result", which let two unrelated loud instants look like
// one continuous utterance). Fixed threshold, no floor-learning warm-up —
// an earlier version of this spent ~300ms learning the room's echo floor
// before it would even arm, which roughly doubled how long it took to
// interrupt Jarvis compared to the original feel. BARGE_FLOOR is already
// well above ordinary residual echo (turn-detector.js's isSpeaking() default
// is 0.02); that's protection enough without adding latency.
const BARGE_SAMPLE_MS = 100;
const BARGE_SUSTAIN_MS = 250;
const BARGE_FLOOR = 0.05;

// How long a hands-free ("conversation mode") session can sit quietly in
// 'listening' before dropping to a real 'idle' — mic stays fully open the
// whole time; only the STATE (and the orb's honesty about it) changes. Same
// constant/value as engines/duplex-engine.js's own HANDS_FREE_IDLE_MS, which
// already has this fix — this engine (the DEFAULT one) never got it, so it
// showed 'listening' indefinitely for as long as a session was active. See
// _armIdleTimer()/_disarmIdleTimer() below.
const HANDS_FREE_IDLE_MS = 30000;

export class PipelineEngine extends VoiceEngine {
  constructor(opts = {}) {
    super();
    // 'browser' (the free, offline speechSynthesis voice) or a configured
    // TTS provider's ref (e.g. 'elevenlabs', server/tts/index.js's registry)
    // — the ref IS the provider id, passed straight through to AudioPlayer;
    // there is no longer a separate "which voice" concept on the client at
    // all (a provider resolves its own voice server-side, e.g. from
    // external-services.js's optional extra field) now that Gemini (whose
    // voice names this used to thread through as `geminiVoice`) is gone.
    this.voiceOutput = opts.voiceOutput || 'browser';
    this.useAiTurnCheck = Boolean(opts.useAiTurnCheck);

    this.recognition = null;
    this.stream = null;
    this.micMonitor = null;
    this.silenceWatcher = null; // SilenceWatcher — the real "has the user gone quiet" arm, created alongside micMonitor in start()
    this.active = false;
    this.pendingUtterance = '';
    this.pendingConfidence = null; // lowest confidence seen across this utterance's final chunks, for the voice-clarity guard
    this.silenceTimer = null; // short one-off re-check timer used only inside _maybeFinalize() — see that method
    this.currentEventSource = null;
    this.speaker = null;
    this._isSpeaking = false;
    this._speakingBuffer = ''; // text of the reply currently playing, for the self-echo filter
    this._recSuspended = false; // true only while Jarvis's audio is actually playing + its echo tail
    this._echoTailTimer = null;
    this._bargeTimer = null; // fixed-rate mic-energy sampler while speaking
    this._idleTimer = null; // real hands-free idle countdown — see HANDS_FREE_IDLE_MS
    this._finalizeInFlight = false; // guards against _maybeFinalize() being entered twice for overlapping speech — see that method's own comment

    // Generation token for the current speaker (AudioPlayer/BrowserSpeaker)
    // — found necessary during a full state-machine audit: neither
    // speaker's onStart/onIdle callback used to check "am I still the
    // current one?", so a callback from an already-discarded speaker could
    // still mutate LIVE engine state. Bumped in _send() (new speaker) AND
    // interrupt() (old one discarded, even before any new one exists).
    this._speakerGen = 0;
  }

  updateOptions(opts = {}) {
    if (opts.voiceOutput) this.voiceOutput = opts.voiceOutput;
    if ('useAiTurnCheck' in opts) this.useAiTurnCheck = Boolean(opts.useAiTurnCheck);
  }

  /**
   * Mute/unmute: disables microphone capture ONLY — never touches `speaker`,
   * `currentEventSource`, `state`, or anything already captured. Composes
   * with `_recSuspended` (the self-listening fix's "Jarvis is talking" gate)
   * via `_shouldListen()`/`_syncRecognitionState()` rather than fighting it:
   * muting while Jarvis is speaking is a no-op on the recognizer itself
   * (already suspended) but is remembered so the echo-tail resume doesn't
   * turn it back on until unmuted; muting while listening or thinking stops
   * it immediately.
   *
   * Deliberately does NOT touch `pendingUtterance`/`silenceTimer` — an
   * earlier version cleared them here, reasoning "nothing spoken in the
   * instant before muting should be sendable once unmuted." That was too
   * blunt: it also discarded an utterance that was ALREADY complete and
   * simply waiting out its natural silence-detection pause before being
   * sent — muting right after finishing a sentence silently ate the turn,
   * which looked exactly like "the conversation stopped." `_onResult()`'s
   * own `this.muted` guard already fully prevents any NEW speech from ever
   * entering `pendingUtterance` while muted (recognition is stopped), so
   * the only thing that can be pending here is something said before
   * muting — which should still go through.
   */
  setMuted(muted) {
    muted = Boolean(muted);
    if (this.muted === muted) return;
    this.muted = muted;
    this._syncRecognitionState();
  }

  /** True exactly when the recognizer should be capturing right now. */
  _shouldListen() {
    return this.active && !this._recSuspended && !this.muted;
  }

  /** Starts or stops the recognizer to match _shouldListen(), idempotently (safe to call redundantly). */
  _syncRecognitionState() {
    if (!this.recognition) return;
    if (this._shouldListen()) {
      try {
        this.recognition.start();
      } catch {
        // already running, or a start() is already pending — ignore
      }
    } else {
      try {
        this.recognition.stop();
      } catch {
        // already stopped
      }
    }
  }

  /** For the orb: mic energy while listening, 0..1. */
  getMicLevel() {
    return this.micMonitor?.getLevel() ?? 0;
  }

  /**
   * For the orb: Jarvis's own voice output energy while speaking, 0..1.
   * Delegates to whichever speaker is active — AudioPlayer reads an
   * offline-decoded envelope of a separate copy of the playing clip's bytes
   * (see voice-envelope.js and audio-player.js's header comment on why the
   * real playback path itself is never touched for this); BrowserSpeaker
   * derives a per-word pulse from speechSynthesis's own boundary timing,
   * since that path exposes no analysable audio at all. Falls back to 0
   * (procedural motion) if `speaker` isn't set yet, or is some future kind
   * without its own getOutputLevel().
   */
  getOutputLevel() {
    return this.speaker?.getOutputLevel?.() ?? 0;
  }

  async start() {
    if (this.active) return;
    if (!SpeechRecognitionCtor) {
      this._emit('error', { message: 'Voice input needs Chrome or Edge.' });
      return;
    }

    try {
      this.stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      });
    } catch {
      this._emit('error', {
        message: 'Microphone access was blocked. Allow the microphone in your browser and try again.',
      });
      return;
    }

    this.active = true;
    this.micMonitor = new MicLevelMonitor(this.stream);
    this.silenceWatcher = new SilenceWatcher(this.micMonitor);
    this._startRecognition();
    this._setState('listening');
    this._armIdleTimer();
  }

  stop() {
    this.active = false;
    this.muted = false; // a freshly started/restarted session always begins unmuted
    this._disarmIdleTimer();
    clearTimeout(this.silenceTimer);
    this.silenceWatcher?.cancel();
    clearTimeout(this._echoTailTimer);
    clearInterval(this._bargeTimer);
    this.pendingUtterance = '';
    this.interrupt({ keepListening: true }); // also silence any reply in progress

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
    this.silenceWatcher = null;
    this._setState('idle');
  }

  sendText(text, { attachments = [] } = {}) {
    clearTimeout(this.silenceTimer);
    this.silenceWatcher?.cancel();
    this.pendingUtterance = '';
    // Typed text can't be misheard, so it's always full-confidence — the
    // voice-clarity guard only ever gates spoken input.
    this._send(String(text || '').trim(), { source: 'text', attachments });
  }

  /**
   * Barge-in: stop Jarvis mid-reply.
   * `keepListening` is for internal use (a fresh turn is about to start, so
   * skip the visual flicker to 'listening' right before 'thinking').
   * `resumeRecognition` is false only for _send()'s own internal call, which
   * is immediately followed by re-suspending for the new turn anyway.
   */
  interrupt({ keepListening = false, resumeRecognition = true } = {}) {
    // Invalidates any in-flight onStart/onIdle from the speaker about to be
    // discarded below, even before a NEW one exists — see _speakerGen's
    // constructor comment. Also a backstop disarm of the 'thinking' hang
    // watchdog (its primary disarm points are _onSpeechStart() and _send()'s
    // own terminal branches; every call to interrupt() — a real barge-in,
    // _send()'s defensive pre-turn clear, or stop() — should leave nothing
    // stale armed regardless of which path got here).
    this._speakerGen++;
    this._disarmStuckWatchdog();
    if (this.speaker) {
      this.speaker.stop();
      this.speaker = null;
    }
    if (this.currentEventSource) {
      this.currentEventSource.close();
      this.currentEventSource = null;
    }
    this._isSpeaking = false;
    this._speakingBuffer = '';
    clearInterval(this._bargeTimer);
    this._bargeTimer = null;
    // The utterance (real or echoed) that triggered this interrupt must not
    // be finalized and sent a moment later — this was a real bug: the old
    // version left pendingUtterance/silenceTimer alone here, so a barge-in
    // caused by Jarvis's own echo would clear the echo detector's buffer
    // (below) and then still send the echo as the next turn.
    clearTimeout(this.silenceTimer);
    this.silenceWatcher?.cancel();
    this.pendingUtterance = '';
    this.pendingConfidence = null;
    this._finalizeInFlight = false; // a barge-in or fresh turn starting mid-finalize must not leave this wedged on
    if (resumeRecognition) this._resumeRecognition();
    if (!keepListening) {
      this._backToListening();
    }
  }

  /**
   * The one place every "a turn just ended, go back to resting" transition
   * lands — same helper duplex-engine.js already has. `active` false means
   * the session was fully stopped (mic released) rather than merely quiet,
   * so that still goes straight to 'idle' with no timer involved.
   */
  _backToListening() {
    if (this.active) {
      this._setState('listening');
      this._armIdleTimer();
    } else {
      this._disarmIdleTimer();
      this._setState('idle');
    }
  }

  /** (Re)starts the hands-free quiet-period countdown — see HANDS_FREE_IDLE_MS. */
  _armIdleTimer() {
    clearTimeout(this._idleTimer);
    this._idleTimer = setTimeout(() => {
      // Only actually drop to idle if still genuinely resting in
      // 'listening' — if something else happened in the meantime
      // (thinking/speaking), this timer is stale and does nothing; the
      // state change that caused that already disarmed it (see
      // _onSpeechStart()/_send()), so reaching here with a mismatched state
      // shouldn't normally happen, but the check costs nothing.
      if (this.state === 'listening') this._setState('idle');
    }, HANDS_FREE_IDLE_MS);
  }

  _disarmIdleTimer() {
    clearTimeout(this._idleTimer);
    this._idleTimer = null;
  }

  // ---------- speech recognition (continuous "conversation mode") ----------

  _startRecognition() {
    const rec = new SpeechRecognitionCtor();
    rec.lang = 'en-US';
    rec.continuous = true;
    rec.interimResults = true;

    rec.onresult = (event) => this._onResult(event);
    rec.onerror = (event) => this._onRecognitionError(event);
    rec.onend = () => {
      // Chrome sometimes ends a long continuous session on its own; restart
      // seamlessly only if it should currently be listening — session
      // active, not suspended for Jarvis's own audio (the self-listening
      // fix), and not muted.
      if (this._shouldListen()) {
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

  /** Stops recognition entirely for the duration Jarvis is talking — an echo can then never become text. */
  _suspendRecognition() {
    clearTimeout(this._echoTailTimer);
    this._echoTailTimer = null;
    if (this._recSuspended) return;
    this._recSuspended = true;
    this._syncRecognitionState();
  }

  _resumeRecognition() {
    clearTimeout(this._echoTailTimer);
    this._echoTailTimer = null;
    if (!this._recSuspended) return;
    this._recSuspended = false;
    this._syncRecognitionState();
  }

  _onRecognitionError(event) {
    if (event.error === 'no-speech' || event.error === 'aborted') return; // expected in continuous mode
    if (event.error === 'not-allowed' || event.error === 'service-not-allowed') {
      this._emit('error', { message: 'Microphone access was blocked. Allow the microphone and try again.' });
      this.stop();
    } else if (event.error === 'network') {
      this._emit('error', { message: 'Speech recognition needs an internet connection.' });
    } else {
      this._emit('error', { message: `Microphone problem: ${event.error}` });
    }
  }

  _onResult(event) {
    // Recognition is stopped for the whole "Jarvis is talking" window (see
    // _suspendRecognition), and while muted, but a result already in
    // Chrome's pipeline when stop() was called can still arrive once more —
    // belt and braces so it can never be treated as something the user said.
    if (this._recSuspended || this._isSpeaking || this.muted) return;

    // Real listening activity — wake from a real hands-free idle if needed
    // (mic was never actually off, only the displayed state was resting)
    // and reset the quiet countdown. Same pattern as
    // duplex-engine.js's _noteListeningActivity().
    if (this.state === 'idle' && this.active) this._setState('listening');
    this._armIdleTimer();

    let interimText = '';
    let finalText = '';
    for (let i = event.resultIndex; i < event.results.length; i++) {
      const result = event.results[i];
      const t = result[0].transcript;
      if (result.isFinal) {
        finalText += t;
        // Chrome's confidence is only meaningful on final results. Keep the
        // lowest seen across this utterance — the conservative choice, so a
        // single garbled chunk in an otherwise-clear sentence still trips
        // the voice-clarity guard for quick actions (see server/clarify.js).
        const conf = result[0].confidence;
        if (typeof conf === 'number' && !Number.isNaN(conf)) {
          this.pendingConfidence = this.pendingConfidence === null ? conf : Math.min(this.pendingConfidence, conf);
        }
      } else {
        interimText += t;
      }
    }
    if (finalText) this.pendingUtterance += finalText;

    const previewText = `${this.pendingUtterance} ${interimText}`.trim();
    this._emit('transcript', { text: previewText, final: false });

    // Barge-in itself now runs on a fixed-rate mic-energy sampler (see
    // _onSpeechStart/_sampleBargeIn) rather than here — this used to fire
    // from whatever cadence Chrome happened to emit results at, which is not
    // reachable while speaking anyway now that recognition is suspended.

    // Arm/rearm the real-silence watcher (turn-detector.js's SilenceWatcher)
    // rather than a plain timer keyed off "time since this event" — see that
    // class's header comment for why. computeWaitMs() still picks the wait
    // DURATION from how the sentence trails off; what changed is that the
    // duration is now measured against genuine continuous mic silence
    // instead of wall-clock time since Chrome's last emitted result.
    const waitMs = computeWaitMs(previewText);
    this.silenceWatcher.arm(waitMs, () => this._maybeFinalize(previewText));
  }

  /**
   * Last-resort defense on the send path: if what's about to be sent still
   * looks like it came from Jarvis's own reply, drop it instead of sending.
   * Normalized (punctuation/case-insensitive) token-overlap comparison
   * rather than a raw substring test, which any punctuation or ASR slip
   * defeated. Not load-bearing any more — _speakingBuffer is normally
   * already cleared by the time this could run, since recognition is fully
   * suspended for the whole speaking window — but cheap insurance.
   */
  _looksLikeEcho(text) {
    if (!text || !this._speakingBuffer) return false;
    const normalize = (s) =>
      s
        .toLowerCase()
        .replace(/[.,!?;:'"()\-]/g, '')
        .replace(/\s+/g, ' ')
        .trim();
    const t = normalize(text);
    if (t.length < 4) return false;
    const buf = normalize(this._speakingBuffer);
    if (buf.includes(t)) return true;
    const tTokens = t.split(' ').filter(Boolean);
    if (tTokens.length < 2) return false;
    const bufTokens = new Set(buf.split(' ').filter(Boolean));
    const overlap = tTokens.filter((w) => bufTokens.has(w)).length;
    return overlap / tTokens.length >= 0.75;
  }

  async _maybeFinalize(text) {
    // Jarvis is talking, or its echo tail hasn't cleared yet — never
    // finalize anything while either is true. This is the guard the
    // original code was missing entirely: it let an echo become a "final"
    // transcript and sent it as the next turn.
    //
    // Deliberately NOT gated on `this.muted` — an utterance reaching here
    // was fully captured before muting took effect (_onResult() already
    // refuses to add anything new to pendingUtterance while muted), so it
    // should still be sent. Muting stops future listening, not delivery of
    // what you already said.
    if (this._isSpeaking || this._recSuspended) return;

    const trimmed = text.trim();
    if (!trimmed) return;

    // Real, confirmed race: this function has two independent entry points
    // for overlapping speech — turn-detector.js's SilenceWatcher (re-armed
    // fresh on every onresult) and this function's OWN retry timers just
    // below. With no guard, two near-simultaneous triggers could each
    // independently reach _send() for what was really the same utterance —
    // confirmed live: 331 real cases of a user message with zero assistant
    // reply before the next message, the clearest a near-identical
    // utterance re-sent 318ms later. `_finalizeInFlight` is checked once,
    // here, at the top of every entry — a second, independently-triggered
    // call sees it already set and bails immediately rather than competing
    // with the attempt already in progress. The two retry branches below
    // are NOT blocked by this — they're the same logical attempt
    // continuing — so each clears the flag itself right before its own
    // scheduled re-entry, not on every early return.
    if (this._finalizeInFlight) return;
    this._finalizeInFlight = true;

    // Still actually speaking (energy-wise), even though recognition paused
    // between words — don't cut in. Only reachable once Jarvis has actually
    // stopped talking (guard above) — previously this could defer an echo
    // until the exact moment playback ended and then send it immediately.
    if (this.micMonitor?.isSpeaking()) {
      this.silenceTimer = setTimeout(() => {
        this._finalizeInFlight = false; // this call's own continuation, not a new competing attempt
        this._maybeFinalize(text);
      }, 400);
      return;
    }

    if (this._looksLikeEcho(trimmed)) {
      this.pendingUtterance = '';
      this._finalizeInFlight = false;
      return;
    }

    if (this.useAiTurnCheck) {
      const complete = await isCompleteThought(trimmed);
      if (!complete) {
        this.silenceTimer = setTimeout(() => {
          this._finalizeInFlight = false; // this call's own continuation, not a new competing attempt
          this._maybeFinalize(trimmed);
        }, 800);
        return;
      }
    }

    this.pendingUtterance = '';
    const confidence = this.pendingConfidence;
    this.pendingConfidence = null;
    this._finalizeInFlight = false;
    this._emit('transcript', { text: trimmed, final: true });
    this._send(trimmed, { confidence, source: 'voice' });
  }

  // ---------- sending a turn and streaming the reply ----------

  _send(text, opts = {}) {
    const attachments = opts.attachments || [];
    // A file shared with no words is a normal turn, so only bail when there's
    // genuinely nothing to send.
    if (!text && !attachments.length) return;
    this.interrupt({ keepListening: true }); // clear anything left over from a previous turn
    this._disarmIdleTimer(); // leaving the listening family — nothing left to time out until _backToListening() re-arms it
    this._setState('thinking');
    // Backstop against a hung-but-open EventSource (no chunk, no error, no
    // close — just silence) leaving the engine stuck in 'thinking' forever
    // — found during the state-machine audit. Re-armed on every
    // chunk/tool_start/tool_result/restart below so a genuinely slow but
    // actively-responding model never trips it; disarmed the moment a reply
    // actually starts being spoken (its own, better-scoped per-sentence
    // watchdog takes over — see audio-player.js/browser-speaker.js) or the
    // turn ends any other way.
    this._armStuckWatchdog(() => this._recoverFromStuckState());
    // Recognition deliberately stays LIVE through "thinking" — nothing is
    // playing yet, so there's no echo risk, and this is what lets you keep
    // talking or change your mind before Jarvis has said a word (the
    // original conversational feel). It's suspended only once actual audio
    // starts — see _onSpeechStart — which is the narrowest window that
    // still fixes the self-listening bug.

    let fullText = '';
    // See _speakerGen's own constructor comment — onStart/onIdle only act
    // if this is still the CURRENT speaker generation by the time they
    // fire, so a stale callback from a discarded speaker is a safe no-op.
    const gen = ++this._speakerGen;
    const onStart = () => {
      if (gen === this._speakerGen) this._onSpeechStart();
    };
    const onIdle = () => {
      if (gen === this._speakerGen) this._onSpeechIdle();
    };
    // 'browser' is the one special value (the free, offline speechSynthesis
    // voice) — anything else is a configured TTS provider's ref, passed
    // straight through to AudioPlayer, which resolves its own voice
    // server-side. Same convention duplex-engine.js's _makeSpeaker() uses.
    // onFailure fires once per reply, only once EVERY retry for a sentence
    // has already failed (see audio-player.js's fetchTts()) — before this
    // existed, a broken/expired TTS provider just dropped every sentence in
    // silence: a console.warn nobody but a developer would see, and Jarvis
    // never actually speaking, with nothing in the UI to say why. Emits
    // 'tts_failure', NOT 'error' — 'error' tells app.js the whole turn ended
    // (it clears the in-progress assistant bubble), which this isn't: the
    // reply itself is still generating/rendering fine, only its audio failed.
    const onFailure = ({ provider }) => {
      this._emit('tts_failure', { provider });
    };
    this.speaker =
      this.voiceOutput === 'browser'
        ? new BrowserSpeaker({ onStart, onIdle })
        : new AudioPlayer({ onStart, onIdle, onFailure, provider: this.voiceOutput });

    const params = new URLSearchParams({ message: text, source: opts.source || 'voice' });
    if (typeof opts.confidence === 'number') params.set('confidence', String(opts.confidence));
    // Upload ids, not paths — the server re-validates each one (uploads.js's
    // getUpload). EventSource can only issue a GET with no body, which is why
    // these ride in the query string.
    if (attachments.length) params.set('attachments', attachments.join(','));
    const es = new EventSource(`/api/chat/stream?${params.toString()}`);
    this.currentEventSource = es;

    es.onmessage = (ev) => {
      let data;
      try {
        data = JSON.parse(ev.data);
      } catch {
        return;
      }

      if (data.type === 'chunk') {
        // The thinking hang watchdog has no job once genuinely speaking —
        // a REAL, confirmed bug (reproduced with a standalone timer test,
        // not something needing real audio hardware to verify): a chunk
        // for a LATER sentence arriving while an EARLIER one is still
        // playing used to re-arm this 45s timer unconditionally, with
        // nothing left to disarm it again until 'done' — if that gap ever
        // exceeded 45s, it fired and force-interrupted audio that was
        // playing completely normally. This was the actual root cause of
        // a reported "replies cut off mid-sentence, not from me
        // interrupting" bug.
        if (this.state !== 'speaking') this._armStuckWatchdog(() => this._recoverFromStuckState());
        fullText += data.text;
        this._speakingBuffer += data.text;
        this._emit('chunk', { text: data.text });
        this.speaker?.pushText(data.text);
      } else if (data.type === 'tool_start') {
        if (this.state !== 'speaking') this._armStuckWatchdog(() => this._recoverFromStuckState());
        this._emit('tool', { name: data.name });
      } else if (data.type === 'tool_result') {
        if (this.state !== 'speaking') this._armStuckWatchdog(() => this._recoverFromStuckState()); // forward progress, same reasoning as 'chunk'
        this._emit('tool_result', data);
      } else if (data.type === 'model_switch') {
        // Same reasoning as 'chunk'/'tool_start'/'tool_result' above — a
        // candidate model failing over to the next one is real forward
        // progress on the server side, but this was the one event type that
        // used to NOT re-arm the watchdog at all (its sibling 'restart'
        // did) — walking a few failed candidates in server/models/runner.js
        // (each with its own 20s "first token" budget) could silently burn
        // past the 45s "stuck" timer with nothing telling the browser
        // anything was still happening.
        if (this.state !== 'speaking') this._armStuckWatchdog(() => this._recoverFromStuckState());
        this._emit('model_switch', data);
      } else if (data.type === 'progress') {
        // A real, typed "still working" heartbeat from the server (see
        // server.js's /api/chat/stream) — covers the case where NEITHER a
        // chunk, tool event, nor model_switch has happened in a while but
        // the server is still genuinely working (e.g. mid-tool-call, or
        // between two candidate models' first-token windows). No event of
        // its own needed here — arming the watchdog again is the whole job.
        if (this.state !== 'speaking') this._armStuckWatchdog(() => this._recoverFromStuckState());
      } else if (data.type === 'style_floors') {
        this._emit('style_floors', data);
      } else if (data.type === 'reaction') {
        // Real, non-verbal vocal cue (see server/personality.js's
        // createReactionScanner()) — queued into the SAME speaker used for
        // spoken text (works for either BrowserSpeaker or AudioPlayer,
        // both share enqueueClip() now), so it plays at exactly the right
        // position relative to the surrounding speech rather than racing
        // whatever's already queued.
        this.speaker?.enqueueClip(REACTION_SOUNDS[data.kind]);
      } else if (data.type === 'restart') {
        // Same gating as 'chunk'/'tool_start'/'tool_result' above, and the
        // exact same real bug: a model-switch mid-turn can land here while
        // an EARLIER sentence is still genuinely playing (audio for
        // sentence 1 succeeded and started; generating sentence 2 failed
        // and triggered this restart) — this branch was the one place that
        // still re-armed unconditionally, missed in the original fix.
        // Found by re-reading this file specifically to check whether that
        // fix was complete, per a follow-up report of ElevenLabs replies
        // still occasionally cutting off after it shipped.
        if (this.state !== 'speaking') this._armStuckWatchdog(() => this._recoverFromStuckState()); // a fresh attempt starting is renewed progress, not a stall
        // The model that was mid-reply failed — clear what's shown/spoken
        // so far; a fresh reply from the next model follows on the same
        // stream. See server/models/runner.js's header comment for why a
        // clean restart beats stitching two models' text together.
        fullText = '';
        this._speakingBuffer = '';
        this.speaker?.reset();
        this._emit('restart', {});
      } else if (data.type === 'paused') {
        this._disarmStuckWatchdog(); // a real, legitimate end to this turn — not a hang
        this.speaker?.end();
        es.close();
        if (this.currentEventSource === es) this.currentEventSource = null;
        // If speaking never actually started on this turn, _onSpeechIdle's
        // tail-then-resume will never run — resume directly, or the mic
        // would stay suspended indefinitely. If speaking DID start, leave it
        // to _onSpeechIdle so the echo tail is still respected.
        if (!this._isSpeaking) this._resumeRecognition();
        this._emit('paused', { reason: data.reason });
        this._backToListening();
      } else if (data.type === 'done') {
        this._disarmStuckWatchdog();
        this.speaker?.end();
        es.close();
        if (this.currentEventSource === es) this.currentEventSource = null;
        this._emit('done', { text: fullText });
      } else if (data.type === 'error') {
        // Flush/finish the speaker here too — a mid-stream error while
        // Jarvis is speaking used to strand playback (no more sentences
        // ever flush, and _isSpeaking/_recSuspended never clear) because
        // only 'paused'/'done' called this.
        this._disarmStuckWatchdog();
        this.speaker?.end();
        es.close();
        if (this.currentEventSource === es) this.currentEventSource = null;
        if (!this._isSpeaking) this._resumeRecognition(); // same reasoning as 'paused' above
        this._emit('error', { message: data.error, code: data.code });
        this._backToListening();
      }
    };

    es.onerror = () => {
      if (this.currentEventSource === es) {
        this._disarmStuckWatchdog();
        this.speaker?.end(); // same reasoning as the 'error' branch above
        es.close();
        this.currentEventSource = null;
        if (!this._isSpeaking) this._resumeRecognition(); // same reasoning as 'paused' above
        this._emit('error', { message: 'Could not reach the Jarvis server.' });
        this._backToListening();
      }
    };
  }

  _onSpeechStart() {
    // Leaving the listening family entirely — nothing left to time out
    // toward idle until _backToListening() re-arms it. Mostly redundant
    // with _send()'s own disarm (thinking always precedes speaking), kept
    // here too for the same defense-in-depth reason every other disarm
    // point in this file is explicit rather than assumed.
    this._disarmIdleTimer();
    // The 'thinking' hang backstop (see _send()) hands off to 'speaking's
    // own, better-scoped per-sentence watchdog now — see
    // audio-player.js/browser-speaker.js.
    this._disarmStuckWatchdog();
    // A SECOND, engine-level backstop, layered on top of the per-sentence
    // one inside the speaker classes — found necessary while re-investigating
    // a reported "stuck on 'speaking'" bug: the per-sentence watchdog only
    // guarantees any ONE utterance eventually settles; it does nothing if
    // `speaker.end()` itself is never called (the SSE stream goes silent
    // after the last chunk with no 'done'/'paused'/'error'/close ever
    // arriving — EventSource gives no guaranteed signal for that) or if a
    // stray late speechSynthesis event re-enters 'speaking' with nothing
    // real behind it. onStart fires once PER SENTENCE (both speaker classes
    // chunk that way), so re-arming here on every sentence is still only a
    // "no real progress for this long" check, never a whole-reply cap — a
    // legitimately long reply keeps refreshing this on its own. 60s is a
    // reasoned default (comfortably past the speaker's own 30s-max
    // per-utterance cap), not empirically tuned against real hardware.
    this._armStuckWatchdog(() => this._recoverFromStuckState(), 60000);
    this._isSpeaking = true;
    this._setState('speaking');
    // Off only now that audio is actually playing — an echo can then never
    // become text, without also killing the mic during "thinking" (see
    // _send()'s comment).
    this._suspendRecognition();
    this._startBargeInSampler();
  }

  _onSpeechIdle() {
    this._disarmStuckWatchdog(); // the engine-level 'speaking' backstop armed in _onSpeechStart() — a real, legitimate end to this turn
    this._isSpeaking = false;
    this._speakingBuffer = '';
    clearInterval(this._bargeTimer);
    this._bargeTimer = null;
    this._backToListening();
    // Recognition stays suspended a little longer than the audio itself —
    // see ECHO_TAIL_MS's definition for why. Also flushes anything the
    // (now-silenced) mic may have queued during that last stretch of
    // playback, so it can't be finalized once recognition resumes.
    clearTimeout(this._echoTailTimer);
    this._echoTailTimer = setTimeout(() => {
      clearTimeout(this.silenceTimer);
      this.pendingUtterance = '';
      this.pendingConfidence = null;
      this._resumeRecognition();
    }, ECHO_TAIL_MS);
  }

  /**
   * The 'thinking' hang watchdog's recovery action (see _armStuckWatchdog()
   * in voice-engine.js, armed in _send() and re-armed on forward progress).
   * Reuses interrupt()'s own, already-safe cleanup rather than duplicating
   * it. `_isSpeaking` is always false here (this watchdog is only ever
   * armed before speaking has started), so nothing about barge-in/echo
   * handling is disturbed — this is equivalent to the user never having
   * spoken at all for this (hung) turn.
   */
  _recoverFromStuckState() {
    this._emit('error', { message: 'Jarvis seems to have gotten stuck — resetting.' });
    this.interrupt();
  }

  /**
   * Barge-in, driven by mic energy on a fixed clock rather than by whatever
   * cadence speech-recognition results happened to arrive at (the old bug:
   * two loud instants seconds apart could look like one continuous
   * utterance). Arms immediately at a fixed threshold — no warm-up delay —
   * so interrupting Jarvis feels as fast as it used to.
   */
  _startBargeInSampler() {
    clearInterval(this._bargeTimer);
    this._bargeTimer = null;
    if (!this.micMonitor) return;
    // Clear any "how long has this been loud" timestamp left over from a
    // PREVIOUS reply — without this, a stale timestamp from seconds ago
    // (e.g. room echo of Jarvis's own last syllable) can make the very next
    // reply's sustain check pass on its first sample, cutting it off almost
    // immediately. Confirmed bug: this is what "voice sometimes stops right
    // after starting" traced back to.
    this.micMonitor.reset();

    this._bargeTimer = setInterval(() => {
      // Muted: ambient noise must never interrupt Jarvis — see setMuted().
      if (this.muted) return;
      if (this.micMonitor.hasSustainedSpeech(BARGE_SUSTAIN_MS, BARGE_FLOOR)) {
        clearInterval(this._bargeTimer);
        this._bargeTimer = null;
        this.interrupt(); // stops playback, resumes recognition, sets state — see interrupt()
      }
    }, BARGE_SAMPLE_MS);
  }
}

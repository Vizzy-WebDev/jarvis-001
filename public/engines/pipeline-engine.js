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
import { computeWaitMs, isCompleteThought, MicLevelMonitor } from '../turn-detector.js';
import { AudioPlayer } from '../audio-player.js';
import { BrowserSpeaker } from '../browser-speaker.js';

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

export class PipelineEngine extends VoiceEngine {
  constructor(opts = {}) {
    super();
    this.voiceOutput = opts.voiceOutput || 'browser'; // 'browser' | 'gemini'
    this.geminiVoice = opts.geminiVoice;
    this.useAiTurnCheck = Boolean(opts.useAiTurnCheck);

    this.recognition = null;
    this.stream = null;
    this.micMonitor = null;
    this.active = false;
    this.pendingUtterance = '';
    this.pendingConfidence = null; // lowest confidence seen across this utterance's final chunks, for the voice-clarity guard
    this.silenceTimer = null;
    this.currentEventSource = null;
    this.speaker = null;
    this._isSpeaking = false;
    this._speakingBuffer = ''; // text of the reply currently playing, for the self-echo filter
    this._recSuspended = false; // true only while Jarvis's audio is actually playing + its echo tail
    this._echoTailTimer = null;
    this._bargeTimer = null; // fixed-rate mic-energy sampler while speaking
  }

  updateOptions(opts = {}) {
    if (opts.voiceOutput) this.voiceOutput = opts.voiceOutput;
    if ('geminiVoice' in opts) this.geminiVoice = opts.geminiVoice;
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
    this._startRecognition();
    this._setState('listening');
  }

  stop() {
    this.active = false;
    this.muted = false; // a freshly started/restarted session always begins unmuted
    clearTimeout(this.silenceTimer);
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
    this._setState('idle');
  }

  sendText(text, { attachments = [] } = {}) {
    clearTimeout(this.silenceTimer);
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
    this.pendingUtterance = '';
    this.pendingConfidence = null;
    if (resumeRecognition) this._resumeRecognition();
    if (!keepListening) {
      this._setState(this.active ? 'listening' : 'idle');
    }
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

    clearTimeout(this.silenceTimer);
    const waitMs = computeWaitMs(previewText);
    this.silenceTimer = setTimeout(() => this._maybeFinalize(previewText), waitMs);
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

    // Still actually speaking (energy-wise), even though recognition paused
    // between words — don't cut in. Only reachable once Jarvis has actually
    // stopped talking (guard above) — previously this could defer an echo
    // until the exact moment playback ended and then send it immediately.
    if (this.micMonitor?.isSpeaking()) {
      this.silenceTimer = setTimeout(() => this._maybeFinalize(text), 400);
      return;
    }

    if (this._looksLikeEcho(trimmed)) {
      this.pendingUtterance = '';
      return;
    }

    if (this.useAiTurnCheck) {
      const complete = await isCompleteThought(trimmed);
      if (!complete) {
        this.silenceTimer = setTimeout(() => this._maybeFinalize(trimmed), 800);
        return;
      }
    }

    this.pendingUtterance = '';
    const confidence = this.pendingConfidence;
    this.pendingConfidence = null;
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
    this._setState('thinking');
    // Recognition deliberately stays LIVE through "thinking" — nothing is
    // playing yet, so there's no echo risk, and this is what lets you keep
    // talking or change your mind before Jarvis has said a word (the
    // original conversational feel). It's suspended only once actual audio
    // starts — see _onSpeechStart — which is the narrowest window that
    // still fixes the self-listening bug.

    let fullText = '';
    this.speaker =
      this.voiceOutput === 'gemini'
        ? new AudioPlayer({ onStart: () => this._onSpeechStart(), onIdle: () => this._onSpeechIdle(), voice: this.geminiVoice })
        : new BrowserSpeaker({ onStart: () => this._onSpeechStart(), onIdle: () => this._onSpeechIdle() });

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
        fullText += data.text;
        this._speakingBuffer += data.text;
        this._emit('chunk', { text: data.text });
        this.speaker?.pushText(data.text);
      } else if (data.type === 'tool_start') {
        this._emit('tool', { name: data.name });
      } else if (data.type === 'tool_result') {
        this._emit('tool_result', data);
      } else if (data.type === 'model_switch') {
        this._emit('model_switch', data);
      } else if (data.type === 'restart') {
        // The model that was mid-reply failed — clear what's shown/spoken
        // so far; a fresh reply from the next model follows on the same
        // stream. See server/models/runner.js's header comment for why a
        // clean restart beats stitching two models' text together.
        fullText = '';
        this._speakingBuffer = '';
        this.speaker?.reset();
        this._emit('restart', {});
      } else if (data.type === 'paused') {
        this.speaker?.end();
        es.close();
        if (this.currentEventSource === es) this.currentEventSource = null;
        // If speaking never actually started on this turn, _onSpeechIdle's
        // tail-then-resume will never run — resume directly, or the mic
        // would stay suspended indefinitely. If speaking DID start, leave it
        // to _onSpeechIdle so the echo tail is still respected.
        if (!this._isSpeaking) this._resumeRecognition();
        this._emit('paused', { reason: data.reason });
        this._setState(this.active ? 'listening' : 'idle');
      } else if (data.type === 'done') {
        this.speaker?.end();
        es.close();
        if (this.currentEventSource === es) this.currentEventSource = null;
        this._emit('done', { text: fullText });
      } else if (data.type === 'error') {
        // Flush/finish the speaker here too — a mid-stream error while
        // Jarvis is speaking used to strand playback (no more sentences
        // ever flush, and _isSpeaking/_recSuspended never clear) because
        // only 'paused'/'done' called this.
        this.speaker?.end();
        es.close();
        if (this.currentEventSource === es) this.currentEventSource = null;
        if (!this._isSpeaking) this._resumeRecognition(); // same reasoning as 'paused' above
        this._emit('error', { message: data.error, code: data.code });
        this._setState(this.active ? 'listening' : 'idle');
      }
    };

    es.onerror = () => {
      if (this.currentEventSource === es) {
        this.speaker?.end(); // same reasoning as the 'error' branch above
        es.close();
        this.currentEventSource = null;
        if (!this._isSpeaking) this._resumeRecognition(); // same reasoning as 'paused' above
        this._emit('error', { message: 'Could not reach the Jarvis server.' });
        this._setState(this.active ? 'listening' : 'idle');
      }
    };
  }

  _onSpeechStart() {
    this._isSpeaking = true;
    this._setState('speaking');
    // Off only now that audio is actually playing — an echo can then never
    // become text, without also killing the mic during "thinking" (see
    // _send()'s comment).
    this._suspendRecognition();
    this._startBargeInSampler();
  }

  _onSpeechIdle() {
    this._isSpeaking = false;
    this._speakingBuffer = '';
    clearInterval(this._bargeTimer);
    this._bargeTimer = null;
    this._setState(this.active ? 'listening' : 'idle');
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

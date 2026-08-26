// Engine C — the full-duplex voice engine. Speech-to-text (Deepgram,
// server-proxied — see server/duplex.js — falling back automatically to the
// browser's own SpeechRecognition when no Deepgram key is configured),
// reasoning (the existing, unchanged /api/chat/stream — any model), and
// text-to-speech (server/tts/index.js's provider seam) stay three separate,
// independently swappable layers — this file is the orchestrator between
// them, not a fused pipeline.
//
// Self-echo prevention — REWRITTEN from scratch after two heuristic-based
// attempts both failed to actually stop it live (reported by the user
// three times total). Both earlier attempts tried to FILTER OUT Jarvis's
// own voice after the fact — first with no defense at all (wrongly assumed
// real browser echo cancellation alone would be enough — it isn't reliable
// enough in practice, varies hugely by hardware/speaker-mic distance/
// volume), then with a text-similarity check, then with a text+energy
// combined check. All three still let SOME of Jarvis's own audio reach
// Deepgram as real audio to transcribe, and relied on guessing afterward
// whether the resulting transcript was an echo. That guess can be wrong.
//
// This version removes the guess entirely: audio is simply NOT SENT to
// Deepgram at all while Jarvis is speaking (plus a short buffer after —
// DEEPGRAM_ECHO_TAIL_MS). If Deepgram never receives the audio, it cannot
// transcribe it, self-echo or otherwise — nothing to filter, nothing to
// get wrong. This is the exact same proven mechanism
// engines/pipeline-engine.js already uses successfully (suspend
// recognition while speaking, resume after a short tail) — see that file's
// header comment — adapted here to "stop sending mic frames to Deepgram"
// instead of "stop the browser's own recognizer", since the underlying
// problem (Jarvis's own voice reaching a speech-to-text engine) is
// identical.
//
// This does NOT weaken barge-in. Interruption is still detected the
// instant the user starts talking over Jarvis, via local mic-energy
// sampling (vad.js) — that reads the microphone directly, completely
// independent of whether frames are being sent to Deepgram, so it keeps
// working exactly as fast during the suspended window. The only thing
// that changes is there's no live transcript DURING that exact window —
// sending resumes immediately the moment a real interruption is detected,
// so whatever the user is actually saying gets transcribed as the start of
// the next turn with no perceptible gap.
//
// The browser-STT fallback path (no Deepgram key) already worked this way
// from the start — see _startBrowserFallback() below, unchanged by this
// rewrite.
//
// Mute vs. barge-in, and reconnection — two more real, reported bugs, traced
// and fixed together since the first shares a mechanism with a false-positive
// tuning issue reported at the same time, but the second (reconnection) is a
// genuinely separate root cause, confirmed by tracing rather than assumed:
//
// 1) Muting must be a hard override on interruption — nothing should be able
//    to cut Jarvis off while muted. _onSpeechStart() used to arm barge-in
//    unconditionally; BargeInDetector/MicLevelMonitor tap the raw
//    MediaStream via their own AnalyserNode, entirely independent of
//    setMuted()'s only effect (gating whether frames are SENT). See
//    _onBargeInFired() below for the mute-gated, self-re-arming fix.
// 2) vad.js's BARGE_SUSTAIN_MS/BARGE_FLOOR were raised after real reports of
//    incidental noise (a bumped laptop, shifting around) falsely triggering
//    an interrupt — see that file's own comment for the reasoning.
// 3) The Deepgram WebSocket closing unexpectedly mid-session (e.g. a real
//    inactivity timeout during an extended mute — zero audio bytes get sent
//    for the whole muted duration — or a network blip) used to be a dead
//    end: one error toast, then a permanently zombie socket, since
//    _sendFrameToDeepgram() silently no-ops on a non-OPEN socket forever
//    and nothing ever reconnected. This matched a reported symptom exactly
//    ("mic visually shows unmuted, speech intermittently stops being picked
//    up") without needing the exact disconnect trigger pinned down. See
//    _handleDeepgramDisconnect() below for the bounded-retry fix.

import { VoiceEngine } from './voice-engine.js';
import { MicStream } from '../voice/mic-stream.js';
import { Playback } from '../voice/playback.js';
import { MicLevelMonitor, BargeInDetector } from '../voice/vad.js';
import { computeWaitMs } from '../turn-detector.js';
import { BrowserSpeaker } from '../browser-speaker.js';

const SpeechRecognitionCtor = window.SpeechRecognition || window.webkitSpeechRecognition;

// Fallback-mode-only constant — same reasoning as pipeline-engine.js's
// identical ECHO_TAIL_MS. Not used at all in Deepgram mode.
const FALLBACK_ECHO_TAIL_MS = 700;

// Deepgram-mode echo tail — same reasoning as FALLBACK_ECHO_TAIL_MS above,
// applied for a different cause: not mic suspension (the mic never stops
// here) but ASR processing lag — Deepgram's transcription of the tail end
// of Jarvis's own last few words can arrive shortly AFTER playback has
// already stopped. See this file's header comment.
const DEEPGRAM_ECHO_TAIL_MS = 700;

// How long a hands-free session can sit quietly in 'listening' before
// dropping to a real 'idle' — mic stays fully open the whole time; only the
// STATE (and the orb's honesty about it) changes. Fixes F7: hands-free
// previously had no real idle at all, claiming to listen forever.
const HANDS_FREE_IDLE_MS = 30000;

// How long the 'interrupted' state (a brief acknowledgment flash after a
// real barge-in) is held before settling back to 'listening'. Purely
// visual/status-text — never gates anything functional; the mic is already
// fully live again the instant the barge-in fires.
const INTERRUPTED_FLASH_MS = 500;

export class DuplexEngine extends VoiceEngine {
  constructor(opts = {}) {
    super();
    // 'browser' (the free, offline speechSynthesis voice —
    // public/browser-speaker.js, default, same choice pipeline-engine.js
    // offers) or a configured TTS provider's ref (server/tts/index.js's
    // registry, e.g. 'elevenlabs') — the ref IS the provider id, passed
    // straight through to Playback, which resolves its own voice
    // server-side (e.g. from external-services.js's optional extra field).
    // This is the swappable-TTS half of the design — the seam itself lives
    // server-side; this is the one client-side fork between "ask the
    // server to synthesize" and "ask the browser to just speak it". No
    // longer a separate `ttsProvider`/`geminiVoice` — those were dead,
    // never actually set by any UI, and collapsed into this one value now
    // that Gemini (the only provider that ever existed before) is gone.
    this.voiceOutput = opts.voiceOutput || 'browser';

    this.active = false;
    this.mic = null; // MicStream
    this.micMonitor = null; // MicLevelMonitor, for VAD/orb — separate tap from mic.getLevel(), same as live-engine.js's split
    this.barge = null; // BargeInDetector
    this.playback = null;
    this.ws = null; // /api/duplex socket — null once in browser-fallback mode
    this.sttMode = null; // 'deepgram' | 'browser'

    // Browser-fallback-only state (see _startBrowserFallback()).
    this.fallbackRecognition = null;
    this._fallbackSuspended = false;
    this._fallbackEchoTailTimer = null;
    this._fallbackSilenceTimer = null;

    // Deepgram-mode self-echo prevention — see this file's header comment.
    // Frames stop being sent to Deepgram the instant Jarvis starts
    // speaking and resume DEEPGRAM_ECHO_TAIL_MS after he stops (via
    // _echoBufferClearTimer below, which does both jobs since they always
    // fire together) or immediately on a real barge-in.
    this._micSendSuspended = false;

    // Bounded Deepgram-WS reconnection state — see _handleDeepgramDisconnect().
    this._reconnectingDeepgram = false;

    // Generation token for the current speaker (Playback/BrowserSpeaker) —
    // found necessary during a full state-machine audit: neither speaker's
    // onStart/onIdle callback used to check "am I still the current one?",
    // so a callback from an already-discarded speaker (e.g. a late
    // BrowserSpeaker.onstart firing after stop(), per that class's own
    // documented Chrome quirk) could still mutate LIVE engine state —
    // re-entering 'speaking', killing the current turn's barge-in timer.
    // Bumped in _makeSpeaker() (new speaker) AND _interrupt() (old one
    // discarded, even before any new one exists) — see both.
    this._speakerGen = 0;

    this._interimText = '';
    this._finalText = ''; // accumulated across possibly-multiple is_final Results before speech_final
    this._isSpeaking = false; // Jarvis talking
    this._speakingBuffer = ''; // text of the reply currently playing, for the self-echo filter — see _looksLikeEcho()
    this._echoTailUntil = 0; // performance.now() timestamp — echo guarding stays active until this, past _isSpeaking going false (see DEEPGRAM_ECHO_TAIL_MS)
    this._echoBufferClearTimer = null; // MUST be tracked, not an anonymous setTimeout — see _onSpeechStart()'s comment for the real bug this fixes
    this.currentEventSource = null;

    // Real hands-free idle (F7) and the brief post-barge-in flash — see
    // _armIdleTimer()/_disarmIdleTimer() and _interrupt()'s use of
    // INTERRUPTED_FLASH_MS.
    this._idleTimer = null;
    this._interruptedFlashTimer = null;
  }

  updateOptions(opts = {}) {
    if ('voiceOutput' in opts) this.voiceOutput = opts.voiceOutput;
  }

  /** For the orb: mic energy while listening, 0..1. */
  getMicLevel() {
    return this.micMonitor?.getLevel() ?? 0;
  }

  /** For the orb: Jarvis's own voice output energy while speaking, 0..1. */
  getOutputLevel() {
    return this.playback?.getOutputLevel?.() ?? 0;
  }

  /**
   * Mute/unmute: mic capture ONLY, never `playback`/`currentEventSource`/
   * `state` — same contract as every other engine (see voice-engine.js).
   */
  setMuted(muted) {
    muted = Boolean(muted);
    if (this.muted === muted) return;
    this.muted = muted;
    if (this.sttMode === 'deepgram' && this.mic) {
      // Simplest correct mute for Deepgram mode: stop sending frames. The
      // mic itself keeps capturing (so getMicLevel() still works for the
      // orb, matching every other engine's mute behavior), only the network
      // send is gated. Unmuting must NOT resume sending if Jarvis is
      // currently speaking (or in his echo tail) — self-echo suspension
      // always wins; _resumeDeepgramSendingNow() re-checks `this.muted`
      // itself once that suspension actually lifts.
      this.mic.onFrame = muted || this._micSendSuspended ? null : this._sendFrameToDeepgram.bind(this);
    } else if (this.sttMode === 'browser') {
      this._syncFallbackRecognitionState();
    }
  }

  async start() {
    if (this.active) return;
    if (!window.WebSocket) {
      this._emit('error', { message: 'Voice input needs a browser with WebSocket support.' });
      return;
    }

    this.mic = new MicStream();
    try {
      await this.mic.start();
    } catch {
      this._emit('error', {
        message: 'Microphone access was blocked. Allow the microphone in your browser and try again.',
      });
      this.mic = null;
      return;
    }

    this.micMonitor = new MicLevelMonitor(this.mic.getRawStream());
    this.barge = new BargeInDetector(this.micMonitor);

    this.active = true;
    const connected = await this._connectStt();
    if (!connected) {
      // _connectStt() already emitted the specific error — clean up
      // exactly like a denied mic permission does above, rather than
      // leaving the mic open and the UI claiming "listening" with nothing
      // able to transcribe (see that method's comment for the bug this
      // fixes). stop() is safe to call here even though start() never
      // fully finished — everything it tears down is either already set
      // or safely null.
      this.stop();
      return;
    }
    this._setState('listening');
    this._armIdleTimer();
  }

  /**
   * A fresh speaker for the turn about to start — same pattern
   * pipeline-engine.js's `_send()` already uses (a brand-new AudioPlayer/
   * BrowserSpeaker per turn, not one reused instance), which is what makes
   * a live voiceOutput change (from the settings panel, mid-session) take
   * effect on the very next reply rather than needing a restart, and makes
   * "no start() was ever called" (a typed-only message) work with zero
   * special-casing — nothing here depends on the mic/STT side at all.
   */
  _makeSpeaker() {
    // See _speakerGen's own constructor comment — onStart/onIdle only act
    // if this is still the CURRENT speaker generation by the time they
    // fire, so a stale callback from a discarded speaker is a safe no-op
    // instead of mutating live state.
    const gen = ++this._speakerGen;
    const onStart = () => {
      if (gen === this._speakerGen) this._onSpeechStart();
    };
    const onIdle = () => {
      if (gen === this._speakerGen) this._onSpeechIdle();
    };
    // 'browser' is the one special value — anything else is a configured
    // TTS provider's ref, passed straight through to Playback, which
    // resolves its own voice server-side. Same convention
    // pipeline-engine.js's speaker creation uses.
    return this.voiceOutput === 'browser'
      ? new BrowserSpeaker({ onStart, onIdle })
      : new Playback({ onStart, onIdle, provider: this.voiceOutput });
  }

  stop() {
    this.active = false;
    this.muted = false;
    this._reconnectingDeepgram = false; // _handleDeepgramDisconnect() checks this.active itself, but this stops a stray retry from settling this.sttMode after a fresh start() reuses this instance
    this._disarmIdleTimer();
    clearTimeout(this._interruptedFlashTimer);
    this.interrupt({ keepListening: true });

    if (this.ws) {
      this.ws.onclose = null; // intentional stop — don't treat this as a lost-connection error
      try {
        this.ws.close();
      } catch {
        // already closed
      }
      this.ws = null;
    }
    if (this.fallbackRecognition) {
      this.fallbackRecognition.onend = null;
      try {
        this.fallbackRecognition.stop();
      } catch {
        // already stopped
      }
      this.fallbackRecognition = null;
    }
    clearTimeout(this._fallbackEchoTailTimer);
    clearTimeout(this._fallbackSilenceTimer);
    clearTimeout(this._echoBufferClearTimer);
    // Explicit reset — found during the state-machine audit that these
    // three flags previously had NO other way to clear except the two
    // timers just cancelled above (_echoBufferClearTimer/
    // _fallbackEchoTailTimer). Stopping mid-echo-tail used to strand the
    // mic soft-muted (_micSendSuspended/_fallbackSuspended stuck true) with
    // no error and no recovery on the next start() reusing this instance —
    // a real bug, found by tracing every exit path rather than from a
    // specific report this time.
    this._micSendSuspended = false;
    this._fallbackSuspended = false;
    this._speakingBuffer = '';
    if (this.barge) this.barge.stop();
    if (this.micMonitor) {
      this.micMonitor.close();
      this.micMonitor = null;
    }
    if (this.mic) {
      this.mic.stop();
      this.mic = null;
    }
    this.sttMode = null;
    this._setState('idle');
  }

  sendText(text, { attachments = [] } = {}) {
    this._finalText = '';
    this._send(String(text || '').trim(), { source: 'text', attachments });
  }

  // ---------- STT connection: Deepgram, or the browser fallback ----------

  /**
   * Resolves `true` once the socket is genuinely ready (either Deepgram
   * mode connected, or the browser fallback has been started), `false` on
   * any failure — including a hung connection. Found by independent
   * review: this used to resolve with nothing meaningful on success and
   * never resolve at all if the socket simply stalled (accepted at the TCP
   * level but never sent 'ready', never errored, never closed) — start()
   * awaited this unconditionally either way, so a stall left the mic open
   * and the UI claiming "listening" with nothing ever able to transcribe,
   * no error shown, no way out except manually stopping. The 8s timeout is
   * generous relative to a normal same-machine WS handshake plus
   * Deepgram's own connect time.
   *
   * `silent` suppresses this method's own error emissions — used by
   * _handleDeepgramDisconnect()'s reconnect attempt, which reports its own
   * single, actionable message on failure instead of stacking a generic
   * "could not reach the server" toast underneath it.
   */
  _connectStt({ silent = false } = {}) {
    return new Promise((resolve) => {
      const proto = location.protocol === 'https:' ? 'wss' : 'ws';
      const ws = new WebSocket(`${proto}://${location.host}/api/duplex`);
      this.ws = ws;

      let settled = false;
      const settle = (ok) => {
        if (settled) return; // a later close/error on an already-running session must never retroactively fail start()
        settled = true;
        clearTimeout(timeout);
        resolve(ok);
      };
      const timeout = setTimeout(() => {
        if (!silent) this._emit('error', { message: 'Could not reach the Jarvis server.' });
        settle(false);
        try {
          ws.close();
        } catch {
          // already closed
        }
      }, 8000);

      ws.onmessage = (ev) => {
        let msg;
        try {
          msg = JSON.parse(ev.data);
        } catch {
          return;
        }
        if (msg.type === 'ready') {
          this.sttMode = msg.mode;
          if (msg.mode === 'browser') {
            // Server already closed its end (see server/duplex.js) — no
            // Deepgram key configured. Fall back entirely; this socket is
            // never used again. _startBrowserFallback() can itself fail
            // (no SpeechRecognition in this browser either) — found during
            // the state-machine audit that this used to settle(true)
            // regardless, letting start() claim 'listening' with literally
            // no STT running at all, forever. settle() on whatever it
            // actually reports instead, same as a denied-mic-permission
            // failure already does.
            this.ws = null;
            settle(this._startBrowserFallback());
          } else {
            // Must match setMuted()'s own condition exactly, not just
            // `this.muted` — a reconnect (see _handleDeepgramDisconnect())
            // can land here while Jarvis is mid-reply or still in his echo
            // tail, and self-echo suspension must keep winning even across
            // a reconnect, the same way it wins on every other path.
            this.mic.onFrame = this.muted || this._micSendSuspended ? null : this._sendFrameToDeepgram.bind(this);
            settle(true);
          }
        } else {
          this._handleDeepgramMessage(msg);
        }
      };
      ws.onerror = () => {
        if (!silent) this._emit('error', { message: 'Could not reach the Jarvis server.' });
        settle(false);
      };
      ws.onclose = () => {
        if (settled) {
          // The initial handshake already succeeded — this is an
          // unexpected LATER disconnect of an established session, not a
          // handshake failure (that path is settle(false) below, which is
          // now a no-op since settled is already true). See
          // _handleDeepgramDisconnect() for the bounded-retry fix.
          if (this.active && this.sttMode === 'deepgram') {
            this._handleDeepgramDisconnect();
          }
          return;
        }
        if (!silent && this.active && this.sttMode === 'deepgram') {
          this._emit('error', { message: 'Lost connection to speech recognition.' });
        }
        settle(false);
      };
    });
  }

  /**
   * Called when the Deepgram WS closes unexpectedly AFTER the session was
   * already up and running — see this file's header comment for the full
   * root-cause trace (a real Deepgram inactivity timeout during an extended
   * mute is a plausible trigger, since muting sends zero audio bytes for as
   * long as it's on, but the fix doesn't depend on confirming that specific
   * cause). Previously this was a dead end: _sendFrameToDeepgram() silently
   * no-ops on a non-OPEN socket forever and nothing ever reconnected — a
   * manual stop/restart was the only recovery. One bounded retry, reusing
   * the existing _connectStt() (whose 'ready' handler already re-wires
   * mic.onFrame correctly against whatever the CURRENT mute/speaking state
   * is by the time it lands — no extra logic needed here for that).
   */
  async _handleDeepgramDisconnect() {
    if (this._reconnectingDeepgram) return; // already mid-retry; don't stack attempts on repeated close events
    if (!this.active || this.sttMode !== 'deepgram') return;
    this._reconnectingDeepgram = true;
    this.sttMode = null; // no longer a live session until the retry proves otherwise
    if (this.mic) this.mic.onFrame = null; // nothing to send to while there's no socket
    const ok = await this._connectStt({ silent: true });
    this._reconnectingDeepgram = false;
    if (!this.active) return; // stop() ran while the retry was in flight — nothing left to report
    if (ok) {
      // A stuck 'hearing_speech' from before the disconnect (see
      // _armIdleTimer()'s comment on that same bug) would otherwise wait up
      // to HANDS_FREE_IDLE_MS to resolve on its own — do it immediately now
      // the connection is confirmed healthy again. Deliberately narrow:
      // never touches 'thinking'/'speaking'/'tool_running' — a Deepgram
      // disconnect during an active reply is unrelated to what's actually
      // stuck there, and forcing those back would cut a real reply's
      // displayed state short for no reason.
      if (this.state === 'hearing_speech') this._backToListening();
    } else {
      this._emit('error', {
        message: 'Lost connection to speech recognition and could not reconnect. Toggle the mic off and on to try again.',
      });
    }
  }

  _sendFrameToDeepgram(base64) {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify({ type: 'audio', data: base64 }));
    }
  }

  /**
   * Stops sending mic frames to Deepgram — the primary self-echo defense
   * (see this file's header comment). Called the instant Jarvis starts
   * speaking. While suspended, Deepgram physically cannot transcribe
   * anything, since it never receives the audio at all. Any pending resume
   * (_echoBufferClearTimer, shared with the echo-tail buffer clear — see
   * _onSpeechIdle()) is already cancelled by the time this runs:
   * _onSpeechStart() clears it before calling this.
   */
  _suspendDeepgramSending() {
    if (this._micSendSuspended) return;
    this._micSendSuspended = true;
    if (this.mic) this.mic.onFrame = null;
  }

  /**
   * Resumes sending mic frames to Deepgram immediately — called either
   * once the echo-tail timer elapses after Jarvis finishes normally, or
   * right away on a real barge-in (so whatever the user is actually saying
   * gets transcribed with no perceptible gap). Respects `muted` — an
   * unmute that happened while suspended must not have silently started
   * sending frames; this is the point that actually applies it.
   */
  _resumeDeepgramSendingNow() {
    if (!this._micSendSuspended) return;
    this._micSendSuspended = false;
    if (this.mic && this.sttMode === 'deepgram') {
      this.mic.onFrame = this.muted ? null : this._sendFrameToDeepgram.bind(this);
    }
  }

  /** True while an incoming transcript must still be treated with suspicion — the short window right after Jarvis stops speaking, before sending has actually resumed. See this file's header comment. */
  _echoGuardActive() {
    return this._isSpeaking || performance.now() < this._echoTailUntil;
  }

  _handleDeepgramMessage(msg) {
    if (msg.type === 'error') {
      this._emit('error', { message: msg.error, code: msg.code });
    } else if (msg.type === 'transcript') {
      // Self-echo filter — a SECOND, backup layer on top of the primary
      // defense (not sending Jarvis's audio to Deepgram at all while he's
      // speaking — see _suspendDeepgramSending()). Because sending is
      // suspended for the whole time _isSpeaking is true, a transcript can
      // only ever arrive here once he's already stopped — this filter's
      // real job is covering the short window right after, in case
      // Deepgram is still finishing up processing audio it received just
      // before suspension kicked in. An echoed transcript is dropped
      // entirely: never accumulated, never treated as a barge-in signal.
      if (this._echoGuardActive() && this._looksLikeEcho(msg.text)) return;

      // Real (non-echo) speech activity — wakes from a real hands-free
      // idle if needed, resets the quiet countdown, and reflects
      // 'hearing_speech'. This handler only ever fires while NOT
      // _isSpeaking (sending is suspended for the whole time Jarvis talks —
      // see this file's header comment), so `state` here is always some
      // member of the listening family, never thinking/speaking/tool_running.
      this._noteListeningActivity();

      if (msg.isFinal) {
        const newText = msg.text.trim();
        // Defensive de-dup: Deepgram can occasionally send a trailing/
        // overlapping final result whose text is already (fully or
        // mostly) present in what's been accumulated — e.g. a late
        // correction right around an internal segment boundary. Appending
        // it blindly would repeat words the user already said. Not
        // independently confirmed as the exact mechanism (no live-speech
        // test was possible here — see this file's header comment) but
        // cheap, safe insurance regardless of the precise cause.
        if (newText && !this._finalText.toLowerCase().includes(newText.toLowerCase())) {
          this._finalText = `${this._finalText} ${newText}`.trim();
        }
      } else {
        this._interimText = msg.text;
      }
      const preview = `${this._finalText} ${this._interimText}`.trim();
      this._emit('transcript', { text: preview, final: false });

      // speech_final is deliberately NOT used to finalize/send a turn —
      // that was the actual bug behind two real, reported symptoms (see
      // this file's header comment): Deepgram's `endpointing` (300ms) is
      // meant to mark one CHUNK of text as stable, not "the user is done
      // talking" — a normal mid-sentence pause (breath, comma, thinking a
      // beat) easily exceeds 300ms and was getting sent as if it were the
      // whole thought, then getting a reply, while the user was still
      // mid-sentence. UtteranceEnd (below) is Deepgram's actual
      // purpose-built "genuinely done" signal, on its own longer,
      // separately-configured timer (utterance_end_ms) — that's the only
      // thing that finalizes a turn now. Text still accumulates into
      // _finalText/_interimText above regardless, so nothing is lost
      // while waiting for it.
    } else if (msg.type === 'utterance_end') {
      if (this._finalText) {
        this._finalizeDeepgramTurn(null);
      } else if (this.state === 'hearing_speech') {
        // Genuine silence with nothing accumulated (a stray "um" that never
        // became real speech, or a false SpeechStarted) — without this,
        // state stays stuck at 'hearing_speech' with nothing actually
        // happening, since nothing else would ever move it back.
        this._backToListening();
      }
    } else if (msg.type === 'speech_started') {
      // Informational only — no action needed; the transcript itself is
      // what drives the barge-in check above.
    }
  }

  _finalizeDeepgramTurn(confidence) {
    const text = this._finalText.trim();
    this._finalText = '';
    this._interimText = '';
    if (!text) return;
    this._emit('transcript', { text, final: true });
    this._send(text, { source: 'voice', confidence: typeof confidence === 'number' ? confidence : undefined });
  }

  /**
   * Browser SpeechRecognition fallback — used only when no Deepgram key is
   * configured (server/duplex.js reports mode:'browser'). Needs the same
   * echo-suspension pipeline-engine.js has, for the same reason: this path
   * opens its own separate, unprocessed mic capture that mic-stream.js's
   * real AEC never reaches. Turn-taking here falls back to
   * turn-detector.js's computeWaitMs() countdown, since there's no
   * Deepgram-grade endpointing signal available in this mode — an honest,
   * disclosed exception to "no fixed silence countdown", scoped to the
   * fallback path only, not the primary one.
   */
  _startBrowserFallback() {
    if (!SpeechRecognitionCtor) {
      this._emit('error', { message: 'No Deepgram key is set up, and this browser has no built-in speech recognition either.' });
      return false; // see _connectStt()'s 'ready' handler — this return value now actually matters
    }
    const rec = new SpeechRecognitionCtor();
    rec.lang = 'en-US';
    rec.continuous = true;
    rec.interimResults = true;

    rec.onresult = (event) => {
      if (this._fallbackSuspended || this._isSpeaking || this.muted) return;
      this._noteListeningActivity(); // same reasoning as the Deepgram path's identical call
      let interimText = '';
      let finalText = '';
      for (let i = event.resultIndex; i < event.results.length; i++) {
        const result = event.results[i];
        if (result.isFinal) finalText += result[0].transcript;
        else interimText += result[0].transcript;
      }
      if (finalText) this._finalText += finalText;
      const preview = `${this._finalText} ${interimText}`.trim();
      this._emit('transcript', { text: preview, final: false });

      clearTimeout(this._fallbackSilenceTimer);
      const waitMs = computeWaitMs(preview);
      this._fallbackSilenceTimer = setTimeout(() => this._finalizeFallbackTurn(preview), waitMs);
    };
    rec.onerror = (event) => {
      if (event.error === 'no-speech' || event.error === 'aborted') return;
      if (event.error === 'not-allowed' || event.error === 'service-not-allowed') {
        this._emit('error', { message: 'Microphone access was blocked. Allow the microphone and try again.' });
        this.stop();
      } else if (event.error === 'network') {
        this._emit('error', { message: 'Speech recognition needs an internet connection.' });
      }
    };
    rec.onend = () => {
      if (this.active && !this._fallbackSuspended && !this.muted) {
        try {
          rec.start();
        } catch {
          // a start() is already pending
        }
      }
    };
    rec.start();
    this.fallbackRecognition = rec;
    return true;
  }

  _finalizeFallbackTurn(text) {
    if (this._isSpeaking || this._fallbackSuspended) return;
    const trimmed = text.trim();
    this._finalText = '';
    if (!trimmed) {
      // Same reasoning as the Deepgram path's identical empty-UtteranceEnd
      // case — nothing accumulated, don't leave state stuck at
      // 'hearing_speech' with nothing actually happening.
      if (this.state === 'hearing_speech') this._backToListening();
      return;
    }
    this._emit('transcript', { text: trimmed, final: true });
    this._send(trimmed, { source: 'voice' });
  }

  _suspendFallbackRecognition() {
    clearTimeout(this._fallbackEchoTailTimer);
    this._fallbackSuspended = true;
    this._syncFallbackRecognitionState();
  }

  _resumeFallbackRecognition() {
    clearTimeout(this._fallbackEchoTailTimer);
    this._fallbackSuspended = false;
    this._syncFallbackRecognitionState();
  }

  _syncFallbackRecognitionState() {
    if (!this.fallbackRecognition) return;
    const shouldListen = this.active && !this._fallbackSuspended && !this.muted;
    try {
      if (shouldListen) this.fallbackRecognition.start();
      else this.fallbackRecognition.stop();
    } catch {
      // already in that state, or a start()/stop() is already pending
    }
  }

  // ---------- sending a turn and streaming the reply (reasoning layer — unchanged /api/chat/stream) ----------

  _send(text, opts = {}) {
    const attachments = opts.attachments || [];
    if (!text && !attachments.length) return;
    this._interrupt({ keepListening: true }); // stop/report anything left over from a previous turn
    this._disarmIdleTimer(); // leaving the listening family — nothing left to time out until back-to-listening re-arms it
    this._setState('thinking');
    // Backstop against a hung-but-open EventSource (no chunk, no error, no
    // close — just silence) leaving the engine stuck in
    // 'thinking'/'tool_running' forever — found during the state-machine
    // audit. Re-armed on every chunk/tool_start/tool_result/restart below
    // so a genuinely slow but actively-responding model never trips it;
    // disarmed the moment a reply actually starts being spoken (its own,
    // better-scoped watchdog takes over — see audio-player.js/
    // voice/playback.js) or the turn ends any other way.
    this._armStuckWatchdog(() => this._recoverFromStuckState());

    let fullText = '';
    this.playback = this._makeSpeaker();

    const params = new URLSearchParams({ message: text, source: opts.source || 'voice' });
    if (typeof opts.confidence === 'number') params.set('confidence', String(opts.confidence));
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
        // Resumes from 'tool_running' back to 'thinking' the moment text
        // starts flowing again after a tool call — 'speaking' takes over
        // on its own once playback.onStart actually fires (see
        // _onSpeechStart()), same as the normal thinking->speaking handoff.
        // Deliberately does NOT force 'tool_running'/'thinking' over a
        // genuinely still-'speaking' state — the model can keep streaming
        // LATER sentences (more chunks) while an EARLIER one is still
        // playing, and forcing the state away mid-playback would be
        // dishonest UI (the orb would stop looking like it's speaking while
        // audio is still audible).
        if (this.state === 'tool_running') this._setState('thinking');
        // The thinking/tool_running hang watchdog has no job once
        // genuinely speaking — a REAL, confirmed bug (reproduced with a
        // standalone timer test, not something that needed real audio
        // hardware): a chunk for a LATER sentence arriving while an
        // EARLIER one is still playing used to re-arm this 45s timer
        // unconditionally, with nothing left to disarm it again until
        // 'done' — if that gap ever exceeded 45s (e.g. a tool call
        // interleaved with playback of an earlier sentence), it fired and
        // force-interrupted audio that was playing completely normally.
        // This was the actual root cause of a reported "replies cut off
        // mid-sentence, not from me interrupting" bug.
        if (this.state !== 'speaking') this._armStuckWatchdog(() => this._recoverFromStuckState());
        fullText += data.text;
        this._speakingBuffer += data.text;
        this._emit('chunk', { text: data.text });
        this.playback.pushText(data.text);
      } else if (data.type === 'tool_start') {
        // Same reasoning as 'chunk' above — a tool call can start while an
        // earlier sentence is still audibly playing; neither the state nor
        // the watchdog should override a genuine 'speaking'.
        if (this.state !== 'speaking') {
          this._setState('tool_running');
          this._armStuckWatchdog(() => this._recoverFromStuckState());
        }
        this._emit('tool', { name: data.name });
      } else if (data.type === 'tool_result') {
        if (this.state !== 'speaking') this._armStuckWatchdog(() => this._recoverFromStuckState()); // forward progress, same reasoning as 'chunk'
        this._emit('tool_result', data);
      } else if (data.type === 'model_switch') {
        this._emit('model_switch', data);
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
        fullText = '';
        this._speakingBuffer = '';
        this.playback.reset();
        this._emit('restart', {});
      } else if (data.type === 'paused') {
        this._disarmStuckWatchdog(); // a real, legitimate end to this turn — not a hang
        this.playback.end();
        es.close();
        if (this.currentEventSource === es) this.currentEventSource = null;
        if (!this._isSpeaking) this._backToListening();
        this._emit('paused', { reason: data.reason });
      } else if (data.type === 'done') {
        this._disarmStuckWatchdog();
        this.playback.end();
        es.close();
        if (this.currentEventSource === es) this.currentEventSource = null;
        this._emit('done', { text: fullText });
      } else if (data.type === 'error') {
        this._disarmStuckWatchdog();
        this.playback.end();
        es.close();
        if (this.currentEventSource === es) this.currentEventSource = null;
        if (!this._isSpeaking) this._backToListening();
        this._emit('error', { message: data.error, code: data.code });
      }
    };

    es.onerror = () => {
      if (this.currentEventSource === es) {
        this._disarmStuckWatchdog();
        this.playback.end();
        es.close();
        this.currentEventSource = null;
        if (!this._isSpeaking) this._backToListening();
        this._emit('error', { message: 'Could not reach the Jarvis server.' });
      }
    };
  }

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
      // 'listening'/'hearing_speech' — if something else happened in the
      // meantime (thinking/speaking/tool_running), this timer is stale and
      // does nothing; the state change that caused that already disarmed it
      // (see _onSpeechStart()/_send()), so reaching here with a mismatched
      // state shouldn't normally happen, but the check costs nothing.
      // 'hearing_speech' matters here specifically — found during the
      // state-machine audit that this check used to be 'listening' only,
      // so a Deepgram socket drop between a transcript arriving and its
      // utterance_end (which is the only OTHER thing that ever moves state
      // out of 'hearing_speech') pinned the engine there permanently — this
      // very timer, armed by that same transcript, was the one thing that
      // could have caught it and never did.
      if (this.state === 'listening' || this.state === 'hearing_speech') this._setState('idle');
    }, HANDS_FREE_IDLE_MS);
  }

  _disarmIdleTimer() {
    clearTimeout(this._idleTimer);
    this._idleTimer = null;
  }

  /**
   * Called on any real transcript activity while listening — wakes from a
   * real hands-free idle if needed (F7's flip side: idle must not stay
   * idle once the user actually starts talking again), resets the quiet
   * countdown, and reflects that the user is actively talking right now
   * (distinct from just "mic open, nothing happening") via 'hearing_speech'.
   */
  _noteListeningActivity() {
    if (this.state === 'idle' && this.active) this._setState('listening');
    this._armIdleTimer();
    if (this.state === 'listening') this._setState('hearing_speech');
  }

  _onSpeechStart() {
    // Leaving the listening/hearing_speech/idle family of states entirely —
    // nothing left to time out toward idle until back-to-listening re-arms it.
    this._disarmIdleTimer();
    // The thinking/tool_running hang backstop (see _send()) hands off to
    // 'speaking's own, better-scoped per-sentence watchdog now — see
    // audio-player.js/voice/playback.js.
    this._disarmStuckWatchdog();
    // A SECOND, engine-level backstop, layered on top of the per-sentence
    // one inside the speaker classes — found necessary while re-investigating
    // a reported "stuck on 'speaking'" bug: the per-sentence watchdog only
    // guarantees any ONE utterance eventually settles; it does nothing if
    // `playback.end()` itself is never called (the SSE stream goes silent
    // after the last chunk with no 'done'/'paused'/'error'/close ever
    // arriving) or if a stray late speechSynthesis event re-enters
    // 'speaking' with nothing real behind it. onStart fires once PER
    // SENTENCE (both speaker classes chunk that way), so re-arming here on
    // every sentence is still only a "no real progress for this long"
    // check, never a whole-reply cap — a legitimately long reply keeps
    // refreshing this on its own. 60s is a reasoned default (comfortably
    // past the speaker's own 30s-max per-utterance cap), not empirically
    // tuned against real hardware.
    this._armStuckWatchdog(() => this._recoverFromStuckState(), 60000);
    // Cancels any pending stale-buffer-clear timer left over from a
    // PREVIOUS turn (see _onSpeechIdle()'s comment) so it can't fire
    // mid-way through THIS turn and blank the echo filter's buffer while
    // it's actively re-accumulating. onStart fires once PER SENTENCE
    // (Playback/BrowserSpeaker both chunk playback that way), not once per
    // whole reply — clearing here on every call is safe (idempotent).
    clearTimeout(this._echoBufferClearTimer);
    this._isSpeaking = true;
    this._setState('speaking');
    if (this.sttMode === 'browser') {
      this._suspendFallbackRecognition();
    } else if (this.sttMode === 'deepgram') {
      // The primary self-echo defense — see this file's header comment.
      this._suspendDeepgramSending();
    }
    // Barge-in: local mic energy is the ONLY signal available while
    // speaking (recognition/sending is suspended either way — no
    // transcript can possibly arrive right now), so VAD firing IS the
    // interrupt, directly — same proven design as pipeline-engine.js's
    // identical sampler. `barge` is null if start() was never called (a
    // typed-only session, speaking its reply with no mic ever requested)
    // — nothing to sample from in that case, correctly.
    this.barge?.start(() => this._onBargeInFired());
  }

  /**
   * BargeInDetector's fire callback — routed through here (rather than
   * `_interrupt` directly) so mute can be a hard override: nothing should
   * ever be able to interrupt Jarvis while muted. BargeInDetector stops
   * itself after firing once (by design — see vad.js), so a fire that gets
   * suppressed here must explicitly re-arm it, or barge-in would go dead
   * for the rest of the reply instead of becoming instantly ready the
   * moment the user unmutes.
   */
  _onBargeInFired() {
    if (this.muted) {
      this.barge?.start(() => this._onBargeInFired());
      return;
    }
    this._interrupt();
  }

  /**
   * Last-resort defense on the send path, same as pipeline-engine.js's
   * identical method: if what's about to be treated as user speech still
   * looks like it came from Jarvis's own reply, it's echo, not input.
   * Normalized (punctuation/case-insensitive) token-overlap comparison
   * rather than a raw substring test, which any punctuation or ASR slip
   * defeated. Unlike pipeline-engine.js, this genuinely IS load-bearing
   * here — this engine never suspends the mic, so this is the only thing
   * standing between real browser echo cancellation being imperfect (which
   * it routinely is) and Jarvis replying to its own voice.
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

  _onSpeechIdle() {
    this._disarmStuckWatchdog(); // the engine-level 'speaking' backstop armed in _onSpeechStart() — a real, legitimate end to this turn
    this._isSpeaking = false;
    // _speakingBuffer is deliberately NOT cleared here — _echoGuardActive()
    // keeps treating incoming transcripts with suspicion for
    // DEEPGRAM_ECHO_TAIL_MS past this point (Deepgram's transcription of
    // Jarvis's last few words can arrive after playback has already
    // stopped), and _looksLikeEcho() needs the buffer intact to compare
    // against during that window. Cleared once the tail actually elapses,
    // below — same timer also resumes sending mic frames to Deepgram
    // (_resumeDeepgramSendingNow()), since both exist for the same reason
    // and should end at the same moment.
    this._echoTailUntil = performance.now() + DEEPGRAM_ECHO_TAIL_MS;
    clearTimeout(this._echoBufferClearTimer);
    this._echoBufferClearTimer = setTimeout(() => {
      this._speakingBuffer = '';
      this._resumeDeepgramSendingNow();
    }, DEEPGRAM_ECHO_TAIL_MS);
    this.barge?.stop();
    this._backToListening();
    if (this.sttMode === 'browser') {
      // Same reasoning as pipeline-engine.js's ECHO_TAIL_MS — resumes
      // slightly after audio actually stops, and flushes anything the mic
      // queued during that tail so it can't be finalized once resumed.
      clearTimeout(this._fallbackEchoTailTimer);
      this._fallbackEchoTailTimer = setTimeout(() => {
        clearTimeout(this._fallbackSilenceTimer);
        this._finalText = '';
        this._resumeFallbackRecognition();
      }, FALLBACK_ECHO_TAIL_MS);
    }
  }

  /**
   * The thinking/tool_running hang watchdog's recovery action (see
   * _armStuckWatchdog() in voice-engine.js, armed in _send() and re-armed
   * on forward progress). Reuses _interrupt()'s own, already-safe cleanup
   * (closes a hung EventSource, stops/nulls out playback, resets echo/barge
   * state) rather than duplicating any of it. `_isSpeaking` is always false
   * here — this watchdog is only ever armed for 'thinking'/'tool_running',
   * never 'speaking' — so _interrupt() takes its "not a real barge-in"
   * branch and goes straight back to listening/idle, no flash.
   */
  _recoverFromStuckState() {
    this._emit('error', { message: 'Jarvis seems to have gotten stuck — resetting.' });
    this._interrupt({ keepListening: false });
  }

  /**
   * Barge-in: stop Jarvis mid-reply. `keepListening` is for internal use
   * (a fresh turn is about to start). Reports how much was actually spoken
   * to the server (see playback.js's getSpokenText() and
   * server.js's /api/chat/interrupt) so both chat history and the model's
   * own context reflect what the user truly heard, not what was merely
   * generated — see conversation.js's markLastAssistantInterrupted().
   */
  interrupt({ keepListening = false } = {}) {
    this._interrupt({ keepListening });
  }

  _interrupt({ keepListening = false } = {}) {
    const wasSpeaking = this._isSpeaking;
    // Invalidates any in-flight onStart/onIdle from the speaker about to be
    // stopped below, even before a NEW one exists to compare against — see
    // _speakerGen's own constructor comment / _makeSpeaker(). Also a
    // backstop disarm of the thinking/tool_running watchdog (its primary
    // disarm points are _onSpeechStart() and _send()'s own terminal
    // branches; every call to _interrupt() — a real barge-in, _send()'s
    // defensive pre-turn clear, or stop() — should leave nothing stale
    // armed regardless of which path got here).
    this._speakerGen++;
    this._disarmStuckWatchdog();
    let spokenText = '';
    if (this.playback) {
      // BrowserSpeaker (the free speechSynthesis voice) has no
      // getSpokenText() — it doesn't track a spoken-so-far cursor the way
      // Playback does, so an interruption while using that voice simply
      // isn't reported to the server (the `if (wasSpeaking && spokenText)`
      // check below just never fires for it). Disclosed gap, not a crash or
      // silently wrong behavior: without a report, the turn is left exactly
      // as un-corrected as it always was before this feature existed.
      spokenText = this.playback.getSpokenText?.() ?? '';
      this.playback.stop();
    }
    if (this.currentEventSource) {
      this.currentEventSource.close();
      this.currentEventSource = null;
    }
    this._isSpeaking = false;
    if (wasSpeaking) {
      // This IS a real interruption (barge-in) — resume sending to
      // Deepgram immediately, not after the usual tail delay, so whatever
      // the user is actually saying right now gets transcribed with no
      // perceptible gap. The echo-tail SUSPICION (buffer kept intact,
      // _echoGuardActive() still true) still applies for a short window
      // regardless — Deepgram may still be finishing up processing audio
      // it received just before suspension, and that backup filter (see
      // _handleDeepgramMessage) is what catches any straggler.
      this._echoTailUntil = performance.now() + DEEPGRAM_ECHO_TAIL_MS;
      clearTimeout(this._echoBufferClearTimer);
      this._echoBufferClearTimer = setTimeout(() => {
        this._speakingBuffer = '';
      }, DEEPGRAM_ECHO_TAIL_MS);
      this._resumeDeepgramSendingNow();
    }
    if (this.barge) this.barge.stop();

    if (wasSpeaking && spokenText) {
      fetch('/api/chat/interrupt', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ spokenText }),
      }).catch(() => {
        // Best-effort — a failed report just means the next turn's context
        // isn't corrected; nothing about the live conversation depends on it.
      });
    }

    if (this.sttMode === 'browser' && wasSpeaking) {
      // A turn genuinely in progress when interrupted — its partial fallback
      // transcript state must not leak into the next one.
      clearTimeout(this._fallbackSilenceTimer);
      this._finalText = '';
      this._resumeFallbackRecognition();
    }

    if (!keepListening) {
      if (wasSpeaking) {
        // A genuine barge-in (as opposed to _send()'s own defensive
        // "clear anything left over from a previous turn" call, which
        // always passes keepListening:true and never reaches here) — a
        // brief visual acknowledgment before settling back to listening.
        // Purely cosmetic: the mic is already fully live again by this
        // point regardless (see _resumeDeepgramSendingNow() above).
        clearTimeout(this._interruptedFlashTimer);
        this._setState('interrupted');
        this._interruptedFlashTimer = setTimeout(() => {
          if (this.state === 'interrupted') this._backToListening();
        }, INTERRUPTED_FLASH_MS);
      } else {
        this._backToListening();
      }
    }
  }
}

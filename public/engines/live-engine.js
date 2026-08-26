// Engine B — Gemini Live only. Your mic audio streams to /api/live (which
// proxies to Gemini) and audio streams back, with Google's own voice
// activity detection handling turn-taking and interruption. Expected
// latency: roughly 0.3-0.6s, vs. Engine A's 0.8-1.5s — the tradeoff is
// being locked to Gemini specifically. See ../turn-detector.js's
// counterpart logic in server/live.js for how tool calls are handled
// mid-conversation.
//
// NOT LIVE-VERIFIED END-TO-END: built and shape-checked against the
// installed @google/genai SDK source (see server/live.js's header comment),
// but the shared test key's quota ran out before an actual voice round-trip
// could be tried. The audio format handling below (resampling, PCM16
// conversion, gapless scheduled playback) is standard Web Audio API
// technique, not Gemini-specific, so it carries its own separate risk from
// the Gemini wiring.

import { VoiceEngine } from './voice-engine.js';

const INPUT_SAMPLE_RATE = 16000; // required by Gemini Live for audio input
const OUTPUT_SAMPLE_RATE = 24000; // Gemini Live's audio output rate (confirmed in docs)

export class LiveEngine extends VoiceEngine {
  constructor() {
    super();
    this.ws = null;
    this.stream = null;
    this.micContext = null;
    this.processorNode = null;
    this.sourceNode = null;
    this.playbackContext = null;
    this.nextPlayTime = 0;
    this.scheduledCount = 0;
    this.turnDone = false;
    this.active = false; // true once mic capture is running (conversation mode)
    this._currentReplyText = '';
    this._inputBuffer = ''; // accumulated inputTranscription text for the turn in progress
    this._micLevel = 0; // for the orb — RMS of the last processed mic frame
    this._outputTimeline = []; // for the orb — {playAt, endAt, rms} per scheduled chunk, see getOutputLevel()

    // Generation token for scheduled playback sources — found during the
    // state-machine audit: _stopPlayback() zeroed scheduledCount without
    // detaching each source's onended, so a late one (closing an
    // AudioContext doesn't guarantee already-scheduled sources never fire
    // it) could drive scheduledCount NEGATIVE, which still passes
    // _maybeFinishTurn()'s `<= 0` check — a stale completion firing on a
    // turn that's since moved on. Bumped in _stopPlayback(); each
    // source's onended checks it before ever touching scheduledCount.
    this._playbackGen = 0;
  }

  /** Opens the WebSocket if needed and waits for it to be ready. Safe to call more than once. */
  _connect() {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) return Promise.resolve();
    if (this._connectPromise) return this._connectPromise;

    this._connectPromise = new Promise((resolve, reject) => {
      const proto = location.protocol === 'https:' ? 'wss' : 'ws';
      const ws = new WebSocket(`${proto}://${location.host}/api/live`);
      this.ws = ws;

      ws.onopen = () => resolve();
      ws.onmessage = (ev) => {
        let msg;
        try {
          msg = JSON.parse(ev.data);
        } catch {
          return;
        }
        this._handleServerMessage(msg);
      };
      ws.onerror = () => {
        this._emit('error', { message: 'Could not reach the Jarvis server.' });
        reject(new Error('WebSocket error'));
      };
      ws.onclose = () => {
        this._connectPromise = null;
        if (this.active) {
          this._emit('error', { message: 'Lost connection to Gemini Live.' });
          this.stop();
        }
      };
    });

    return this._connectPromise;
  }

  async start() {
    if (this.active) return;

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

    try {
      await this._connect();
    } catch {
      return; // 'error' event already emitted by _connect
    }

    this.active = true;
    this._startMicCapture();
    this._setState('listening');
  }

  stop() {
    this.active = false;
    this.muted = false; // a freshly started/restarted session always begins unmuted
    this._disarmStuckWatchdog();
    if (this.processorNode) {
      this.processorNode.disconnect();
      this.processorNode = null;
    }
    if (this.sourceNode) {
      this.sourceNode.disconnect();
      this.sourceNode = null;
    }
    if (this.micContext) {
      this.micContext.close().catch(() => {});
      this.micContext = null;
    }
    if (this.stream) {
      this.stream.getTracks().forEach((t) => t.stop());
      this.stream = null;
    }
    this._stopPlayback();
    if (this.ws) {
      this.ws.onclose = null;
      this.ws.close();
      this.ws = null;
    }
    // The same engine instance can be stopped and restarted (via the mic
    // button) without creating a fresh one — clear per-turn state so a new
    // session doesn't inherit leftovers from the last.
    this.turnDone = false;
    this._currentReplyText = '';
    this._inputBuffer = '';
    this._connectPromise = null;
    this._setState('idle');
  }

  /** For the orb: mic energy while listening, 0..1. */
  getMicLevel() {
    return this._micLevel;
  }

  /**
   * For the orb: Jarvis's own voice output energy while speaking, 0..1.
   * Reads `_outputTimeline` (built in `_playChunk` as chunks are scheduled)
   * against `playbackContext.currentTime` — not the schedule-time RMS this
   * used to return directly, which ran ahead of what's actually audible and
   * froze on the last chunk's value once everything was queued.
   */
  getOutputLevel() {
    if (!this.playbackContext) return 0;
    const now = this.playbackContext.currentTime;
    const chunk = this._outputTimeline.find((c) => now >= c.playAt && now < c.endAt);
    return chunk ? chunk.rms : 0;
  }

  /**
   * Mute/unmute: stops uploading mic audio frames ONLY — never touches `ws`,
   * `playbackContext`, `scheduledCount`, `turnDone`, or `state`. Note this
   * does disable Gemini Live's own native barge-in while muted (it depends
   * on continuous mic upload to detect an interruption at all) — that's
   * expected and correct: if the mic is muted, Jarvis genuinely can't hear
   * you talking over him either, same as if you'd walked away.
   */
  setMuted(muted) {
    this.muted = Boolean(muted);
  }

  async sendText(text) {
    text = String(text || '').trim();
    if (!text) return;
    try {
      await this._connect();
    } catch {
      return;
    }
    this.ws.send(JSON.stringify({ type: 'text', text }));
    this._setState('thinking');
    // Backstop against the server going silent after this — no chunk, no
    // audio, no error, no close — found during the state-machine audit.
    // Re-armed on transcript_out/tool_start/tool_result (forward progress);
    // disarmed once audio actually starts (its own tracking takes over —
    // see the constructor's _playbackGen comment) or the turn ends any
    // other way.
    this._armStuckWatchdog(() => this._recoverFromStuckState());
  }

  // Gemini Live always speaks with its own voice and has no separate model
  // to switch, turn-check setting, etc. — app.js calls updateOptions()
  // unconditionally whenever those controls change, regardless of which
  // engine is active, so this needs to exist as a safe no-op rather than
  // throwing (those rows are hidden in the UI while Live is active, but the
  // listeners themselves aren't torn down).
  updateOptions() {}

  /** Barge-in: clear playback immediately for responsiveness (Gemini's own VAD also detects this server-side). */
  interrupt() {
    this._disarmStuckWatchdog();
    this._stopPlayback();
    this._setState(this.active ? 'listening' : 'idle');
  }

  /**
   * The 'thinking' hang watchdog's recovery action (see _armStuckWatchdog()
   * in voice-engine.js, armed in sendText() and re-armed on forward
   * progress). Unlike the other two engines this doesn't reuse
   * interrupt() — a genuinely stuck turn may have partial
   * _currentReplyText/_inputBuffer accumulated, which interrupt() alone
   * doesn't clear and stop() (which does) would tear down the whole
   * session, not just this turn.
   */
  _recoverFromStuckState() {
    this._emit('error', { message: 'Jarvis seems to have gotten stuck — resetting.' });
    this._stopPlayback();
    this.turnDone = false;
    this._currentReplyText = '';
    this._inputBuffer = '';
    this._setState(this.active ? 'listening' : 'idle');
  }

  // ---------- server -> client messages ----------

  _handleServerMessage(msg) {
    if (msg.type === 'error') {
      // Found during the state-machine audit: this was the only one of the
      // three engines whose server-error path reset nothing — a mid-turn
      // error left state (and any already-scheduled playback) exactly as
      // it was, matching neither pipeline-engine.js's nor duplex-engine.js's
      // own error handling.
      this._disarmStuckWatchdog();
      this._stopPlayback();
      this._setState(this.active ? 'listening' : 'idle');
      this._emit('error', { message: msg.error, code: msg.code });
    } else if (msg.type === 'tool_start') {
      // The 'thinking' hang watchdog has no job once genuinely speaking —
      // a REAL, confirmed bug (reproduced with a standalone timer test,
      // not something needing real audio hardware): Gemini can stream a
      // tool call or later reply text while earlier audio is still
      // playing, and re-arming this 45s timer unconditionally in that
      // window left it live with nothing to disarm it again until
      // 'turn_complete' — if that gap ever exceeded 45s, it fired and
      // force-interrupted audio that was playing completely normally.
      if (this.state !== 'speaking') this._armStuckWatchdog(() => this._recoverFromStuckState());
      this._emit('tool', { name: msg.name });
    } else if (msg.type === 'tool_result') {
      if (this.state !== 'speaking') this._armStuckWatchdog(() => this._recoverFromStuckState()); // forward progress, same reasoning as transcript_out below
      this._emit('tool_result', msg);
    } else if (msg.type === 'transcript_in') {
      // Gemini doesn't mark a distinct "final" point for input transcription
      // — the natural boundary is the moment the reply starts, so we buffer
      // and finalize it there (see _finalizeInputTranscript below).
      this._inputBuffer += msg.text;
      this._emit('transcript', { text: this._inputBuffer, final: false });
    } else if (msg.type === 'transcript_out') {
      if (this.state !== 'speaking') this._armStuckWatchdog(() => this._recoverFromStuckState()); // forward progress — re-arm, not a fresh problem
      this._finalizeInputTranscript();
      this._currentReplyText += msg.text;
      this._emit('chunk', { text: msg.text });
    } else if (msg.type === 'audio') {
      // The 'thinking' hang backstop (see sendText()) hands off to
      // scheduled-playback's own completion tracking now (_maybeFinishTurn
      // via each source's onended) — see the generation-token comment in
      // the constructor for the guarantee that tracking is correct.
      this._disarmStuckWatchdog();
      // A SECOND, engine-level backstop layered on top of that tracking —
      // added while re-investigating a reported "stuck on 'speaking'" bug
      // on the other two engines, and extended here for the same reason:
      // _maybeFinishTurn() only ever fires from 'turn_complete' arriving OR
      // a scheduled source's onended — if the server goes silent after the
      // last audio chunk with no 'turn_complete'/'error'/close ever
      // arriving, nothing would otherwise ever recover. Re-armed on every
      // 'audio' chunk (forward progress), same reasoning as the other two
      // engines' identical fix.
      this._armStuckWatchdog(() => this._recoverFromStuckState(), 60000);
      this._finalizeInputTranscript();
      this._playChunk(msg.data);
      this._setState('speaking');
    } else if (msg.type === 'interrupted') {
      // Gemini's own server-side VAD detected the user talking over it —
      // this is Gemini Live's native barge-in, and it depends on us
      // streaming mic audio to it continuously, including while its own
      // audio is playing (an earlier version of this gated mic upload
      // during playback to avoid Jarvis hearing its own voice, the same fix
      // as the Any-model pipeline engine — but that fix broke exactly this
      // feature for Live, since Gemini can't detect an interruption in
      // audio it was never sent. Self-echo here is Gemini's own problem to
      // solve server-side, not ours to gate client-side).
      // Diagnostic only, no behavior change: if Jarvis's speech ever stops
      // early on THIS engine, check for this line in the console — it means
      // Gemini's own VAD decided (rightly or wrongly) that it was
      // interrupted; nothing client-side can distinguish a real interruption
      // from Gemini's VAD over-triggering on its own echo.
      console.info('[LiveEngine] Gemini reported an interruption — stopping playback.');
      // Disarms the engine-level 'speaking' backstop armed on 'audio' above
      // — a real, legitimate end to this turn (a gap that would otherwise
      // have let a later false "stuck" recovery fire on a turn that
      // already correctly ended here).
      this._disarmStuckWatchdog();
      this._stopPlayback();
      this._setState(this.active ? 'listening' : 'idle');
    } else if (msg.type === 'turn_complete') {
      this.turnDone = true;
      this._maybeFinishTurn();
    }
  }

  /** Turns the buffered input transcript into a permanent bubble, right as the reply begins. */
  _finalizeInputTranscript() {
    const text = this._inputBuffer.trim();
    this._inputBuffer = '';
    if (text) this._emit('transcript', { text, final: true });
  }

  _maybeFinishTurn() {
    if (this.turnDone && this.scheduledCount <= 0) {
      this._disarmStuckWatchdog(); // covers a turn that finished WITHOUT ever producing audio (e.g. tool-only) — 'audio' isn't guaranteed to be the thing that disarms it
      this._emit('done', { text: this._currentReplyText });
      this._currentReplyText = '';
      this._inputBuffer = '';
      this.turnDone = false;
      this._setState(this.active ? 'listening' : 'idle');
    }
  }

  // ---------- mic capture: resample to 16kHz PCM16, stream over the socket ----------

  _startMicCapture() {
    const AudioContextClass = window.AudioContext || window.webkitAudioContext;
    this.micContext = new AudioContextClass();
    this.sourceNode = this.micContext.createMediaStreamSource(this.stream);

    // ScriptProcessorNode is deprecated but still universally supported and
    // far simpler than shipping a separate AudioWorklet module for a first
    // version — worth revisiting if a browser ever drops it.
    this.processorNode = this.micContext.createScriptProcessor(4096, 1, 1);
    this.processorNode.onaudioprocess = (event) => {
      if (!this.active) return;
      const input = event.inputBuffer.getChannelData(0);
      this._micLevel = rmsOf(input);
      // Muted: the whole point is Jarvis genuinely can't hear you — don't
      // send anything. (Mic level above still updates for the orb, same as
      // pipeline-engine.js's getMicLevel().)
      if (this.muted) return;
      // Otherwise streams continuously, including while Jarvis's own audio
      // plays — Gemini Live's own server-side VAD needs that to detect an
      // interruption at all (see the 'interrupted' handler above).
      if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;
      const resampled = resampleTo16k(input, this.micContext.sampleRate);
      const pcm16 = floatTo16BitPCM(resampled);
      this.ws.send(JSON.stringify({ type: 'audio', data: arrayBufferToBase64(pcm16.buffer) }));
    };

    this.sourceNode.connect(this.processorNode);
    // Some browsers require the processor node connected to a destination
    // to keep running; this doesn't route mic audio to the speakers.
    this.processorNode.connect(this.micContext.destination);
  }

  // ---------- playback: decode 24kHz PCM16, schedule gaplessly ----------

  _ensurePlaybackContext() {
    if (this.playbackContext) return;
    const AudioContextClass = window.AudioContext || window.webkitAudioContext;
    this.playbackContext = new AudioContextClass({ sampleRate: OUTPUT_SAMPLE_RATE });
    this.nextPlayTime = this.playbackContext.currentTime;
    // Chrome's autoplay policy can hand back a SUSPENDED context — this one
    // is created inside a WebSocket message handler, not a user-gesture
    // callback, so that's real exposure here (audio-player.js's plain
    // `new Audio()` elements don't need an AudioContext at all, so they
    // never had this specific risk). A source scheduled on a suspended
    // context never actually plays and never fires onended, so
    // scheduledCount never drains and _maybeFinishTurn() can never pass —
    // 'speaking' stuck forever with literally no sound. Found during the
    // state-machine audit; this engine is documented at the top of this
    // file as never having been live-verified end-to-end, so this was
    // genuinely untested territory, not a regression.
    if (this.playbackContext.state === 'suspended') {
      this.playbackContext.resume().catch((err) => {
        console.warn('[LiveEngine] could not resume the playback AudioContext:', err);
      });
    }
  }

  _playChunk(base64) {
    this._ensurePlaybackContext();
    const buffer = base64ToArrayBuffer(base64);
    const int16 = new Int16Array(buffer);
    const float32 = new Float32Array(int16.length);
    for (let i = 0; i < int16.length; i++) float32[i] = int16[i] / 32768;

    const audioBuffer = this.playbackContext.createBuffer(1, float32.length, OUTPUT_SAMPLE_RATE);
    audioBuffer.copyToChannel(float32, 0);

    const source = this.playbackContext.createBufferSource();
    source.buffer = audioBuffer;
    source.connect(this.playbackContext.destination);

    const startAt = Math.max(this.nextPlayTime, this.playbackContext.currentTime);
    source.start(startAt);
    this.nextPlayTime = startAt + audioBuffer.duration;

    // For the orb — see getOutputLevel(). Recorded against this chunk's own
    // scheduled [start, end) window rather than exposed directly, so a
    // caller polling later (once several chunks are already queued) reads
    // whichever chunk is ACTUALLY audible at that moment, not just the last
    // one scheduled. Pruned to the last ~2s so this array can't grow
    // unbounded over a long reply.
    this._outputTimeline.push({ playAt: startAt, endAt: startAt + audioBuffer.duration, rms: rmsOf(float32) });
    const cutoff = this.playbackContext.currentTime - 2;
    this._outputTimeline = this._outputTimeline.filter((c) => c.endAt >= cutoff);

    this.scheduledCount++;
    const gen = this._playbackGen;
    source.onended = () => {
      // See the constructor's _playbackGen comment — a source scheduled
      // before a _stopPlayback() call can still fire onended after it
      // (closing an AudioContext doesn't guarantee that), and must not
      // touch the NEW generation's scheduledCount if so.
      if (gen !== this._playbackGen) return;
      this.scheduledCount--;
      this._maybeFinishTurn();
    };
  }

  _stopPlayback() {
    this._playbackGen++; // invalidate any in-flight onended from sources scheduled before this call — see the constructor's comment
    if (this.playbackContext) {
      this.playbackContext.close().catch(() => {});
      this.playbackContext = null;
    }
    this.nextPlayTime = 0;
    this.scheduledCount = 0;
    this._outputTimeline = [];
  }
}

// ---------- audio format helpers ----------

/** RMS of a Float32Array in [-1, 1] — same maths as turn-detector.js's MicLevelMonitor, for a Float32 source instead of a Uint8 one. */
function rmsOf(float32) {
  let sumSquares = 0;
  for (const sample of float32) sumSquares += sample * sample;
  return Math.sqrt(sumSquares / float32.length);
}

function resampleTo16k(float32Input, inputSampleRate) {
  if (inputSampleRate === INPUT_SAMPLE_RATE) return float32Input;
  const ratio = inputSampleRate / INPUT_SAMPLE_RATE;
  const outputLength = Math.round(float32Input.length / ratio);
  const output = new Float32Array(outputLength);
  for (let i = 0; i < outputLength; i++) {
    output[i] = float32Input[Math.floor(i * ratio)];
  }
  return output;
}

function floatTo16BitPCM(float32) {
  const int16 = new Int16Array(float32.length);
  for (let i = 0; i < float32.length; i++) {
    const s = Math.max(-1, Math.min(1, float32[i]));
    int16[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
  }
  return int16;
}

function arrayBufferToBase64(buffer) {
  let binary = '';
  const bytes = new Uint8Array(buffer);
  for (let i = 0; i < bytes.length; i++) binary += String.fromCharCode(bytes[i]);
  return btoa(binary);
}

function base64ToArrayBuffer(base64) {
  const binary = atob(base64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return bytes.buffer;
}

// The shared contract both engines implement — Pipeline (any AI model,
// this file's sibling) and Live (Gemini Live, Phase 3) — so app.js can
// drive whichever one is active without knowing or caring which it is.
//
// Methods every engine implements:
//   start()          - begin listening (mic permission, wire everything up)
//   stop()           - stop listening entirely, release the mic
//   sendText(text)   - send a typed message through the same pipeline as voice
//   interrupt()      - stop Jarvis mid-reply (barge-in)
//   setMuted(bool)   - mic input on/off ONLY; never touches the current turn (`.muted` reflects it)
//
// Events, subscribed via .on(event, handler):
//   'state'         { state } — the base set every engine emits is
//                    'idle'|'listening'|'thinking'|'speaking'.
//                    `DuplexEngine` emits a superset on top of that:
//                    'hearing_speech' (live transcript activity while
//                    listening), 'tool_running' (a tool call in flight,
//                    distinct from passive thinking), and 'interrupted' (a
//                    brief flash right after a real barge-in) — see that
//                    file for exactly when each fires. A previous draft of
//                    this comment described the base set only and called
//                    the richer one "planned but not yet built" well after
//                    it actually shipped — stale documentation being worse
//                    than none is why this note exists at all; keep this
//                    list matching whatever states are actually emitted.
//   'transcript'    { text, final }   - live transcript as the user speaks
//   'chunk'         { text }          - a piece of Jarvis's reply, as it streams in
//   'tool'          { name }          - Jarvis is using a skill
//   'tool_result'   { name, ok, ui_action?, needs_confirmation?, summary? }
//   'model_switch'  { from, to, reason } - the model handling this turn changed mid-reply
//   'style_floors'  { floors, sticky } - debug-only: personality.js's readStyle() found at
//                    least one floor this turn (distress/serious-topic/explicit style/devil's
//                    advocate). Never shown unless the debug toggle in settings is on
//                    (public/settings.js) — see app.js's handling of this event.
//   'reaction'      { kind }           - a real, non-verbal vocal cue belongs here (currently only
//                    kind:'laugh'). Both engines queue a real sound clip for it (see
//                    reaction-sounds.js and each speaker's enqueueClip()) rather than re-emitting
//                    it further — app.js does not need to handle this event itself.
//   'restart'       {}                 - clear any partial reply shown/spoken; a fresh one follows
//   'stt_fallback'  { mode }           - DuplexEngine only: landed on the browser-STT fallback
//                    (no Deepgram key configured) instead of real Deepgram endpointing/interrupts.
//                    Fires once, from start()'s own connect — never from the silent bounded
//                    reconnect retry. See duplex-engine.js's _connectStt() and app.js's handling.
//   'paused'        { reason }         - no model could complete the turn; nothing was answered
//   'done'          { text }          - the full reply, once finished
//   'tts_failure'   { provider }       - the configured server-side TTS provider failed to
//                    produce audio for this reply (every retry exhausted — see
//                    audio-player.js's/voice/playback.js's fetchTts()), e.g. an expired key
//                    or an account out of credit. NOT 'error' on purpose — the reply itself
//                    still generated/rendered fine, only its audio didn't; 'error' would tell
//                    app.js the whole turn failed and clear the in-progress assistant bubble.
//                    Fires at most once per reply. See app.js's handling of this event.
//   'error'         { message }
export class VoiceEngine {
  constructor() {
    this._listeners = new Map();
    this.state = 'idle';
    this.muted = false; // engines set this true/false directly — see setMuted()
    this._stuckWatchdogTimer = null; // see _armStuckWatchdog()
  }

  on(event, handler) {
    if (!this._listeners.has(event)) this._listeners.set(event, new Set());
    this._listeners.get(event).add(handler);
    return () => this._listeners.get(event)?.delete(handler);
  }

  _emit(event, payload) {
    for (const handler of this._listeners.get(event) || []) {
      try {
        handler(payload);
      } catch (err) {
        console.error(`[${this.constructor.name}] listener error for "${event}":`, err);
      }
    }
  }

  _setState(state) {
    this.state = state;
    this._emit('state', { state });
  }

  /**
   * A generic "something's stuck" backstop — found necessary during a full
   * audit of every engine's state transitions (a real, reported bug:
   * sometimes the UI stays stuck showing 'speaking'). NOT wired into
   * `_setState()` itself and not armed for every non-idle/non-listening
   * state — 'speaking' has its own, better-scoped fix (a per-sentence
   * watchdog in audio-player.js/voice/playback.js/browser-speaker.js,
   * sized to each sentence rather than the whole reply, so a long reply
   * can never falsely trip a reply-wide timer); 'hearing_speech' and
   * 'interrupted' have their own correct/fixed mechanisms. This helper
   * exists for 'thinking'/'tool_running' specifically, armed/disarmed
   * explicitly at each engine's own call sites (entering those states,
   * and re-armed on every chunk/tool event so a genuinely slow but
   * actively-responding model never trips it — only a truly silent hang
   * does). `ms` starting value ~45s is a reasoned default, not empirically
   * tuned against real hardware (not possible in this dev environment) —
   * may need adjustment from real-world use.
   */
  _armStuckWatchdog(onStuck, ms = 45000) {
    clearTimeout(this._stuckWatchdogTimer);
    this._stuckWatchdogTimer = setTimeout(() => {
      console.warn(`[${this.constructor.name}] stuck in '${this.state}' with no forward progress for ${ms}ms — recovering.`);
      onStuck();
    }, ms);
  }

  _disarmStuckWatchdog() {
    clearTimeout(this._stuckWatchdogTimer);
    this._stuckWatchdogTimer = null;
  }

  async start() {
    throw new Error('start() not implemented');
  }

  stop() {
    throw new Error('stop() not implemented');
  }

  sendText(_text) {
    throw new Error('sendText() not implemented');
  }

  interrupt() {
    throw new Error('interrupt() not implemented');
  }

  /**
   * Mute/unmute: disables microphone input ONLY. Must never touch the
   * current turn — no effect on `speaker`, `currentEventSource`/`ws`,
   * `state`, or anything else in flight. Default no-op; engines override.
   */
  setMuted(_muted) {}

  /** For the orb: current mic input energy, 0..1. Default no-op; engines override. */
  getMicLevel() {
    return 0;
  }

  /** For the orb: current Jarvis-voice output energy, 0..1, while state is 'speaking'. Default no-op; engines override. */
  getOutputLevel() {
    return 0;
  }
}

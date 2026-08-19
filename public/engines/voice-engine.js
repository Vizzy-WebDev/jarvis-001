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
//   'state'         { state: 'idle'|'listening'|'thinking'|'speaking' }
//   'transcript'    { text, final }   - live transcript as the user speaks
//   'chunk'         { text }          - a piece of Jarvis's reply, as it streams in
//   'tool'          { name }          - Jarvis is using a skill
//   'tool_result'   { name, ok, ui_action?, needs_confirmation?, summary? }
//   'model_switch'  { from, to, reason } - the model handling this turn changed mid-reply
//   'restart'       {}                 - clear any partial reply shown/spoken; a fresh one follows
//   'paused'        { reason }         - no model could complete the turn; nothing was answered
//   'done'          { text }          - the full reply, once finished
//   'error'         { message }
export class VoiceEngine {
  constructor() {
    this._listeners = new Map();
    this.state = 'idle';
    this.muted = false; // engines set this true/false directly — see setMuted()
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

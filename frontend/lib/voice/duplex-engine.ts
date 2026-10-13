'use client';

import { AudioPlayer } from './audio-player';
import { BrowserSpeaker } from './browser-speaker';
import { VoiceEngine } from './engine';
import { MicStream } from './mic-stream';
import { soundFor } from './reaction-sounds';
import { describeProgress } from './recognition-socket';
import { MicLevelMonitor } from './turn-detector';

/**
 * Engine C — keeps listening while it talks.
 *
 * Four layers, kept separate on purpose: recognition (this app's own
 * `/api/duplex` — on this computer or the cloud service the person chose; never the
 * browser's built-in recognition), reasoning (the
 * ordinary chat stream, any model), tools, and speech (any configured voice).
 * This file is the orchestrator between them, not a fusion of them.
 *
 * **Self-echo is prevented by not sending the audio, not by filtering it
 * afterwards.** The original tried filtering three times — nothing, then text
 * similarity, then text plus energy — and all three still let some of the
 * assistant's own voice reach the recogniser as real audio, leaving a guess
 * about whether the resulting transcript was an echo. A guess can be wrong.
 * Frames simply stop being sent while it speaks, plus a short tail. Nothing
 * received, nothing to transcribe, nothing to guess about.
 *
 * That costs nothing for barge-in: interruption is detected from local
 * microphone energy, which reads the device directly and is unaffected by
 * whether frames are being sent. Sending resumes the instant an interruption
 * fires, so what the person is actually saying is transcribed as the next turn.
 */

/** Recognition runs a beat behind the audio, so the last words of a reply can
 *  be transcribed shortly after playback has already stopped. */
const ECHO_TAIL_MS = 700;

/**
 * Barge-in, sampled on a fixed clock — sampled sporadically instead, two loud
 * instants seconds apart can look like one continuous utterance.
 *
 * These floors sit deliberately higher than the pipeline engine's. There, a
 * false trigger cuts a reply off silently with nothing following, which is
 * invisible enough that nobody reports it; here it visibly kills a reply that
 * is being spoken. Raised after repeated real reports of incidental noise — a
 * bumped desk, shifting in a chair — interrupting. Sustained duration is the
 * main lever: a bump is a transient, speech is not.
 */
const BARGE_SAMPLE_MS = 100;
const BARGE_SUSTAIN_MS = 450;
const BARGE_FLOOR = 0.08;

/** How long a hands-free session may rest quietly before the state drops to a
 *  real idle. The microphone stays fully open — only the honesty changes. */
const HANDS_FREE_IDLE_MS = 30_000;

/** How long `interrupted` is held before settling back. Purely a visible
 *  acknowledgement: the microphone is already live again by then. */
const INTERRUPTED_FLASH_MS = 500;

/** The engine-level backstop while speaking, above each sentence's own. */
const SPEAKING_STUCK_MS = 60_000;

/** A stalled socket that accepts and then says nothing must not leave the
 *  microphone open under a screen claiming to be listening. */
const SOCKET_READY_TIMEOUT_MS = 8000;

type Speaker = AudioPlayer | BrowserSpeaker;

/**
 * Which recogniser the socket answered with (`local`, or a cloud service's name).
 *
 * Deliberately not a union of provider names. Nothing here branches on which one it
 * is — only on whether recognition is running — so adding another server-side needs
 * no change on this side at all.
 */
type Mode = string;

interface SocketMessage {
  type?: string;
  mode?: Mode;
  text?: string;
  isFinal?: boolean;
  speechFinal?: boolean;
  confidence?: number;
  error?: string;
  code?: string;
  fatal?: boolean;
  label?: string;
  done?: number;
  total?: number;
}

export class DuplexEngine extends VoiceEngine {
  private active = false;
  private mic: MicStream | null = null;
  /** A second tap on the same device: the level monitor measures loudness its
   *  own way, which is what barge-in and the orb read. */
  private levels: MicLevelMonitor | null = null;
  private socket: WebSocket | null = null;
  private mode: Mode | null = null;
  private reconnecting = false;
  /** Why the first connection failed, said only once stop() has run: stop() ends in
   *  setState('idle'), which writes a generic status — the reason must be the last write. */
  private startFailure: string | null = null;

  /** The recognition socket is ready and running. */
  private get proxied(): boolean {
    return this.mode !== null;
  }

  private speaker: Speaker | null = null;
  /** Invalidates a discarded speaker's late callbacks — see `interrupt`. */
  private speakerGeneration = 0;
  private turnStream: EventSource | null = null;

  private speaking = false;
  private spokenBuffer = '';
  private finalText = '';
  private interimText = '';

  /** Frames are held back while it speaks — the primary echo defence. */
  private sendSuspended = false;
  private echoGuardUntil = 0;
  private echoTailTimer: ReturnType<typeof setTimeout> | null = null;

  private idleTimer: ReturnType<typeof setTimeout> | null = null;
  private flashTimer: ReturnType<typeof setTimeout> | null = null;
  private bargeTimer: ReturnType<typeof setInterval> | null = null;

  constructor(private readonly options: { voiceOutput?: string } = {}) {
    super();
  }

  get voiceOutput(): string {
    return this.options.voiceOutput || 'browser';
  }

  /** For the orb: input loudness, 0..1. */
  getMicLevel(): number {
    return this.levels?.getLevel() ?? 0;
  }

  /** For the orb: its own voice, 0..1. */
  getOutputLevel(): number {
    return this.speaker?.getOutputLevel() ?? 0;
  }

  // --- taking and releasing the microphone ------------------------------------

  async start(): Promise<void> {
    if (this.active) return;
    if (typeof WebSocket === 'undefined') {
      this.emit('error', { message: 'Speaking to Jarvis needs a browser with WebSocket support.', fatal: true });
      return;
    }

    this.mic = new MicStream();
    try {
      await this.mic.start();
    } catch {
      this.emit('error', {
        message: 'The microphone was blocked. Allow it in your browser and try again.',
        fatal: true,
      });
      this.mic = null;
      return;
    }

    const raw = this.mic.getRawStream();
    if (raw) this.levels = new MicLevelMonitor(raw);

    this.active = true;
    this.startFailure = null;
    if (!(await this.connect())) {
      // Tearing down matters: leaving the microphone open under a screen claiming to
      // listen, with nothing able to transcribe, is the exact shape of the bug this
      // replaces. The server's own reason is said after, so it is what stays on screen.
      const reason = this.startFailure;
      this.stop();
      if (reason) this.emit('error', { message: reason, fatal: true });
      return;
    }
    this.setState('listening');
    this.armIdle();
  }

  stop(): void {
    this.active = false;
    this.muted = false;  // a restarted session always begins unmuted
    this.reconnecting = false;
    this.disarmIdle();
    if (this.flashTimer) clearTimeout(this.flashTimer);
    this.interrupt({ keepListening: true });

    if (this.socket) {
      this.socket.onclose = null;  // deliberate: not a lost connection
      try {
        this.socket.close();
      } catch {
        // already closed
      }
      this.socket = null;
    }
    if (this.echoTailTimer) clearTimeout(this.echoTailTimer);
    // Cleared explicitly, not left to the timers just cancelled. Stopping
    // mid-tail otherwise stranded the microphone soft-muted, with no error and
    // no recovery when the same instance was started again.
    this.sendSuspended = false;
    this.spokenBuffer = '';
    this.finalText = '';
    this.interimText = '';
    this.levels?.close();
    this.levels = null;
    this.mic?.stop();
    this.mic = null;
    this.mode = null;
    this.setState('idle');
  }

  setMuted(muted: boolean): void {
    if (this.muted === muted) return;
    super.setMuted(muted);
    // Capture keeps running either way, so the orb still shows real input —
    // only whether frames leave changes. Unmuting must NOT resume sending while
    // it is still speaking: the echo suspension always wins, and the tail's own
    // resume re-checks the mute state when it lifts.
    this.syncSending();
  }

  sendText(text: string, options: { attachments?: string[] } = {}): void {
    this.finalText = '';
    this.interimText = '';
    this.send(String(text || '').trim(), { source: 'text', attachments: options.attachments });
  }

  // --- recognition: this app's own socket --------------------------------------

  /**
   * Resolves once recognition is genuinely running — the socket says it is ready,
   * after any first-time download of the on-device model — and false on any failure,
   * including a socket that is accepted and then simply never speaks. That last
   * case used never to resolve at all.
   *
   * `silent` is for the reconnect below, which reports one actionable message of
   * its own rather than stacking a generic one underneath it.
   */
  private connect({ silent = false } = {}): Promise<boolean> {
    return new Promise((resolve) => {
      const protocol = location.protocol === 'https:' ? 'wss' : 'ws';
      const socket = new WebSocket(`${protocol}://${location.host}/api/duplex`);
      this.socket = socket;

      let settled = false;
      const settle = (ok: boolean) => {
        // A later close on an already-running session must never retroactively
        // fail the start that succeeded.
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        resolve(ok);
      };
      const giveUp = () => {
        if (!silent) this.emit('error', { message: 'Could not reach Jarvis.' });
        settle(false);
        try {
          socket.close();
        } catch {
          // already closed
        }
      };
      let timer = setTimeout(giveUp, SOCKET_READY_TIMEOUT_MS);

      socket.onmessage = (event) => {
        let message: SocketMessage;
        try {
          message = JSON.parse(event.data);
        } catch {
          return;
        }
        if (message.type === 'downloading' || message.type === 'loading') {
          // The on-device model being fetched (once) or opened: it can take a while,
          // so the ready timeout restarts with every sign of life, and the person
          // sees real progress rather than a silent wait.
          clearTimeout(timer);
          timer = setTimeout(giveUp, SOCKET_READY_TIMEOUT_MS);
          if (!silent) {
            this.emit('listening_progress', { text: describeProgress({
              kind: message.type, label: message.label, done: message.done, total: message.total }) });
          }
          return;
        }
        if (message.type === 'error' && !settled) {
          // Nothing will be recognised on this connection (no key, nothing installed):
          // said in the server's own words, and the start fails.
          if (!silent) this.startFailure = message.error ?? 'Could not start speech recognition.';
          settle(false);
          return;
        }
        if (message.type !== 'ready') {
          this.onRecognitionMessage(message);
          return;
        }
        this.mode = message.mode ?? 'server';
        this.syncSending();
        settle(true);
      };
      socket.onerror = () => {
        if (!silent) this.emit('error', { message: 'Could not reach Jarvis.' });
        settle(false);
      };
      socket.onclose = () => {
        if (settled) {
          if (this.active && this.proxied) void this.reconnect();
          return;
        }
        if (!silent && this.active) {
          this.emit('error', { message: 'Lost the connection to speech recognition.' });
        }
        settle(false);
      };
    });
  }

  /**
   * One bounded retry when an established recognition socket closes.
   *
   * Before this it was a dead end: frames silently went nowhere on a closed
   * socket forever and nothing ever reconnected, which matched a real report
   * exactly — the microphone showing live while speech intermittently stopped
   * being picked up.
   */
  private async reconnect(): Promise<void> {
    if (this.reconnecting) return;  // do not stack attempts on repeated closes
    if (!this.active || !this.proxied) return;
    this.reconnecting = true;
    this.mode = null;  // not a live session until a retry proves otherwise
    if (this.mic) this.mic.onFrame = null;
    const ok = await this.connect({ silent: true });
    this.reconnecting = false;
    if (!this.active) return;  // stopped while the retry was in flight
    if (ok) {
      // A `hearing_speech` left over from before the drop would otherwise wait
      // out the whole idle timer. Deliberately narrow: a drop during a reply has
      // nothing to do with what that reply is doing.
      if (this.state === 'hearing_speech') this.backToListening();
    } else {
      this.emit('error', {
        message: 'Lost speech recognition and could not get it back. '
          + 'Switch the microphone off and on to try again.',
      });
    }
  }

  /** Frames go out only when there is somewhere to send them, nothing is
   *  speaking, and nobody has muted. One place decides all three. */
  private syncSending(): void {
    if (!this.mic) return;
    const shouldSend = this.proxied && !this.muted && !this.sendSuspended;
    this.mic.onFrame = shouldSend ? (frame) => this.sendFrame(frame) : null;
  }

  private sendFrame(frame: string): void {
    if (this.socket?.readyState === WebSocket.OPEN) {
      this.socket.send(JSON.stringify({ type: 'audio', data: frame }));
    }
  }

  /** True while an arriving transcript is still suspect: the short window after
   *  the audio stops, before sending has actually resumed. */
  private echoGuardActive(): boolean {
    return this.speaking || performance.now() < this.echoGuardUntil;
  }

  private onRecognitionMessage(message: SocketMessage): void {
    if (message.type === 'error') {
      this.emit('error', { message: message.error ?? 'Speech recognition failed.', code: message.code });
      return;
    }

    if (message.type === 'transcript') {
      const text = message.text ?? '';
      // The backup layer, behind not sending the audio at all. Its real job is
      // the moment right after speech stops, when the provider may still be
      // finishing audio it received just before frames were held back. An echo
      // is dropped whole: never accumulated, never treated as speech.
      if (this.echoGuardActive() && this.looksLikeEcho(text)) return;
      this.noteActivity();

      if (message.isFinal) {
        const settled = text.trim();
        // A late correction around an internal segment boundary can arrive as a
        // final whose words are already accumulated. Appending it blindly
        // repeats what was already said.
        if (settled && !this.finalText.toLowerCase().includes(settled.toLowerCase())) {
          this.finalText = `${this.finalText} ${settled}`.trim();
        }
      } else {
        this.interimText = text;
      }
      this.emit('transcript', { text: `${this.finalText} ${this.interimText}`.trim(), final: false });
      // `speechFinal` deliberately does NOT end a turn. It marks one CHUNK as
      // stable on a short timer, and an ordinary mid-sentence pause — a breath,
      // a comma, a beat of thought — clears it easily. Treating it as "done
      // talking" got a reply mid-sentence, which was the real cause of two
      // separate reports. The end-of-utterance signal below is the one built
      // for this, on its own longer timer. Text keeps accumulating either way,
      // so nothing is lost while waiting for it.
      return;
    }

    if (message.type === 'utterance_end') {
      if (this.finalText) {
        this.finaliseTurn(message.confidence);
      } else if (this.state === 'hearing_speech') {
        // Real silence with nothing accumulated: a stray sound that never
        // became speech. Without this the state simply sticks.
        this.backToListening();
      }
    }
    // `speech_started` is informational — the transcript itself is what acts.
  }

  private finaliseTurn(confidence?: number): void {
    const text = this.finalText.trim();
    this.finalText = '';
    this.interimText = '';
    if (!text) return;
    this.emit('transcript', { text, final: true });
    this.send(text, { source: 'voice', confidence });
  }

  // --- one turn ---------------------------------------------------------------

  private send(text: string, options: {
    source: string; confidence?: number; attachments?: string[];
  }): void {
    const attachments = options.attachments ?? [];
    // Sharing a file with no words is an ordinary turn, so only bail when there
    // is genuinely nothing to send.
    if (!text && !attachments.length) return;

    this.interrupt({ keepListening: true });  // clear anything left from before
    this.disarmIdle();
    this.setState('thinking');
    // A backstop against a hung-but-open stream — no chunk, no error, no close,
    // just silence. Re-armed on every real sign of progress below.
    this.armStuckWatchdog(() => this.recoverFromStuck());

    let full = '';
    const generation = (this.speakerGeneration += 1);
    // A discarded speaker's late callback must not mutate live state: the free
    // browser voice really can report a start after it was told to stop.
    const onStart = () => {
      if (generation === this.speakerGeneration) this.onSpeechStart();
    };
    const onIdle = () => {
      if (generation === this.speakerGeneration) this.onSpeechIdle();
    };
    // 'browser' is the one special value — the free, offline voice. Anything
    // else is a configured provider's ref, passed straight through; the server
    // resolves which actual voice that means.
    this.speaker = this.voiceOutput === 'browser'
      ? new BrowserSpeaker({ onStart, onIdle })
      : new AudioPlayer({
        onStart,
        onIdle,
        provider: this.voiceOutput,
        onFailure: ({ provider }) => this.emit('tts_failure', { provider }),
      });

    const params = new URLSearchParams({ message: text, source: options.source });
    if (typeof options.confidence === 'number') {
      params.set('confidence', String(options.confidence));
    }
    if (attachments.length) params.set('attachments', attachments.join(','));

    const source = new EventSource(`/api/chat/stream?${params}`);
    this.turnStream = source;

    const progress = () => {
      // The thinking backstop has no job once genuinely speaking. A real,
      // confirmed bug: a chunk for a LATER sentence arriving while an EARLIER
      // one was still playing re-armed this unconditionally, with nothing left
      // to disarm it — if that gap exceeded the timeout it fired and cut off
      // audio that was playing perfectly normally.
      if (this.state !== 'speaking') this.armStuckWatchdog(() => this.recoverFromStuck());
    };

    const finish = () => {
      this.disarmStuckWatchdog();
      this.speaker?.end();
      source.close();
      if (this.turnStream === source) this.turnStream = null;
    };

    source.onmessage = (event) => {
      let data: Record<string, string>;
      try {
        data = JSON.parse(event.data);
      } catch {
        return;
      }

      switch (data.type) {
        case 'chunk':
          // Back from a tool to thinking the moment text flows again; speaking
          // takes over on its own once playback really starts. Deliberately
          // never forced over a genuine `speaking`: later sentences can stream
          // while an earlier one is still audible, and moving the state away
          // mid-playback would simply be untrue.
          if (this.state === 'tool_running') this.setState('thinking');
          progress();
          full += data.text;
          this.spokenBuffer += data.text;
          this.emit('chunk', { text: data.text! });
          this.speaker?.pushText(data.text!);
          break;
        case 'tool_start':
          if (this.state !== 'speaking') this.setState('tool_running');
          progress();
          this.emit('tool', { name: data.name! });
          break;
        case 'tool_result':
          progress();
          this.emit('tool_result', data as never);
          break;
        case 'model_switch':
          // A candidate failing over to the next is real forward progress on
          // the server. This was the one event that used not to re-arm at all —
          // walking a few failed candidates could burn past the timeout with
          // nothing telling the browser anything was still happening.
          progress();
          this.emit('model_switch', data as never);
          break;
        case 'progress':
          // A typed "still working" heartbeat. Re-arming is the whole job.
          progress();
          break;
        case 'style_floors':
          this.emit('style_floors', data as never);
          break;
        case 'reaction': {
          // Into the SAME queue as spoken text, so it lands at the right place
          // in the speech rather than racing whatever is already queued.
          const url = soundFor(data.kind!);
          if (url) this.speaker?.enqueueClip(url);
          break;
        }
        case 'restart':
          progress();
          full = '';
          this.spokenBuffer = '';
          this.speaker?.reset();
          this.emit('restart', {});
          break;
        case 'paused':
          finish();
          this.emit('paused', { reason: data.reason! });
          // If speaking never started, its idle path never runs — go back
          // directly. If it did, leave it: the echo tail is still owed.
          if (!this.speaking) this.backToListening();
          break;
        case 'routed':
          if (data.userMessageId) this.emit('saved', { userMessageId: data.userMessageId });
          break;
        case 'done':
          finish();
          if (data.messageId) this.emit('saved', { replyMessageId: data.messageId });
          this.emit('done', { text: full });
          break;
        case 'approval_required':
          // The turn ends here, asking — the stream closes with no `done`, and
          // treating that close as a failure said "Could not reach Jarvis."
          finish();
          this.emit('approval', { approvalId: data.approvalId!, capability: data.capability!,
                                  reason: data.reason });
          this.emit('done', { text: full });
          break;
        case 'error':
          finish();
          this.emit('error', { message: data.error!, code: data.code, turn: true });
          if (!this.speaking) this.backToListening();
          break;
        default:
          break;
      }
    };

    source.onerror = () => {
      if (this.turnStream !== source) return;
      finish();
      this.emit('error', { message: 'Could not reach Jarvis.', turn: true });
      if (!this.speaking) this.backToListening();
    };
  }

  // --- speaking ---------------------------------------------------------------

  private onSpeechStart(): void {
    this.disarmIdle();
    // The thinking backstop hands off to each sentence's own watchdog. A
    // SECOND, engine-level one is layered above it: the per-sentence watchdog
    // only guarantees any ONE utterance settles, and does nothing if the stream
    // goes silent after its last chunk with no terminal event ever arriving.
    // `onStart` fires per sentence, so re-arming here stays "no progress for
    // this long" and never caps a legitimately long reply.
    this.armStuckWatchdog(() => this.recoverFromStuck(), SPEAKING_STUCK_MS);
    if (this.echoTailTimer) clearTimeout(this.echoTailTimer);
    this.speaking = true;
    this.setState('speaking');
    this.sendSuspended = true;
    this.syncSending();
    this.levels?.reset();
    this.startBargeSampler();
  }

  private onSpeechIdle(): void {
    this.disarmStuckWatchdog();
    this.speaking = false;
    if (this.bargeTimer) clearInterval(this.bargeTimer);
    this.bargeTimer = null;
    // The buffer is deliberately NOT cleared here: transcripts stay suspect for
    // the tail past this point, and the echo check needs it to compare against
    // during that window. The same timer clears it and resumes sending, because
    // both exist for the same reason and end at the same moment.
    this.echoGuardUntil = performance.now() + ECHO_TAIL_MS;
    this.backToListening();
    if (this.echoTailTimer) clearTimeout(this.echoTailTimer);
    this.echoTailTimer = setTimeout(() => {
      this.spokenBuffer = '';
      this.sendSuspended = false;
      this.syncSending();
    }, ECHO_TAIL_MS);
  }

  /**
   * Local loudness is the ONLY signal available while speaking — nothing is
   * being sent to a recogniser, so no transcript can arrive — which makes this
   * firing the interruption itself.
   *
   * Muting is a hard override, and it is a guard here rather than a detector
   * that stops itself and has to be re-armed. The original had to re-arm by
   * hand every time mute suppressed a fire, and forgetting to left barge-in
   * dead for the rest of the reply.
   */
  private startBargeSampler(): void {
    if (this.bargeTimer) clearInterval(this.bargeTimer);
    this.bargeTimer = setInterval(() => {
      if (!this.speaking || this.muted) return;
      if (this.levels?.hasSustainedSpeech(BARGE_SUSTAIN_MS, BARGE_FLOOR)) this.interrupt();
    }, BARGE_SAMPLE_MS);
  }

  private recoverFromStuck(): void {
    console.warn('[voice] nothing happened for a long time — ending this turn.');
    this.emit('error', { message: 'That took too long, so I stopped waiting.', turn: true });
    this.interrupt();
  }

  // --- interrupting -----------------------------------------------------------

  /** Stop the reply mid-flight. `keepListening` skips the flicker back when a
   *  fresh turn is about to start anyway. */
  interrupt({ keepListening = false } = {}): void {
    const wasSpeaking = this.speaking;
    // Invalidates any in-flight callback from the speaker about to be dropped,
    // even before a replacement exists.
    this.speakerGeneration += 1;
    this.disarmStuckWatchdog();
    const heard = this.speaker?.getSpokenText() ?? '';
    this.speaker?.stop();
    this.speaker = null;
    this.turnStream?.close();
    this.turnStream = null;
    this.speaking = false;
    if (this.bargeTimer) clearInterval(this.bargeTimer);
    this.bargeTimer = null;

    if (wasSpeaking) {
      // A real interruption: resume sending at once, so what the person is
      // saying right now is transcribed with no perceptible gap. Suspicion
      // still applies for the tail — the provider may still be finishing audio
      // it received just before frames were held back.
      this.echoGuardUntil = performance.now() + ECHO_TAIL_MS;
      if (this.echoTailTimer) clearTimeout(this.echoTailTimer);
      this.echoTailTimer = setTimeout(() => {
        this.spokenBuffer = '';
      }, ECHO_TAIL_MS);
      this.sendSuspended = false;
      this.syncSending();
      // Best effort, and deliberately so: what this corrects is the NEXT turn's
      // context, so a failed report costs nothing that is happening right now.
      if (heard) {
        void fetch('/api/chat/interrupt', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ spokenText: heard }),
        }).catch(() => {});
      }
    }

    if (keepListening) return;
    if (wasSpeaking) {
      // A brief acknowledgement, purely visible: the microphone is already
      // fully live again by this point.
      if (this.flashTimer) clearTimeout(this.flashTimer);
      this.setState('interrupted');
      this.flashTimer = setTimeout(() => {
        if (this.state === 'interrupted') this.backToListening();
      }, INTERRUPTED_FLASH_MS);
    } else {
      this.backToListening();
    }
  }

  /**
   * Last-resort guard: if what is about to be treated as speech still looks like
   * the assistant's own words, it is echo. Token overlap rather than a raw
   * substring test, which any punctuation or recognition slip defeats.
   */
  private looksLikeEcho(text: string): boolean {
    const normalise = (value: string) =>
      value.toLowerCase().replace(/[^a-z0-9\s]/g, ' ').split(/\s+/).filter(Boolean);
    const said = new Set(normalise(this.spokenBuffer));
    if (said.size < 3) return false;
    const words = normalise(text);
    if (words.length < 2) return false;
    const overlap = words.filter((word) => said.has(word)).length;
    return overlap / words.length >= 0.75;
  }

  // --- resting ----------------------------------------------------------------

  /** The one place every "the turn ended, go back to resting" transition lands. */
  private backToListening(): void {
    if (this.active) {
      this.setState('listening');
      this.armIdle();
    } else {
      this.disarmIdle();
      this.setState('idle');
    }
  }

  /**
   * Real speech activity: wake from a real idle, restart the quiet countdown,
   * and say that someone is actively talking — which is a different thing from
   * a microphone being open with nothing happening.
   */
  private noteActivity(): void {
    if (this.state === 'idle' && this.active) this.setState('listening');
    this.armIdle();
    if (this.state === 'listening') this.setState('hearing_speech');
  }

  private armIdle(): void {
    this.disarmIdle();
    this.idleTimer = setTimeout(() => {
      // `hearing_speech` matters here as much as `listening`: a socket drop
      // between a transcript arriving and its end-of-utterance pinned the state
      // there permanently, and this timer was the one thing that could have
      // caught it and did not.
      if (this.state === 'listening' || this.state === 'hearing_speech') this.setState('idle');
    }, HANDS_FREE_IDLE_MS);
  }

  private disarmIdle(): void {
    if (this.idleTimer) clearTimeout(this.idleTimer);
    this.idleTimer = null;
  }
}

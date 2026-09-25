'use client';

import { VoiceEngine } from './engine';
import { MicStream } from './mic-stream';
import { base64ToBytes, pcm16ToFloat, rmsOf } from './pcm';

/**
 * Engine B — a provider's own speech-to-speech session, relayed by the server.
 *
 * Microphone audio goes up `/api/live` and audio comes back down it, with the
 * provider's own voice-activity detection handling turn-taking and
 * interruption. Roughly 0.3–0.6s to first words, against 0.8–1.5s for the
 * pipeline engine, because nothing has to be transcribed, reasoned about and
 * synthesised in three separate hops.
 *
 * **Nothing here names a provider.** The socket picks whichever connection
 * DECLARED a realtime capability, which is the same rule the picker uses one
 * layer up — so the two can never disagree, and a provider that ships a
 * realtime API later appears by declaring the flag.
 *
 * **Not verified end to end.** Its audio handling — resampling, 16-bit
 * conversion, gapless scheduled playback — is ordinary Web Audio technique and
 * nothing about it is provider-specific, but a real round trip needs a real
 * microphone and a live key, which is the owner's machine.
 *
 * **Microphone audio is NOT held back while it speaks**, unlike the other two
 * engines. Its interruption detection is server-side and depends on hearing the
 * person while its own audio plays; gating the upload would silently disable
 * that entirely, since nothing can detect being talked over in audio it was
 * never sent. Echo is the provider's own problem here, and it is equipped for
 * it in a way a client-side gate cannot be.
 */

/** What the session sends back. Its input rate is set by the server. */
const OUTPUT_SAMPLE_RATE = 24_000;

/** A stalled socket that accepts and then says nothing must not leave the
 *  microphone open under a screen claiming to be listening. */
const READY_TIMEOUT_MS = 8000;

/** The engine-level backstop once audio is genuinely flowing. */
const SPEAKING_STUCK_MS = 60_000;

/** How much of the playback timeline to keep, so it cannot grow without bound
 *  over a long reply. */
const TIMELINE_KEEP_SECONDS = 2;

interface Scheduled {
  playAt: number;
  endAt: number;
  level: number;
}

export class RealtimeEngine extends VoiceEngine {
  private active = false;
  private mic: MicStream | null = null;
  private socket: WebSocket | null = null;
  private micLevel = 0;

  private playback: AudioContext | null = null;
  private nextPlayAt = 0;
  private scheduled = 0;
  /** Invalidates a source scheduled before a stop: closing a context does not
   *  guarantee its pending `ended` callbacks never arrive, and one that lands
   *  late must not decrement the NEW generation's count — which could take it
   *  negative, and a negative count still satisfies "everything has finished". */
  private playbackGeneration = 0;
  private timeline: Scheduled[] = [];

  private turnDone = false;
  private replyText = '';
  private heardText = '';

  /** For the orb: input loudness, 0..1. */
  getMicLevel(): number {
    return this.micLevel;
  }

  /**
   * For the orb: its own voice, 0..1.
   *
   * Read against what is audible NOW rather than returned from the last chunk
   * scheduled — several seconds of audio can be queued ahead, so the loudness
   * of the newest chunk is not the loudness anyone is hearing.
   */
  getOutputLevel(): number {
    if (!this.playback) return 0;
    const now = this.playback.currentTime;
    return this.timeline.find((chunk) => now >= chunk.playAt && now < chunk.endAt)?.level ?? 0;
  }

  // --- session ----------------------------------------------------------------

  async start(): Promise<void> {
    if (this.active) return;
    if (typeof WebSocket === 'undefined') {
      this.emit('error', { message: 'Speaking to Jarvis needs a browser with WebSocket support.' });
      return;
    }

    this.mic = new MicStream();
    try {
      await this.mic.start();
    } catch {
      this.emit('error', {
        message: 'The microphone was blocked. Allow it in your browser and try again.',
      });
      this.mic = null;
      return;
    }

    if (!(await this.connect())) {
      // Whatever failed has already said so — and the microphone is released
      // rather than left open under a screen claiming to be listening. The
      // original resolved on the socket merely OPENING, so a session that could
      // never start (no key) still took the microphone first and reported it
      // afterwards.
      this.stop();
      return;
    }

    this.active = true;
    this.mic.onFrame = (frame) => this.sendAudio(frame);
    this.setState('listening');
  }

  stop(): void {
    this.active = false;
    this.muted = false;  // a restarted session always begins unmuted
    this.disarmStuckWatchdog();
    this.mic?.stop();
    this.mic = null;
    this.micLevel = 0;
    this.stopPlayback();
    if (this.socket) {
      this.socket.onclose = null;  // deliberate: not a lost connection
      try {
        this.socket.close();
      } catch {
        // already closed
      }
      this.socket = null;
    }
    // The same instance can be stopped and started again from the microphone
    // button, so per-turn state is cleared rather than inherited.
    this.turnDone = false;
    this.replyText = '';
    this.heardText = '';
    this.setState('idle');
  }

  /**
   * Muting stops the upload and nothing else — never the socket, the playback,
   * or the state.
   *
   * It does disable the provider's own interruption detection while it holds,
   * which is correct rather than a gap: with the microphone muted it genuinely
   * cannot hear anyone talking over it, exactly as if they had walked away.
   */
  setMuted(muted: boolean): void {
    super.setMuted(muted);
  }

  sendText(text: string): void {
    const message = String(text || '').trim();
    if (!message) return;
    if (this.socket?.readyState !== WebSocket.OPEN) {
      this.emit('error', { message: 'The realtime voice is not connected.' });
      return;
    }
    this.socket.send(JSON.stringify({ type: 'text', text: message }));
    this.setState('thinking');
    // A backstop against the session going quiet after this — no audio, no
    // error, no close. Re-armed on every real sign of progress; handed over to
    // playback's own tracking once audio actually starts.
    this.armStuckWatchdog(() => this.recoverFromStuck());
  }

  /** Clear playback at once for responsiveness. The provider's own detection
   *  reaches the same conclusion server-side a moment later. */
  interrupt(): void {
    this.disarmStuckWatchdog();
    this.stopPlayback();
    this.setState(this.active ? 'listening' : 'idle');
  }

  /**
   * Resolves once the session is genuinely ready, false on any failure —
   * including a socket that is accepted and then never speaks.
   */
  private connect(): Promise<boolean> {
    return new Promise((resolve) => {
      const protocol = location.protocol === 'https:' ? 'wss' : 'ws';
      const socket = new WebSocket(`${protocol}://${location.host}/api/live`);
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
      const timer = setTimeout(() => {
        this.emit('error', { message: 'Could not reach Jarvis.' });
        settle(false);
        try {
          socket.close();
        } catch {
          // already closed
        }
      }, READY_TIMEOUT_MS);

      socket.onmessage = (event) => {
        let message: Record<string, string>;
        try {
          message = JSON.parse(event.data);
        } catch {
          return;
        }
        if (message.type === 'ready') {
          settle(true);
          return;
        }
        // An error BEFORE ready is the session refusing to start at all — no
        // realtime-capable model, or no key for one. It has to fail the start,
        // not surface as a toast over a screen that claims to be listening.
        if (message.type === 'error' && !settled) {
          this.emit('error', { message: message.error!, code: message.code });
          settle(false);
          return;
        }
        this.onMessage(message);
      };
      socket.onerror = () => {
        if (!settled) this.emit('error', { message: 'Could not reach Jarvis.' });
        settle(false);
      };
      socket.onclose = () => {
        if (settled && this.active) {
          this.emit('error', { message: 'Lost the realtime voice connection.' });
          this.stop();
        }
        settle(false);
      };
    });
  }

  private sendAudio(frame: string): void {
    this.micLevel = this.mic?.getLevel() ?? 0;
    // Muted means it genuinely cannot hear — nothing goes up. The level above
    // still updates, so the orb keeps showing real input, as on every engine.
    if (this.muted) return;
    if (this.socket?.readyState !== WebSocket.OPEN) return;
    this.socket.send(JSON.stringify({ type: 'audio', data: frame }));
  }

  // --- what the session says --------------------------------------------------

  private onMessage(message: Record<string, string>): void {
    /** Forward progress. Never re-armed over a genuine `speaking`: a tool call
     *  or later text can arrive while earlier audio is still playing, and
     *  re-arming there left a timer live with nothing to disarm it until the
     *  turn completed — long enough, sometimes, to cut off audio that was
     *  playing perfectly normally. */
    const progress = () => {
      if (this.state !== 'speaking') this.armStuckWatchdog(() => this.recoverFromStuck());
    };

    switch (message.type) {
      case 'error':
        this.disarmStuckWatchdog();
        this.stopPlayback();
        this.setState(this.active ? 'listening' : 'idle');
        this.emit('error', { message: message.error!, code: message.code });
        break;
      case 'tool_start':
        progress();
        this.emit('tool', { name: message.name! });
        break;
      case 'tool_result':
        progress();
        this.emit('tool_result', message as never);
        break;
      case 'transcript_in':
        // There is no distinct "final" marker for what was heard — the natural
        // boundary is the moment the reply begins, so it is buffered and
        // settled there.
        this.heardText += message.text;
        this.emit('transcript', { text: this.heardText, final: false });
        break;
      case 'transcript_out':
        progress();
        this.settleHeard();
        this.replyText += message.text;
        this.emit('chunk', { text: message.text! });
        break;
      case 'audio':
        // Scheduled playback's own completion tracking takes over from the
        // thinking backstop here. A second, engine-level backstop is armed
        // above it: nothing else would ever recover if the session went silent
        // after its last chunk with no completion, error or close arriving.
        this.armStuckWatchdog(() => this.recoverFromStuck(), SPEAKING_STUCK_MS);
        this.settleHeard();
        this.playChunk(message.data!);
        this.setState('speaking');
        break;
      case 'interrupted':
        // The provider's own detection heard someone talking over it. Nothing
        // here can tell a real interruption from its detection over-triggering
        // on its own echo; if replies ever stop early on this engine, this is
        // the thing to look for.
        console.info('[voice] the realtime session reported an interruption.');
        this.disarmStuckWatchdog();  // a real, legitimate end to this turn
        this.stopPlayback();
        this.setState(this.active ? 'listening' : 'idle');
        break;
      case 'turn_complete':
        this.turnDone = true;
        this.maybeFinishTurn();
        break;
      default:
        break;
    }
  }

  /** What was heard becomes permanent right as the reply begins. */
  private settleHeard(): void {
    const text = this.heardText.trim();
    this.heardText = '';
    if (text) this.emit('transcript', { text, final: true });
  }

  private maybeFinishTurn(): void {
    if (!this.turnDone || this.scheduled > 0) return;
    // Covers a turn that finished without ever producing audio — a tool-only
    // one — where nothing else would have disarmed this.
    this.disarmStuckWatchdog();
    this.emit('done', { text: this.replyText });
    this.replyText = '';
    this.heardText = '';
    this.turnDone = false;
    this.setState(this.active ? 'listening' : 'idle');
  }

  private recoverFromStuck(): void {
    // Deliberately not `interrupt()`: a genuinely stuck turn can have partial
    // text accumulated, which that does not clear, and `stop()` — which does —
    // would tear down the whole session rather than this one turn.
    console.warn('[voice] nothing happened for a long time — ending this turn.');
    this.emit('error', { message: 'That took too long, so I stopped waiting.' });
    this.stopPlayback();
    this.turnDone = false;
    this.replyText = '';
    this.heardText = '';
    this.setState(this.active ? 'listening' : 'idle');
  }

  // --- playback ---------------------------------------------------------------

  private ensurePlayback(): AudioContext {
    if (this.playback) return this.playback;
    const context = new AudioContext({ sampleRate: OUTPUT_SAMPLE_RATE });
    this.playback = context;
    this.nextPlayAt = context.currentTime;
    // This context is created inside a socket message handler, not a click, so
    // autoplay policy can hand it back suspended. A source scheduled on a
    // suspended context never plays and never reports finishing, so the count
    // never drains and the turn never completes — speaking forever, in silence.
    if (context.state === 'suspended') {
      void context.resume().catch((err) => {
        console.warn('[voice] could not resume the playback context:', err);
      });
    }
    return context;
  }

  private playChunk(encoded: string): void {
    const context = this.ensurePlayback();
    const bytes = base64ToBytes(encoded);
    const samples = pcm16ToFloat(
      new Int16Array(bytes.buffer, bytes.byteOffset, bytes.byteLength / 2),
    );

    const buffer = context.createBuffer(1, samples.length, OUTPUT_SAMPLE_RATE);
    buffer.copyToChannel(samples, 0);
    const source = context.createBufferSource();
    source.buffer = buffer;
    source.connect(context.destination);

    const startAt = Math.max(this.nextPlayAt, context.currentTime);
    source.start(startAt);
    this.nextPlayAt = startAt + buffer.duration;

    this.timeline.push({ playAt: startAt, endAt: startAt + buffer.duration, level: rmsOf(samples) });
    const cutoff = context.currentTime - TIMELINE_KEEP_SECONDS;
    this.timeline = this.timeline.filter((chunk) => chunk.endAt >= cutoff);

    this.scheduled += 1;
    const generation = this.playbackGeneration;
    source.onended = () => {
      if (generation !== this.playbackGeneration) return;
      this.scheduled -= 1;
      this.maybeFinishTurn();
    };
  }

  private stopPlayback(): void {
    this.playbackGeneration += 1;
    void this.playback?.close().catch(() => {});
    this.playback = null;
    this.nextPlayAt = 0;
    this.scheduled = 0;
    this.timeline = [];
  }
}

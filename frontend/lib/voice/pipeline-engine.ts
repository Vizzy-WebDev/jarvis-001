'use client';

import { AudioPlayer } from './audio-player';
import { BrowserSpeaker } from './browser-speaker';
import { VoiceEngine } from './engine';
import { MicStream } from './mic-stream';
import { soundFor } from './reaction-sounds';
import { describeProgress, RecognitionSocket } from './recognition-socket';
import { computeWaitMs, MicLevelMonitor, SilenceWatcher } from './turn-detector';

/**
 * Engine A ("Speak and listen") — works with any model.
 *
 * One microphone capture (`MicStream`, echo-cancelled) whose frames go to this app's own
 * recognition socket (`RecognitionSocket`: on this computer or the chosen cloud service —
 * never the browser's built-in recognition). The reply streams from the ordinary chat
 * stream, and speech comes from either the browser's own voice or a configured provider.
 * Turn-taking and barge-in are handled here.
 *
 * Expect roughly 0.8–1.5s to first words: text has to stream far enough for a
 * speakable chunk before audio can start. The realtime engine is the faster
 * alternative, when a model that offers one is connected.
 */

/**
 * How long after the audio physically stops before frames are sent again. Recognition
 * runs a little behind real time, so audio captured DURING playback can otherwise surface
 * as a transcript AFTER the speaking guard has already cleared — becoming a phantom turn
 * the instant the assistant finishes.
 */
const ECHO_TAIL_MS = 700;

/**
 * Barge-in is sampled on a fixed clock, not "whenever a recognition result
 * happens to arrive" — sampled sporadically, two loud instants seconds apart
 * can look like one continuous utterance. No floor-learning warm-up: an earlier
 * version spent ~300ms learning the room before it would even arm, which
 * roughly doubled how long interrupting took. This floor already sits well
 * above ordinary residual echo.
 */
const BARGE_SAMPLE_MS = 100;
const BARGE_SUSTAIN_MS = 250;
const BARGE_FLOOR = 0.05;

/** How long a hands-free session may sit quiet in `listening`, with nothing said
 *  yet, before it genuinely stops: recognition is torn down and the microphone
 *  released — the same as a manual stop, not a relabelled display. This is the
 *  "just clicked the mic, still deciding what to say" window, deliberately more
 *  patient than FOLLOWUP_GRACE_MS below. */
const HANDS_FREE_IDLE_MS = 30_000;

/** How long the microphone stays open for a quick follow-up right after Jarvis
 *  finishes a reply, before the same real stop happens. Deliberately much
 *  shorter than HANDS_FREE_IDLE_MS: this window is for someone mid-conversation
 *  who is likely to keep talking immediately, not someone who just sat down and
 *  hasn't said anything yet. */
const FOLLOWUP_GRACE_MS = 7_000;

/** The engine-level backstop while speaking, above each sentence's own. */
const SPEAKING_STUCK_MS = 60_000;

type Speaker = AudioPlayer | BrowserSpeaker;

export class PipelineEngine extends VoiceEngine {
  private active = false;
  /** The one microphone capture; its frames go to `recognizer`. */
  private capture: MicStream | null = null;
  private mic: MicLevelMonitor | null = null;
  private silence: SilenceWatcher | null = null;
  private recognizer: RecognitionSocket | null = null;
  /** Frames are held back while Jarvis speaks (plus a tail) — see ECHO_TAIL_MS. */
  private recognitionSuspended = false;
  /** Words recognised but not yet settled into `pendingUtterance`. */
  private interim = '';
  private speaker: Speaker | null = null;
  /** Invalidates a discarded speaker's late callbacks — see `interrupt`. */
  private speakerGeneration = 0;
  private stream_: EventSource | null = null;
  private speaking = false;
  private spokenBuffer = '';
  private pendingUtterance = '';
  private pendingConfidence: number | null = null;
  private finalizeInFlight = false;
  private idleTimer: ReturnType<typeof setTimeout> | null = null;
  private echoTailTimer: ReturnType<typeof setTimeout> | null = null;
  private bargeTimer: ReturnType<typeof setInterval> | null = null;

  constructor(private readonly options: {
    voiceOutput?: string;
    /** Test-only overrides for the two idle timers below, so a test can prove
     *  real teardown happens without waiting out the real durations. Always
     *  unset in production. */
    handsFreeIdleMs?: number;
    followupGraceMs?: number;
  } = {}) {
    super();
  }

  get voiceOutput(): string {
    return this.options.voiceOutput || 'browser';
  }

  private get handsFreeIdleMs(): number {
    return this.options.handsFreeIdleMs ?? HANDS_FREE_IDLE_MS;
  }

  private get followupGraceMs(): number {
    return this.options.followupGraceMs ?? FOLLOWUP_GRACE_MS;
  }

  async start(): Promise<void> {
    if (this.active) return;
    const capture = new MicStream();
    try {
      await capture.start();
    } catch {
      capture.stop();
      this.emit('error', {
        message: 'The microphone was blocked. Allow it in your browser and try again.',
        fatal: true,
      });
      return;
    }

    this.active = true;
    this.capture = capture;
    this.mic = new MicLevelMonitor(capture.getRawStream()!);
    this.silence = new SilenceWatcher(this.mic);

    let failure: string | null = null;
    const recognizer = new RecognitionSocket({
      onTranscript: (text, isFinal, _speechFinal, confidence) => this.onTranscript(text, isFinal, confidence),
      onProgress: (progress) => this.emit('listening_progress', { text: describeProgress(progress) }),
      onError: (message, fatal) => {
        if (fatal && !recognizer.mode) failure = message;   // reported once start() gives up
        else this.emit('error', { message });
      },
      onLost: () => {
        if (this.recognizer !== recognizer) return;
        this.stop();
        this.emit('error', { message: 'Lost the connection to speech recognition. Tap the mic to talk again.' });
      },
    });
    this.recognizer = recognizer;
    capture.onFrame = (frame) => {
      if (this.active && !this.recognitionSuspended && !this.muted) recognizer.send(frame);
    };
    const ok = await recognizer.connect();
    if (this.recognizer !== recognizer) return;      // stopped while it was getting ready
    if (!ok) {
      // stop() first: it ends in setState('idle'), which sets a generic status text;
      // the specific message must be the last write.
      this.stop();
      this.emit('error', { message: failure ?? 'Could not start speech recognition.', fatal: true });
      return;
    }
    this.setState('listening');
    this.armIdle();
  }

  stop(): void {
    this.active = false;
    this.muted = false;  // a restarted session always begins unmuted
    this.disarmIdle();
    this.silence?.cancel();
    if (this.echoTailTimer) clearTimeout(this.echoTailTimer);
    if (this.bargeTimer) clearInterval(this.bargeTimer);
    this.pendingUtterance = '';
    this.interrupt({ keepListening: true });  // silence any reply in progress

    this.recognizer?.close();
    this.recognizer = null;
    this.recognitionSuspended = false;
    this.interim = '';
    // Releasing the microphone is the point of stopping: the capture's tracks are ended
    // here, so the browser's "in use" indicator goes out with the session.
    this.capture?.stop();
    this.capture = null;
    this.mic?.close();
    this.mic = null;
    this.silence = null;
    this.setState('idle');
  }

  sendText(text: string, options: { attachments?: string[] } = {}): void {
    this.silence?.cancel();
    this.pendingUtterance = '';
    // Typed text cannot be misheard, so it is always full confidence — the
    // clarity guard only ever gates spoken input.
    this.send(String(text || '').trim(), { source: 'text', attachments: options.attachments });
  }

  /**
   * Stop the reply mid-flight. `keepListening` skips the flicker back to
   * listening when a fresh turn is about to start anyway.
   */
  interrupt({ keepListening = false, resumeRecognition = true } = {}): void {
    // Invalidates any in-flight callback from the speaker about to be dropped,
    // even before a new one exists.
    this.speakerGeneration += 1;
    this.disarmStuckWatchdog();
    this.speaker?.stop();
    this.speaker = null;
    this.stream_?.close();
    this.stream_ = null;
    this.speaking = false;
    this.spokenBuffer = '';
    if (this.bargeTimer) clearInterval(this.bargeTimer);
    this.bargeTimer = null;
    // Whatever triggered this interrupt must not be finalised and sent a moment
    // later. A real bug: an interrupt caused by the assistant's own echo used to
    // clear the echo detector and then still send the echo as the next turn.
    this.silence?.cancel();
    this.pendingUtterance = '';
    this.interim = '';
    this.pendingConfidence = null;
    this.finalizeInFlight = false;
    if (resumeRecognition) this.resumeRecognition();
    if (!keepListening) this.backToListening();
  }

  // Muting needs nothing more here: frames are only sent while unmuted (`capture.onFrame`).

  // --- listening --------------------------------------------------------------

  /** No frames leave while the assistant is talking, so an echo can never become
   *  text — the narrowest window that still fixes self-listening. */
  private suspendRecognition(): void {
    if (this.echoTailTimer) clearTimeout(this.echoTailTimer);
    this.echoTailTimer = null;
    this.recognitionSuspended = true;
  }

  private resumeRecognition(): void {
    if (this.echoTailTimer) clearTimeout(this.echoTailTimer);
    this.echoTailTimer = null;
    this.recognitionSuspended = false;
  }

  private onTranscript(text: string, isFinal: boolean, confidence?: number): void {
    // Nothing is sent while speaking or muted, but a result for audio sent just before can
    // still arrive: belt and braces, so it can never be treated as something someone said.
    if (this.recognitionSuspended || this.speaking || this.muted) return;

    // Real speech heard: restart the quiet countdown. `idle` genuinely means the session
    // was torn down via stop(), so there is nothing left to "wake" here.
    this.armIdle();

    if (isFinal) {
      this.pendingUtterance = `${this.pendingUtterance} ${text}`.trim();
      this.interim = '';
      // Keep the LOWEST confidence seen across the utterance — the conservative choice,
      // so one garbled piece in an otherwise clear sentence still trips the clarity guard.
      // The on-device recogniser reports none.
      if (typeof confidence === 'number' && !Number.isNaN(confidence)) {
        this.pendingConfidence = this.pendingConfidence === null
          ? confidence
          : Math.min(this.pendingConfidence, confidence);
      }
    } else {
      this.interim = text;
    }

    const preview = `${this.pendingUtterance} ${this.interim}`.trim();
    this.emit('transcript', { text: preview, final: false });

    // The wait DURATION still comes from how the sentence trails off; what the
    // watcher changes is that it is measured against genuine continuous silence
    // rather than time since the recogniser last spoke up.
    this.silence?.arm(computeWaitMs(preview), () => this.finalise(preview));
  }

  /**
   * Last-resort guard on the send path: if what is about to be sent still looks
   * like the assistant's own words, drop it. Token overlap rather than a raw
   * substring test, which any punctuation or recognition slip defeated. Not
   * load-bearing now that recognition is suspended for the whole speaking
   * window, but cheap insurance.
   */
  private looksLikeEcho(text: string): boolean {
    const normalise = (value: string) =>
      value.toLowerCase().replace(/[^a-z0-9\s]/g, ' ').split(/\s+/).filter(Boolean);
    const said = new Set(normalise(this.spokenBuffer));
    if (said.size < 3) return false;
    const words = normalise(text);
    if (words.length < 3) return false;
    const overlap = words.filter((word) => said.has(word)).length;
    return overlap / words.length > 0.6;
  }

  private finalise(preview: string): void {
    // Guards against being entered twice for overlapping speech.
    if (this.finalizeInFlight) return;
    this.finalizeInFlight = true;

    const text = preview.trim();
    this.pendingUtterance = '';
    this.interim = '';
    if (!text) {
      this.finalizeInFlight = false;
      return;
    }
    if (this.looksLikeEcho(text)) {
      this.pendingConfidence = null;
      this.finalizeInFlight = false;
      return;
    }

    const confidence = this.pendingConfidence;
    this.pendingConfidence = null;
    this.emit('transcript', { text, final: true });
    this.finalizeInFlight = false;
    this.send(text, { source: 'voice', confidence: confidence ?? undefined });
  }

  // --- one turn ---------------------------------------------------------------

  private send(text: string, options: {
    source: string; confidence?: number; attachments?: string[];
  }): void {
    const attachments = options.attachments ?? [];
    // A file shared with no words is a normal turn, so only bail when there is
    // genuinely nothing to send.
    if (!text && !attachments.length) return;

    this.interrupt({ keepListening: true });
    this.disarmIdle();
    this.setState('thinking');
    // A backstop against a hung-but-open stream — no chunk, no error, no close,
    // just silence — leaving this engine thinking forever. Re-armed on every
    // real sign of progress below.
    this.armStuckWatchdog(() => this.recoverFromStuck());

    // Frames deliberately keep flowing through thinking: nothing is playing,
    // so there is no echo risk, and this is what lets someone keep talking or
    // change their mind before a word has been said.

    let full = '';
    const generation = (this.speakerGeneration += 1);
    const onStart = () => {
      if (generation === this.speakerGeneration) this.onSpeechStart();
    };
    const onIdle = () => {
      if (generation === this.speakerGeneration) this.onSpeechIdle();
    };
    // 'browser' is the one special value — the free, offline voice. Anything
    // else is a configured provider's ref, passed straight through.
    this.speaker = this.voiceOutput === 'browser'
      ? new BrowserSpeaker({ onStart, onIdle })
      : new AudioPlayer({
        onStart,
        onIdle,
        provider: this.voiceOutput,
        // Fires once per reply, only after every retry for a sentence has
        // failed. Deliberately not an error: the reply itself is fine, and
        // reporting it as an error would clear what is on screen.
        onFailure: ({ provider }) => this.emit('tts_failure', { provider }),
      });

    const params = new URLSearchParams({ message: text, source: options.source });
    if (typeof options.confidence === 'number') {
      params.set('confidence', String(options.confidence));
    }
    // Upload ids, never paths — the server re-validates each one. They ride in
    // the query string because an event stream is a GET with no body.
    if (attachments.length) params.set('attachments', attachments.join(','));

    const source = new EventSource(`/api/chat/stream?${params}`);
    this.stream_ = source;

    const progress = () => {
      // The thinking backstop has no job once genuinely speaking. A real,
      // confirmed bug: a chunk for a LATER sentence arriving while an EARLIER
      // one was still playing re-armed this unconditionally, with nothing left
      // to disarm it — if that gap exceeded the timeout it fired and cut off
      // audio that was playing perfectly normally. That was the root cause of a
      // reported "replies cut off mid-sentence, not by me".
      if (this.state !== 'speaking') this.armStuckWatchdog(() => this.recoverFromStuck());
    };

    const finish = () => {
      this.disarmStuckWatchdog();
      this.speaker?.end();
      source.close();
      if (this.stream_ === source) this.stream_ = null;
    };

    source.onmessage = (message) => {
      // The same staleness guard onerror below already has: a message queued
      // for a just-superseded stream (interrupt() replaced this.stream_ with a
      // newer turn's) must not be acted on as if it belonged to the current one.
      if (this.stream_ !== source) return;

      let data: Record<string, string>;
      try {
        data = JSON.parse(message.data);
      } catch {
        return;
      }

      switch (data.type) {
        case 'chunk':
          progress();
          full += data.text;
          this.spokenBuffer += data.text;
          this.emit('chunk', { text: data.text! });
          this.speaker?.pushText(data.text!);
          break;
        case 'tool_start':
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
          // A typed "still working" heartbeat, for when neither a chunk nor a
          // tool event has happened in a while but the server genuinely is.
          // Re-arming is the whole job; there is no event to pass on.
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
          // If speaking never started, the idle path will never run — resume
          // directly, or the mic stays suspended indefinitely. If it did start,
          // leave it, so the echo tail is still respected.
          if (!this.speaking) this.resumeRecognition();
          this.emit('paused', { reason: data.reason! });
          this.backToListening();
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
          if (!this.speaking) this.resumeRecognition();
          this.emit('error', { message: data.error!, code: data.code, turn: true });
          this.backToListening();
          break;
        default:
          break;
      }
    };

    source.onerror = () => {
      if (this.stream_ !== source) return;
      finish();
      if (!this.speaking) this.resumeRecognition();
      this.emit('error', { message: 'Could not reach Jarvis.', turn: true });
      this.backToListening();
    };
  }

  // --- speaking ---------------------------------------------------------------

  private onSpeechStart(): void {
    this.disarmIdle();
    // The thinking backstop hands off to each sentence's own watchdog now.
    // A SECOND, engine-level backstop is layered on top: the per-sentence one
    // only guarantees any ONE utterance settles, and does nothing if the stream
    // goes silent after the last chunk with no terminal event ever arriving.
    // `onStart` fires per sentence, so re-arming here is still "no progress for
    // this long", never a cap on a legitimately long reply.
    this.armStuckWatchdog(() => this.recoverFromStuck(), SPEAKING_STUCK_MS);
    this.speaking = true;
    this.setState('speaking');
    // Off only now that audio is really playing: an echo can then never become
    // text, without killing the mic during thinking.
    this.suspendRecognition();
    this.mic?.reset();
    this.startBargeSampler();
  }

  private onSpeechIdle(): void {
    this.disarmStuckWatchdog();
    this.speaking = false;
    this.spokenBuffer = '';
    if (this.bargeTimer) clearInterval(this.bargeTimer);
    this.bargeTimer = null;
    // Just replied: a short follow-up window rather than the long first-listen
    // one, so a hands-free back-and-forth doesn't sit fully open indefinitely.
    this.backToListening(this.followupGraceMs);
    // Recognition stays suspended a little past the audio itself, and anything
    // the mic queued during that last stretch is dropped so it cannot be
    // finalised once recognition resumes.
    if (this.echoTailTimer) clearTimeout(this.echoTailTimer);
    this.echoTailTimer = setTimeout(() => {
      this.silence?.cancel();
      this.pendingUtterance = '';
      this.interim = '';
      this.pendingConfidence = null;
      this.resumeRecognition();
    }, ECHO_TAIL_MS);
  }

  private startBargeSampler(): void {
    if (this.bargeTimer) clearInterval(this.bargeTimer);
    this.bargeTimer = setInterval(() => {
      if (!this.speaking || this.muted) return;
      if (this.mic?.hasSustainedSpeech(BARGE_SUSTAIN_MS, BARGE_FLOOR)) {
        this.setState('interrupted');
        this.interrupt();
      }
    }, BARGE_SAMPLE_MS);
  }

  private recoverFromStuck(): void {
    console.warn('[voice] nothing happened for a long time — ending this turn.');
    this.emit('error', { message: 'That took too long, so I stopped waiting.', turn: true });
    this.interrupt();
  }

  // --- resting ----------------------------------------------------------------

  /**
   * The one place every "the turn ended, go back to resting" transition lands.
   * `graceMs` is how long to wait before genuinely stopping — callers that just
   * finished a reply pass the short follow-up window; every other caller (barge-in
   * recovery, error paths) takes the default first-listen duration.
   */
  private backToListening(graceMs: number = this.handsFreeIdleMs): void {
    if (this.active) {
      this.setState('listening');
      this.armIdle(graceMs);
    } else {
      this.disarmIdle();
      this.setState('idle');
    }
  }

  private armIdle(ms: number = this.handsFreeIdleMs): void {
    this.disarmIdle();
    this.idleTimer = setTimeout(() => {
      // Only if still genuinely resting: if something else happened since, this
      // timer is stale and the state change that caused it already disarmed it.
      // Genuinely stops now — the same teardown a manual click triggers — rather
      // than only relabelling the display while recognition kept running.
      if (this.state === 'listening') {
        this.stop();
        this.emit('paused', { reason: 'Stopped listening — tap the mic to talk again.' });
      }
    }, ms);
  }

  private disarmIdle(): void {
    if (this.idleTimer) clearTimeout(this.idleTimer);
    this.idleTimer = null;
  }
}

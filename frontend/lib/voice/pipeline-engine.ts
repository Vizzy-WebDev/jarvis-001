'use client';

import { AudioPlayer } from './audio-player';
import { BrowserSpeaker } from './browser-speaker';
import { VoiceEngine } from './engine';
import { soundFor } from './reaction-sounds';
import { computeWaitMs, MicLevelMonitor, SilenceWatcher } from './turn-detector';

/**
 * Engine A — works with any model.
 *
 * Recognition happens in the browser, the reply streams from the ordinary chat
 * stream, and speech comes from either the browser's own voice or a configured
 * provider. Turn-taking and barge-in are handled here.
 *
 * Expect roughly 0.8–1.5s to first words: text has to stream far enough for a
 * speakable chunk before audio can start. The realtime engine is the faster
 * alternative, when a model that offers one is connected.
 */

/**
 * How long after the audio physically stops before recognition resumes. Cloud
 * recognition runs several hundred milliseconds behind real time, so audio
 * captured DURING playback can otherwise surface as a transcript AFTER the
 * speaking guard has already cleared — becoming a phantom turn the instant the
 * assistant finishes.
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

/** How long a hands-free session may sit quiet in `listening` before dropping to
 *  a real `idle`. The mic stays fully open — only the state changes, so the orb
 *  stops claiming to be listening intently at nothing. */
const HANDS_FREE_IDLE_MS = 30_000;

/** The engine-level backstop while speaking, above each sentence's own. */
const SPEAKING_STUCK_MS = 60_000;

type Speaker = AudioPlayer | BrowserSpeaker;

export class PipelineEngine extends VoiceEngine {
  private active = false;
  private stream: MediaStream | null = null;
  private mic: MicLevelMonitor | null = null;
  private silence: SilenceWatcher | null = null;
  private recognition: SpeechRecognition | null = null;
  private recognitionSuspended = false;
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

  constructor(private readonly options: { voiceOutput?: string } = {}) {
    super();
  }

  get voiceOutput(): string {
    return this.options.voiceOutput || 'browser';
  }

  async start(): Promise<void> {
    if (this.active) return;
    const Recognition = window.SpeechRecognition ?? window.webkitSpeechRecognition;
    if (!Recognition) {
      this.emit('error', { message: 'Speaking to Jarvis needs Chrome or Edge.' });
      return;
    }
    try {
      this.stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      });
    } catch {
      this.emit('error', {
        message: 'The microphone was blocked. Allow it in your browser and try again.',
      });
      return;
    }

    this.active = true;
    this.mic = new MicLevelMonitor(this.stream);
    this.silence = new SilenceWatcher(this.mic);
    this.startRecognition(Recognition);
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

    if (this.recognition) {
      this.recognition.onend = null;  // do not auto-restart what we are stopping
      try {
        this.recognition.stop();
      } catch {
        /* already stopped */
      }
      this.recognition = null;
    }
    this.stream?.getTracks().forEach((track) => track.stop());
    this.stream = null;
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
    this.pendingConfidence = null;
    this.finalizeInFlight = false;
    if (resumeRecognition) this.resumeRecognition();
    if (!keepListening) this.backToListening();
  }

  setMuted(muted: boolean): void {
    super.setMuted(muted);
    this.syncRecognition();
  }

  // --- listening --------------------------------------------------------------

  private startRecognition(Recognition: typeof SpeechRecognition): void {
    const recognition = new Recognition();
    recognition.continuous = true;
    recognition.interimResults = true;
    recognition.lang = 'en-US';
    recognition.onresult = (event) => this.onResult(event);
    recognition.onerror = (event) => this.onRecognitionError(event);
    recognition.onend = () => {
      // Continuous recognition still ends on its own; restart unless this
      // engine deliberately stopped or is currently suspended.
      if (this.active && !this.recognitionSuspended && !this.muted) {
        try {
          recognition.start();
        } catch {
          /* a start is already pending */
        }
      }
    };
    recognition.start();
    this.recognition = recognition;
  }

  /** Off entirely while the assistant is talking, so an echo can never become
   *  text — the narrowest window that still fixes self-listening. */
  private suspendRecognition(): void {
    if (this.echoTailTimer) clearTimeout(this.echoTailTimer);
    this.echoTailTimer = null;
    if (this.recognitionSuspended) return;
    this.recognitionSuspended = true;
    this.syncRecognition();
  }

  private resumeRecognition(): void {
    if (this.echoTailTimer) clearTimeout(this.echoTailTimer);
    this.echoTailTimer = null;
    if (!this.recognitionSuspended) return;
    this.recognitionSuspended = false;
    this.syncRecognition();
  }

  private syncRecognition(): void {
    if (!this.recognition) return;
    const shouldRun = this.active && !this.recognitionSuspended && !this.muted;
    try {
      if (shouldRun) this.recognition.start();
      else this.recognition.stop();
    } catch {
      /* already in the requested state */
    }
  }

  private onRecognitionError(event: SpeechRecognitionErrorEvent): void {
    if (event.error === 'no-speech' || event.error === 'aborted') return;  // ordinary
    if (event.error === 'not-allowed' || event.error === 'service-not-allowed') {
      this.emit('error', { message: 'The microphone was blocked. Allow it and try again.' });
      this.stop();
    } else if (event.error === 'network') {
      this.emit('error', { message: 'Speech recognition needs an internet connection.' });
    } else {
      this.emit('error', { message: `Microphone problem: ${event.error}` });
    }
  }

  private onResult(event: SpeechRecognitionEvent): void {
    // Recognition is stopped for the whole speaking window and while muted, but
    // a result already in the pipeline when it was stopped can still arrive:
    // belt and braces, so it can never be treated as something someone said.
    if (this.recognitionSuspended || this.speaking || this.muted) return;

    // Real activity: wake from a hands-free rest (the mic was never off, only
    // the displayed state) and restart the quiet countdown.
    if (this.state === 'idle' && this.active) this.setState('listening');
    this.armIdle();

    let interim = '';
    let settled = '';
    for (let i = event.resultIndex; i < event.results.length; i += 1) {
      const result = event.results[i]!;
      const text = result[0]!.transcript;
      if (result.isFinal) {
        settled += text;
        // Confidence is only meaningful on a final result. Keep the LOWEST seen
        // across the utterance — the conservative choice, so one garbled chunk
        // in an otherwise clear sentence still trips the clarity guard.
        const confidence = result[0]!.confidence;
        if (typeof confidence === 'number' && !Number.isNaN(confidence)) {
          this.pendingConfidence = this.pendingConfidence === null
            ? confidence
            : Math.min(this.pendingConfidence, confidence);
        }
      } else {
        interim += text;
      }
    }
    if (settled) this.pendingUtterance += settled;

    const preview = `${this.pendingUtterance} ${interim}`.trim();
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

    // Recognition deliberately stays LIVE through thinking: nothing is playing,
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
        case 'done':
          finish();
          this.emit('done', { text: full });
          break;
        case 'error':
          finish();
          if (!this.speaking) this.resumeRecognition();
          this.emit('error', { message: data.error!, code: data.code });
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
      this.emit('error', { message: 'Could not reach Jarvis.' });
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
    this.backToListening();
    // Recognition stays suspended a little past the audio itself, and anything
    // the mic queued during that last stretch is dropped so it cannot be
    // finalised once recognition resumes.
    if (this.echoTailTimer) clearTimeout(this.echoTailTimer);
    this.echoTailTimer = setTimeout(() => {
      this.silence?.cancel();
      this.pendingUtterance = '';
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
    this.emit('error', { message: 'That took too long, so I stopped waiting.' });
    this.interrupt();
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

  private armIdle(): void {
    this.disarmIdle();
    this.idleTimer = setTimeout(() => {
      // Only if still genuinely resting: if something else happened since, this
      // timer is stale and the state change that caused it already disarmed it.
      if (this.state === 'listening') this.setState('idle');
    }, HANDS_FREE_IDLE_MS);
  }

  private disarmIdle(): void {
    if (this.idleTimer) clearTimeout(this.idleTimer);
    this.idleTimer = null;
  }
}

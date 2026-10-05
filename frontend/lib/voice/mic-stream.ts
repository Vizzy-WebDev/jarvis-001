'use client';

import { frameToBase64, rmsOf } from './pcm';

/**
 * One owned microphone capture, delivering ready-to-send frames.
 *
 * The engine above decides where a frame goes — a recognition socket, a
 * realtime session, or nowhere — and this class has no opinion about it. That
 * split is why the same capture serves both sockets.
 *
 * **There is exactly ONE capture here, taken with the browser's own echo
 * cancellation.** That is what makes this different from the pipeline engine,
 * where the browser's built-in recognition opens a second, unprocessed capture
 * that `echoCancellation: true` never reaches — the whole reason that engine has
 * to suspend recognition while the assistant speaks.
 */
export class MicStream {
  private stream: MediaStream | null = null;
  private context: AudioContext | null = null;
  private source: MediaStreamAudioSourceNode | null = null;
  private processor: ScriptProcessorNode | null = null;
  private level = 0;

  /**
   * Called for every frame, loud or silent — what to do about silence is the
   * caller's decision, not this class's. Set to null to stop sending without
   * stopping the capture: that is how muting works, and it is why the level
   * above keeps updating while muted.
   */
  onFrame: ((base64: string) => void) | null = null;

  /** Throws when permission is denied or there is no device — the caller words
   *  the message, because only it knows what the person was trying to do. */
  async start(): Promise<void> {
    this.stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });

    this.context = new AudioContext();
    this.source = this.context.createMediaStreamSource(this.stream);

    // A script processor is deprecated but universally supported, and an audio
    // worklet means shipping a second module file for a first version of this.
    // Worth revisiting if a browser ever actually drops it.
    this.processor = this.context.createScriptProcessor(4096, 1, 1);
    this.processor.onaudioprocess = (event) => {
      const input = event.inputBuffer.getChannelData(0);
      this.level = rmsOf(input);
      if (!this.onFrame || !this.context) return;
      this.onFrame(frameToBase64(input, this.context.sampleRate));
    };

    this.source.connect(this.processor);
    // Some browsers stop running a script processor that reaches no
    // destination. This does not route the microphone to the speakers.
    this.processor.connect(this.context.destination);
  }

  /** Input loudness right now, 0..1. */
  getLevel(): number {
    return this.level;
  }

  /** The raw stream, for anything wanting its own tap — the level monitor takes
   *  one, measuring the same thing a different way. Two proven techniques; not
   *  worth collapsing into one. */
  getRawStream(): MediaStream | null {
    return this.stream;
  }

  stop(): void {
    this.onFrame = null;
    this.processor?.disconnect();
    this.processor = null;
    this.source?.disconnect();
    this.source = null;
    void this.context?.close().catch(() => {});
    this.context = null;
    this.stream?.getTracks().forEach((track) => track.stop());
    this.stream = null;
    this.level = 0;
  }
}

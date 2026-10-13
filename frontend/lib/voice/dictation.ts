'use client';

import { MicStream } from './mic-stream';
import { describeProgress, RecognitionSocket } from './recognition-socket';

/**
 * The composer's own microphone — speaking instead of typing.
 *
 * Separate from the voice engines in what it DOES: words appear in the box as they are
 * recognised, and they are sent, or edited first, by hand — it never sends a message on its
 * own. It hears the same way they do: one echo-cancelled capture whose frames go to this
 * app's own recognition socket (on this computer, or the cloud service the person chose) —
 * never the browser's built-in recognition.
 *
 * **Only one capture runs at a time**, so whoever owns both this and a voice engine stops one
 * before starting the other.
 *
 * **The speaking guard is a real backstop, not decoration.** The caller is expected to have
 * silenced the assistant first, but a race — started a moment before playback actually stops,
 * or the assistant beginning to speak for an unrelated reason while this is already open —
 * would otherwise transcribe its own voice straight into the box.
 */
export class Dictation {
  private capture: MicStream | null = null;
  private recognizer: RecognitionSocket | null = null;
  private running = false;
  private base = '';
  private settled = '';

  constructor(private readonly hooks: {
    /** The full text as it stands, live. */
    onText?: (text: string) => void;
    onState?: (state: 'idle' | 'listening') => void;
    /** Why it stopped, when it stopped for a reason — never left talking to a box that is
     *  not listening. */
    onError?: (message: string) => void;
    /** A passing note — the first-time download of the on-device model, say. Null clears it. */
    onNote?: (note: string | null) => void;
    isSpeaking?: () => boolean;
  } = {}) {}

  get active(): boolean {
    return this.running;
  }

  get supported(): boolean {
    return typeof window !== 'undefined' && typeof WebSocket !== 'undefined'
      && Boolean(navigator.mediaDevices?.getUserMedia);
  }

  start(baseText = ''): void {
    if (this.running || !this.supported) return;
    if (this.hooks.isSpeaking?.()) return;  // see this class's note
    this.base = baseText ? `${baseText} ` : '';
    this.settled = '';
    this.running = true;
    this.hooks.onState?.('listening');
    void this.listen();
  }

  stop(): void {
    if (!this.running) return;
    this.running = false;
    this.recognizer?.close();
    this.recognizer = null;
    // The capture's tracks end here, so the microphone is really released.
    this.capture?.stop();
    this.capture = null;
    // A result for audio already sent can still arrive after this returns; `running` is what
    // drops it, and clearing here means nothing stale can come back through what survived.
    this.base = '';
    this.settled = '';
    this.hooks.onNote?.(null);
    this.hooks.onState?.('idle');
  }

  /**
   * Re-base onto text that was just edited by hand.
   *
   * Setting a field's value in code never fires a real input event — only genuine typing
   * does — so this is only ever reached for an actual edit. New speech always lands after
   * wherever the edit left off; an edit made in the MIDDLE does not retroactively get
   * inserted there. A disclosed trade-off, not a hidden one.
   */
  rebase(text: string): void {
    if (!this.running) return;
    const trimmed = String(text || '').replace(/\s+$/, '');
    this.base = trimmed ? `${trimmed} ` : '';
    this.settled = '';
  }

  private fail(message: string): void {
    this.stop();
    this.hooks.onError?.(message);
  }

  private async listen(): Promise<void> {
    const capture = new MicStream();
    try {
      await capture.start();
    } catch {
      capture.stop();
      if (this.running) this.fail('The microphone was blocked. Allow it in your browser and try again.');
      return;
    }
    if (!this.running) {           // stopped while the browser was asking for the microphone
      capture.stop();
      return;
    }
    this.capture = capture;

    const recognizer = new RecognitionSocket({
      onTranscript: (text, isFinal) => {
        if (!this.running || this.recognizer !== recognizer) return;
        if (this.hooks.isSpeaking?.()) return;
        if (isFinal) {
          this.settled = `${this.settled}${text} `;
          this.hooks.onText?.(`${this.base}${this.settled}`.replace(/\s+$/, ''));
        } else {
          this.hooks.onText?.(`${this.base}${this.settled}${text}`);
        }
      },
      onProgress: (progress) => this.hooks.onNote?.(describeProgress(progress)),
      onError: (message, fatal) => {
        if (fatal && this.recognizer === recognizer) this.fail(message);
      },
      onLost: () => {
        if (this.recognizer === recognizer) this.fail('Dictation stopped unexpectedly.');
      },
    });
    this.recognizer = recognizer;
    capture.onFrame = (frame) => {
      if (this.running && !this.hooks.isSpeaking?.()) recognizer.send(frame);
    };
    const ok = await recognizer.connect();
    if (ok && this.recognizer === recognizer) this.hooks.onNote?.(null);
  }
}

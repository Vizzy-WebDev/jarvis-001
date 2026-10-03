'use client';

import { takeSpeakable } from './chunker';
import { settleOnce } from './browser-speaker';
import { buildEnvelope, sampleEnvelope, type Envelope } from './voice-envelope';

/**
 * Plays a configured provider's speech, sentence by sentence as it becomes
 * available, gaplessly, stopping instantly for a barge-in.
 *
 * Each sentence is fetched as soon as its text is ready — often while the
 * previous one is still playing — so playback never stalls waiting on
 * synthesis. **Slots are reserved in the queue synchronously**, before the
 * fetch resolves, so a slower earlier sentence can never be overtaken by a
 * faster later one.
 */
const RETRY_DELAY_MS = 400;
const CHARS_PER_SECOND = 12;

interface Ready {
  url: string;
  blob: Blob | null;
  text: string;
  /** True for a static clip, whose URL this class does not own. */
  borrowed?: boolean;
}

/**
 * One sentence's audio, retried once before giving up. Never throws — null on
 * final failure.
 *
 * The retry is not defensive padding: a transient blip or a momentary provider
 * rate limit is exactly what one retry fixes, and before it existed ANY failure
 * silently dropped that sentence. Several in a row near the end of a reply — the
 * likely case, since sentences all start fetching close together — looked
 * exactly like the assistant simply stopping mid-answer with its text complete
 * on screen.
 *
 * `onFinalFailure` fires only after the retry, never on the first attempt.
 */
async function fetchSpeech(
  text: string,
  options: { voice?: string; provider?: string },
  onFinalFailure: () => void,
  attempt = 0,
): Promise<Ready | null> {
  try {
    const response = await fetch('/api/tts', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text, ...options }),
    });
    if (!response.ok) throw new Error(`speech request failed (${response.status})`);
    const blob = await response.blob();
    return { url: URL.createObjectURL(blob), blob, text };
  } catch (err) {
    if (attempt === 0) {
      await new Promise((resolve) => setTimeout(resolve, RETRY_DELAY_MS));
      return fetchSpeech(text, options, onFinalFailure, attempt + 1);
    }
    console.warn('[voice] speech failed twice, skipping this sentence:', err);
    try {
      onFinalFailure();
    } catch (callbackError) {
      console.error('[voice] failure handler threw:', callbackError);
    }
    return null;
  }
}

export class AudioPlayer {
  private buffer = '';
  private queue: Promise<Ready | null>[] = [];
  private current: HTMLAudioElement | null = null;
  private envelope: Envelope | null = null;
  private playing = false;
  private pending = 0;
  private streamEnded = false;
  private stopped = false;
  private flushed = false;
  /** At most once per REPLY: a broken provider fails every sentence, and one
   *  notification per sentence would be a wall of identical noise. */
  private failureNotified = false;
  /** Every sentence that has actually STARTED playing, in order. Read on a
   *  barge-in: what was HEARD, which is not what was fetched — several
   *  sentences can be in flight ahead of the audible one. */
  private spoken = '';

  constructor(
    private readonly hooks: {
      onStart?: () => void;
      onIdle?: () => void;
      onFailure?: (detail: { provider?: string }) => void;
      voice?: string;
      provider?: string;
    } = {},
  ) {}

  /**
   * What has actually been heard so far — reported on a barge-in so the stored
   * reply reflects what the person heard rather than what the model finished
   * generating. Text, not a character offset: an offset would have to be sliced
   * back out of a string whose whitespace need not match, since chunk
   * boundaries trim differently than the model's own spacing.
   */
  getSpokenText(): string {
    return this.spoken;
  }

  /** For the orb: sampled from the offline envelope of whatever is playing. */
  getOutputLevel(): number {
    if (!this.current || !this.envelope) return 0;
    return sampleEnvelope(this.envelope, this.current.currentTime);
  }

  pushText(chunk: string): void {
    const { pieces, rest, flushed } = takeSpeakable(this.buffer + chunk, this.flushed);
    this.buffer = rest;
    this.flushed = flushed;
    for (const piece of pieces) this.enqueueText(piece);
  }

  /**
   * A real sound at its place in the surrounding speech — the same ordered
   * queue, not a second scheduler running beside it.
   */
  enqueueClip(url: string): void {
    if (this.stopped || !url) return;
    this.pending += 1;
    this.queue.push(
      // A separate copy of the same bytes, purely for the orb. If it fails the
      // clip still plays; the orb just shows nothing for it.
      fetch(url)
        .then((response) => response.blob())
        .then((blob): Ready => ({ url, blob, text: '', borrowed: true }))
        .catch((): Ready => ({ url, blob: null, text: '', borrowed: true })),
    );
    if (!this.playing) void this.advance();
  }

  enqueueText(text: string, options: { voice?: string; provider?: string } = {}): void {
    if (this.stopped || !text) return;
    const provider = options.provider ?? this.hooks.provider;
    this.pending += 1;
    this.queue.push(fetchSpeech(text, { voice: options.voice ?? this.hooks.voice, provider }, () => {
      if (this.failureNotified) return;
      this.failureNotified = true;
      this.hooks.onFailure?.({ provider });
    }));
    if (!this.playing) void this.advance();
  }

  end(): void {
    const rest = this.buffer.trim();
    this.buffer = '';
    if (rest) this.enqueueText(rest);
    this.streamEnded = true;
    this.maybeIdle();
  }

  /** Instantly stops and drops everything queued — a barge-in. */
  stop(): void {
    this.stopped = true;
    this.current?.pause();
    this.current = null;
    this.envelope = null;
    this.queue = [];
    this.playing = false;
    this.pending = 0;
    this.buffer = '';
  }

  /**
   * Ready for a fresh reply after a model switch. It reuses `stop()`'s halt —
   * an earlier version reset only flags, leaving the FAILED model's audio and
   * queue intact so it kept playing underneath the replacement — then undoes
   * the one flag stop sets that must not survive.
   */
  reset(): void {
    this.stop();
    this.stopped = false;
    this.streamEnded = false;
    this.buffer = '';
    this.flushed = false;
    this.failureNotified = false;
    this.spoken = '';  // per reply, and this is a fresh one
  }

  private async advance(): Promise<void> {
    if (this.stopped) return;
    const next = this.queue.shift();
    if (!next) {
      this.playing = false;
      this.maybeIdle();
      return;
    }

    this.playing = true;
    const ready = await next;
    if (this.stopped) return;  // stopped while waiting on this fetch
    this.pending -= 1;
    if (!ready) {
      void this.advance();
      return;
    }

    this.hooks.onStart?.();
    // Appended the moment this sentence starts, never when it was fetched. A
    // borrowed URL is a sound effect, not spoken words, and this text is
    // exactly what a barge-in truncates the stored reply to.
    if (!ready.borrowed && ready.text) {
      this.spoken = this.spoken ? `${this.spoken} ${ready.text}` : ready.text;
    }
    const audio = new Audio(ready.url);
    this.current = audio;
    this.envelope = null;

    // The same watchdog as the browser voice, for the same class of bug on a
    // different API: an element that never reports ended or errored leaves this
    // player "playing" forever, so the state never exits. Sized per SENTENCE,
    // not per reply — a new sentence starting is itself proof of progress.
    const once = settleOnce(() => {
      if (!ready.borrowed) URL.revokeObjectURL(ready.url);
      void this.advance();
    });
    const estimated = (ready.text.length / CHARS_PER_SECOND) * 1000;
    const watchdog = setTimeout(() => {
      console.warn('[voice] a sentence never confirmed it finished playing — carrying on.');
      once.fire();
    }, Math.min(30_000, Math.max(6000, estimated * 2.5)));
    once.onSettle = () => clearTimeout(watchdog);

    audio.onended = once.fire;
    audio.onerror = once.fire;
    void audio.play().catch(once.fire);

    // Decoded from the separate copy. It never gates or touches the playback
    // above, including on failure.
    if (ready.blob) {
      void buildEnvelope(ready.blob).then((envelope) => {
        if (this.current === audio) this.envelope = envelope;
      });
    }
  }

  private maybeIdle(): void {
    if (this.streamEnded && this.pending <= 0 && !this.playing) this.hooks.onIdle?.();
  }
}

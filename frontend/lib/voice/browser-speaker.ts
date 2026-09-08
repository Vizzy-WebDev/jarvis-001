'use client';

import { takeSpeakable } from './chunker';

/**
 * The free, offline, no-setup voice: the browser's own speech synthesis.
 *
 * Same interface as the server-voice player, so an engine can hold either one
 * without knowing which.
 *
 * **This class owns an explicit sequential queue — utterances AND clips — and
 * that is load-bearing, not tidiness.** The browser's own synthesis queue holds
 * only utterances, so there is no way to interleave a raw sound clip into it.
 * Before this, each sentence was fired at the browser as soon as it was found,
 * relying entirely on it to serialise playback. Audibly identical for plain
 * speech either way; this is what makes putting a clip at exactly the right
 * position possible at all.
 */
type Item = { type: 'utterance'; text: string } | { type: 'clip'; url: string };

/** ~12 characters a second, a conservative speaking rate. */
const CHARS_PER_SECOND = 12;
const CLIP_WATCHDOG_MS = 6000;
/** How long a word boundary keeps the orb lit. */
const BOUNDARY_DECAY_MS = 220;

function pickVoice(): SpeechSynthesisVoice | null {
  const voices = window.speechSynthesis.getVoices();
  for (const name of ['Aria', 'Ava', 'Guy', 'Natural']) {
    const match = voices.find((v) => v.name.includes(name) && v.lang.startsWith('en'));
    if (match) return match;
  }
  return voices.find((v) => v.lang === 'en-US')
    ?? voices.find((v) => v.lang.startsWith('en'))
    ?? voices[0]
    ?? null;
}

export class BrowserSpeaker {
  private buffer = '';
  private queue: Item[] = [];
  private processing = false;
  /** Queued but not yet settled. */
  private pending = 0;
  private streamEnded = false;
  private stopped = false;
  private flushed = false;
  private lastBoundaryAt = 0;
  private currentClip: HTMLAudioElement | null = null;
  private spoken = '';

  constructor(private readonly hooks: { onStart?: () => void; onIdle?: () => void } = {}) {}

  /**
   * For the orb: this voice's own energy, 0..1.
   *
   * Timing-derived rather than amplitude-derived, because this path exposes no
   * analysable audio at all — each word boundary starts a short decaying pulse,
   * which gives a real per-word rhythm instead of flat procedural motion. A
   * clip has no boundaries, so it shows nothing: a known, accepted gap.
   */
  /**
   * What has actually been heard so far, for a barge-in report.
   *
   * **A gap the original had and this closes.** There, only the provider player
   * tracked this, so interrupting the free browser voice reported nothing and
   * the stored reply kept claiming the whole thing was said. The explicit queue
   * this class now owns makes the cursor available: an utterance is appended
   * when the browser actually STARTS it, not when it was queued.
   */
  getSpokenText(): string {
    return this.spoken;
  }

  getOutputLevel(): number {
    if (!this.lastBoundaryAt) return 0;
    const elapsed = performance.now() - this.lastBoundaryAt;
    return elapsed >= BOUNDARY_DECAY_MS ? 0 : 1 - elapsed / BOUNDARY_DECAY_MS;
  }

  pushText(chunk: string): void {
    if (this.stopped) return;
    const { pieces, rest, flushed } = takeSpeakable(this.buffer + chunk, this.flushed);
    this.buffer = rest;
    this.flushed = flushed;
    for (const piece of pieces) this.enqueue({ type: 'utterance', text: piece });
  }

  /** A real sound, at exactly its place in the speech around it. */
  enqueueClip(url: string): void {
    if (this.stopped || !url) return;
    this.enqueue({ type: 'clip', url });
  }

  end(): void {
    const rest = this.buffer.trim();
    this.buffer = '';
    if (!this.stopped && rest) this.enqueue({ type: 'utterance', text: rest });
    this.streamEnded = true;
    this.maybeIdle();
  }

  stop(): void {
    this.stopped = true;
    window.speechSynthesis.cancel();
    this.currentClip?.pause();
    this.currentClip = null;
    this.queue = [];
    this.processing = false;
    this.pending = 0;
    this.buffer = '';
    this.lastBoundaryAt = 0;
  }

  /**
   * Ready for a fresh reply after a model switch.
   *
   * The cancel matters: the browser keeps its own utterance queue, independent
   * of anything tracked here, so without it a failed model's already-queued
   * sentences keep speaking underneath the replacement's.
   */
  reset(): void {
    window.speechSynthesis.cancel();
    this.currentClip?.pause();
    this.currentClip = null;
    this.queue = [];
    this.processing = false;
    this.pending = 0;
    this.stopped = false;
    this.streamEnded = false;
    this.buffer = '';
    this.flushed = false;
    this.spoken = '';  // per reply, and this is a fresh one
  }

  private enqueue(item: Item): void {
    this.pending += 1;
    this.queue.push(item);
    this.advance();
  }

  private advance(): void {
    if (this.stopped || this.processing) return;
    const item = this.queue.shift();
    if (!item) {
      this.maybeIdle();
      return;
    }
    this.processing = true;
    if (item.type === 'clip') this.playClip(item.url);
    else this.speak(item.text);
  }

  private settle = (): void => {
    this.processing = false;
    this.pending -= 1;
    this.maybeIdle();
    this.advance();
  };

  private speak(sentence: string): void {
    const utterance = new SpeechSynthesisUtterance(sentence);
    const voice = pickVoice();
    if (voice) utterance.voice = voice;
    utterance.rate = 1.02;

    // Browsers have a real bug class where an utterance is dropped and NEITHER
    // end nor error ever fires. Without a watchdog, playback stalls on that one
    // item forever — stuck speaking, mic never resumes, nothing after it starts.
    // Sized to a generous multiple of the estimated speaking time, so it only
    // ever fires on the already-broken path.
    const once = settleOnce(this.settle);
    const estimated = (sentence.length / CHARS_PER_SECOND) * 1000;
    const watchdog = setTimeout(() => {
      console.warn('[voice] the browser never confirmed an utterance finished — carrying on.');
      once.fire();
    }, Math.min(30_000, Math.max(6000, estimated * 2.5)));
    once.onSettle = () => clearTimeout(watchdog);

    utterance.onstart = () => {
      this.spoken = this.spoken ? `${this.spoken} ${sentence}` : sentence;
      this.hooks.onStart?.();
    };
    utterance.onboundary = () => {
      this.lastBoundaryAt = performance.now();
    };
    utterance.onend = once.fire;
    utterance.onerror = once.fire;
    window.speechSynthesis.speak(utterance);
  }

  /** The same watchdog shape: a stalled audio element that never reports back
   *  is the same class of bug on a different API. */
  private playClip(url: string): void {
    const once = settleOnce(() => {
      this.currentClip = null;
      this.settle();
    });
    const watchdog = setTimeout(() => {
      console.warn('[voice] a clip never confirmed it finished — carrying on.');
      once.fire();
    }, CLIP_WATCHDOG_MS);
    once.onSettle = () => clearTimeout(watchdog);

    const audio = new Audio(url);
    this.currentClip = audio;
    this.hooks.onStart?.();
    audio.onended = once.fire;
    audio.onerror = once.fire;
    void audio.play().catch(once.fire);
  }

  private maybeIdle(): void {
    if (this.streamEnded && this.pending <= 0) this.hooks.onIdle?.();
  }
}

/** Settles once, however many of the racing callbacks arrive. */
export function settleOnce(done: () => void) {
  let settled = false;
  const box = {
    onSettle: undefined as (() => void) | undefined,
    fire: () => {
      if (settled) return;
      settled = true;
      box.onSettle?.();
      done();
    },
  };
  return box;
}

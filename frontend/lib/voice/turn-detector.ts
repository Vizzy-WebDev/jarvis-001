/**
 * Deciding when someone has actually finished talking, rather than paused to
 * think. Three layers, cheapest first:
 *
 * 1. **Mic energy**, so "silent" can be told apart from "still speaking, no
 *    transcript chunk yet" — a transcript alone cannot tell you that.
 * 2. **An adaptive wait**, from how the sentence trails off. Ends on "um" or
 *    "and" → wait longer. Ends on a complete question → answer almost at once.
 *    Free, instant, and it catches most real cases.
 * 3. **An optional server round trip** for the genuinely ambiguous middle band.
 *    Off by default: it adds 200–400ms, which is a real cost on every turn.
 */

const FILLER_WORDS = new Set(['um', 'uh', 'er', 'hmm', 'like', 'well', 'so']);
const TRAILING_WORDS = new Set([
  'and', 'but', 'so', 'because', 'or', 'the', 'a', 'an', 'to', 'that',
  'if', 'when', 'while', 'of', 'in', 'on', 'for', 'with', 'my', 'your',
]);

export const WAIT_MS = {
  // filler and trailing stay long on purpose: they exist to avoid cutting off a
  // thought that is clearly still going. Shortening them trades real
  // interruptions for latency, which is the wrong trade.
  filler: 2500,
  trailing: 2000,
  // These two fire on input that already reads as finished, where the wait is
  // dead air rather than a guard — every voice turn pays one of them before the
  // request is even sent.
  default: 900,
  complete: 400,
} as const;

function lastWord(text: string): string {
  const words = text.trim().toLowerCase().replace(/[.,!?;:]+$/g, '').split(/\s+/);
  return words[words.length - 1] || '';
}

const QUESTION_STARTS =
  /^(what|who|where|when|why|how|is|are|do|does|did|can|could|would|will|should)\b/i;

/**
 * A command is the single most common thing said to an assistant — "open
 * YouTube", "search for X". Without recognising these as complete on their own,
 * every clean command fell through to the slow generic wait, missing the most
 * common case entirely.
 */
const IMPERATIVE_STARTS =
  /^(open|search|close|turn|set|play|stop|pause|resume|tell|remind|check|launch|start|show|find|look up|call|text|send|read|cancel|mute|unmute)\b/i;

export function looksComplete(text: string): boolean {
  const trimmed = text.trim();
  if (!trimmed) return false;
  if (/[.!?]\s*$/.test(trimmed)) return true;
  const last = lastWord(trimmed);
  if (FILLER_WORDS.has(last) || TRAILING_WORDS.has(last)) return false;
  // A single bare word is never a complete thought: "Open" could as easily be
  // the start of "Open YouTube".
  if (trimmed.split(/\s+/).length < 2) return false;
  return QUESTION_STARTS.test(trimmed) || IMPERATIVE_STARTS.test(trimmed);
}

/** How long to wait, once speech goes quiet, before treating it as finished. */
export function computeWaitMs(text: string | null | undefined): number {
  const trimmed = (text || '').trim();
  if (!trimmed) return WAIT_MS.default;
  const last = lastWord(trimmed);
  if (FILLER_WORDS.has(last)) return WAIT_MS.filler;
  if (TRAILING_WORDS.has(last)) return WAIT_MS.trailing;
  if (looksComplete(trimmed)) return WAIT_MS.complete;
  return WAIT_MS.default;
}

/**
 * The optional third layer. **Fails open** — an error is treated as "yes,
 * complete" — so a network hiccup can never leave the assistant waiting
 * forever for an answer that is not coming.
 */
export async function isCompleteThought(text: string): Promise<boolean> {
  try {
    const response = await fetch('/api/turn-check', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text }),
    });
    if (!response.ok) return true;
    return Boolean(((await response.json()) as { complete?: boolean }).complete);
  } catch {
    return true;
  }
}

/** What the two classes below need from a microphone — an interface rather than
 *  a concrete monitor, so the silence logic can be tested without a browser. */
export interface LevelSource {
  getLevel(): number;
  isSpeaking(threshold?: number): boolean;
}

/**
 * Raw microphone energy, so the engine can tell silence apart from "speaking,
 * but no transcript yet" — and, during barge-in, require sustained speech
 * rather than a cough.
 */
export class MicLevelMonitor implements LevelSource {
  private readonly context: AudioContext;
  private readonly analyser: AnalyserNode;
  private readonly data: Uint8Array<ArrayBuffer>;
  private readonly source: MediaStreamAudioSourceNode;
  private speakingSince: number | null = null;

  constructor(stream: MediaStream) {
    this.context = new AudioContext();
    this.analyser = this.context.createAnalyser();
    this.analyser.fftSize = 512;
    this.data = new Uint8Array(new ArrayBuffer(this.analyser.frequencyBinCount));
    this.source = this.context.createMediaStreamSource(stream);
    this.source.connect(this.analyser);
  }

  /** Roughly 0 (silence) to 1 (loud), as RMS of the time-domain signal. */
  getLevel(): number {
    this.analyser.getByteTimeDomainData(this.data);
    let squares = 0;
    for (const sample of this.data) {
      const normalised = (sample - 128) / 128;
      squares += normalised * normalised;
    }
    return Math.sqrt(squares / this.data.length);
  }

  isSpeaking(threshold = 0.02): boolean {
    return this.getLevel() > threshold;
  }

  /**
   * True once the mic has been continuously above threshold for `ms` — the
   * barge-in gate. It only means what it says when sampled on a FIXED clock:
   * called sporadically, two loud instants seconds apart can satisfy
   * "sustained" by accident. The threshold is deliberately higher than
   * `isSpeaking`'s, because this one has to clear ordinary speaker echo.
   */
  hasSustainedSpeech(ms = 250, threshold = 0.06, now = performance.now()): boolean {
    if (this.isSpeaking(threshold)) {
      if (this.speakingSince === null) this.speakingSince = now;
      return now - this.speakingSince >= ms;
    }
    this.speakingSince = null;
    return false;
  }

  /**
   * Clear the "how long has this been loud" mark before arming a fresh barge-in
   * window. Without it a stale mark from a PREVIOUS window — this monitor lives
   * for the whole session, not one reply — makes the very first check pass,
   * because the elapsed time is already far past the threshold. A confirmed
   * bug: one loud sample at the end of a reply interrupted the NEXT one almost
   * the instant it started.
   */
  reset(): void {
    this.speakingSince = null;
  }

  close(): void {
    try {
      this.source.disconnect();
    } catch {
      /* already disconnected */
    }
    void this.context.close();
  }
}

/**
 * "Have they finished" measured against REAL silence, not elapsed time since
 * the last transcript event.
 *
 * The gap this closes: restarting a timeout on every recognition result
 * measures how long since the recogniser last emitted something, which is not
 * the same as how long the room has been quiet. Continued mouth noise, breath,
 * or a phoneme gap the recogniser does not emit for could let that timer elapse
 * while someone was still audibly speaking. Sampling energy on a fixed clock
 * fixes that without changing what `computeWaitMs` decides — the wait it
 * produces is still what gets waited for, just measured against genuine
 * silence.
 *
 * One instance lives for the whole listening session; `arm()` is called
 * wherever the old code restarted its timeout, with the same "call again to
 * restart" contract.
 */
export class SilenceWatcher {
  private timer: ReturnType<typeof setInterval> | null = null;
  private quietSince: number | null = null;
  private waitMs = 0;
  private onFire: (() => void) | null = null;

  constructor(
    private readonly mic: LevelSource | null,
    private readonly options: { sampleMs?: number; threshold?: number } = {},
  ) {}

  /**
   * From this call onward, fire once the mic reads continuously below the
   * threshold for `waitMs`. Safe to call repeatedly — each call restarts the
   * quiet clock, and the clock only advances while the mic is genuinely silent,
   * so continuing to make sound keeps deferring it.
   */
  arm(waitMs: number, onFire: () => void): void {
    this.waitMs = waitMs;
    this.onFire = onFire;
    // Never assume "already quiet" the instant this is armed: the next sample
    // decides, which costs at most one tick.
    this.quietSince = null;
    if (!this.timer) {
      this.timer = setInterval(() => this.tick(), this.options.sampleMs ?? 100);
    }
  }

  /** Exposed for tests, which drive the clock rather than waiting on it. */
  tick(now = performance.now()): void {
    if (!this.mic) return;
    if (this.mic.isSpeaking(this.options.threshold ?? 0.02)) {
      this.quietSince = null;
      return;
    }
    if (this.quietSince === null) this.quietSince = now;
    if (now - this.quietSince >= this.waitMs) {
      const fire = this.onFire;
      this.cancel();
      fire?.();
    }
  }

  /**
   * Stop sampling and disarm — on interrupt, mute or teardown.
   *
   * It drops the callback too, not just the interval. Stopping the clock is
   * enough in production, where nothing else drives a tick; dropping the
   * callback makes "cancelled" mean cancelled no matter who calls in, which is
   * the difference between a class that happens to be safe and one that is.
   */
  cancel(): void {
    if (this.timer) clearInterval(this.timer);
    this.timer = null;
    this.quietSince = null;
    this.onFire = null;
  }
}

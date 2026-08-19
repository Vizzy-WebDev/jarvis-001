// Decides when the user is actually done talking, vs. just pausing to
// think. Three layers, cheapest first:
//
//   1. MicLevelMonitor — raw mic energy, so "silent" can be told apart from
//      "still speaking, just no transcript chunk yet" (Chrome's transcript
//      alone can't tell you that).
//   2. computeWaitMs — an adaptive timer based on how the sentence trails
//      off. Ends on "um"/"and"/"the" → wait longer. Ends on a complete
//      question → reply almost immediately. Free, instant, catches most
//      real cases.
//   3. isCompleteThought — an optional server round-trip for the genuinely
//      ambiguous middle band. Off by default (adds ~200-400ms); a toggle
//      in settings turns it on for anyone who wants the extra accuracy.

const FILLER_WORDS = new Set(['um', 'uh', 'er', 'hmm', 'like', 'well', 'so']);
const TRAILING_WORDS = new Set([
  'and', 'but', 'so', 'because', 'or', 'the', 'a', 'an', 'to', 'that',
  'if', 'when', 'while', 'of', 'in', 'on', 'for', 'with', 'my', 'your',
]);

export const WAIT_MS = {
  filler: 2500,
  trailing: 2000,
  default: 1300,
  complete: 700,
};

function lastWord(text) {
  const cleaned = text.trim().toLowerCase().replace(/[.,!?;:]+$/g, '');
  const words = cleaned.split(/\s+/);
  return words[words.length - 1] || '';
}

const QUESTION_STARTS = /^(what|who|where|when|why|how|is|are|do|does|did|can|could|would|will|should)\b/i;

// Commands are Jarvis's single most common input shape ("open YouTube",
// "search for X", "what time is it" phrased as a command rather than a
// question) — without recognizing these as complete on their own, every
// clean command fell through to the generic 1.3s default instead of the
// fast 0.7s reply, missing the most common case entirely.
const IMPERATIVE_STARTS =
  /^(open|search|close|turn|set|play|stop|pause|resume|tell|remind|check|launch|start|show|find|look up|call|text|send|read|cancel|mute|unmute)\b/i;

function looksComplete(text) {
  const trimmed = text.trim();
  if (!trimmed) return false;
  if (/[.!?]\s*$/.test(trimmed)) return true;
  const last = lastWord(trimmed);
  if (FILLER_WORDS.has(last) || TRAILING_WORDS.has(last)) return false;
  // A bare single word ("Open", "What") is never a complete thought on its
  // own — could easily be the start of "Open YouTube" or "What time...".
  if (trimmed.split(/\s+/).length < 2) return false;
  // A question or a command that ends cleanly (not trailing off mid-thought).
  return QUESTION_STARTS.test(trimmed) || IMPERATIVE_STARTS.test(trimmed);
}

/** How long to wait, after speech goes quiet, before treating it as finished. */
export function computeWaitMs(text) {
  const trimmed = (text || '').trim();
  if (!trimmed) return WAIT_MS.default;

  const last = lastWord(trimmed);
  if (FILLER_WORDS.has(last)) return WAIT_MS.filler;
  if (TRAILING_WORDS.has(last)) return WAIT_MS.trailing;
  if (looksComplete(trimmed)) return WAIT_MS.complete;
  return WAIT_MS.default;
}

/**
 * Layer 3 (optional, off by default): asks the server whether a borderline
 * transcript reads as a complete thought. Fails open (treats errors as
 * "yes, complete") so a network hiccup can't make Jarvis hang forever
 * waiting for a reply that will never come.
 */
export async function isCompleteThought(text) {
  try {
    const res = await fetch('/api/turn-check', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text }),
    });
    if (!res.ok) return true;
    const data = await res.json();
    return Boolean(data.complete);
  } catch {
    return true;
  }
}

/**
 * Watches raw microphone energy so the engine can tell "silence" apart from
 * "speaking, but no transcript yet" — and, during barge-in, require
 * sustained speech (not a cough or a "mm-hm") before treating it as a real
 * interrupt.
 */
export class MicLevelMonitor {
  constructor(stream) {
    const AudioContextClass = window.AudioContext || window.webkitAudioContext;
    this.audioContext = new AudioContextClass();
    this.analyser = this.audioContext.createAnalyser();
    this.analyser.fftSize = 512;
    this.dataArray = new Uint8Array(this.analyser.frequencyBinCount);
    this.source = this.audioContext.createMediaStreamSource(stream);
    this.source.connect(this.analyser);
    this._speakingSince = null;
  }

  /** Roughly 0 (silence) to 1 (loud), via RMS of the time-domain signal. */
  getLevel() {
    this.analyser.getByteTimeDomainData(this.dataArray);
    let sumSquares = 0;
    for (const sample of this.dataArray) {
      const normalized = (sample - 128) / 128;
      sumSquares += normalized * normalized;
    }
    return Math.sqrt(sumSquares / this.dataArray.length);
  }

  isSpeaking(threshold = 0.02) {
    return this.getLevel() > threshold;
  }

  /**
   * True once the mic has been continuously above threshold for `ms` — the
   * barge-in gate. Must be sampled on a fixed clock (pipeline-engine.js's
   * barge-in sampler) to mean what it says — called sporadically (e.g. once
   * per speech-recognition result, as an earlier version of the caller did),
   * two loud instants seconds apart can satisfy "sustained" by accident. The
   * default threshold is deliberately higher than isSpeaking()'s — this is
   * the barge-in-specific gate, and 0.02 (~-34dBFS) is low enough that
   * ordinary residual speaker echo clears it.
   */
  hasSustainedSpeech(ms = 250, threshold = 0.06) {
    const now = performance.now();
    if (this.isSpeaking(threshold)) {
      if (this._speakingSince === null) this._speakingSince = now;
      return now - this._speakingSince >= ms;
    }
    this._speakingSince = null;
    return false;
  }

  /**
   * Clears the "how long has this been continuously loud" timestamp.
   * Call this before arming a fresh barge-in window (e.g. at the start of
   * each reply) — without it, a stale timestamp from a PREVIOUS window
   * (this monitor lives for the whole session, not per-reply) can make
   * hasSustainedSpeech() pass on its very first check, since `now -
   * _speakingSince` is already far past `ms`. This was a real, confirmed
   * bug: a single loud sample right at the end of one reply's barge-in
   * window could cause the very next reply to be interrupted almost
   * immediately after it started.
   */
  reset() {
    this._speakingSince = null;
  }

  close() {
    try {
      this.source.disconnect();
    } catch {
      // already disconnected
    }
    this.audioContext.close();
  }
}

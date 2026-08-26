// Local energy-based voice-activity detection for the duplex engine's
// barge-in — reuses turn-detector.js's MicLevelMonitor (same RMS maths
// that's already proven correct in pipeline-engine.js) rather than
// duplicating it, so there is exactly one implementation of "how loud is
// the mic right now" in the codebase.
//
// Why local energy is still needed even with Deepgram doing real
// transcription: it's the FAST signal. Local RMS is available every
// animation frame with zero network round-trip; Deepgram's own transcript
// (the ACCURATE signal) takes a beat to arrive. The duplex engine uses both
// — energy fires first for responsiveness, a transcript arriving moments
// later confirms it was real speech and not a cough or a door — see
// duplex-engine.js's barge-in handling.

import { MicLevelMonitor } from '../turn-detector.js';

export { MicLevelMonitor };

// Originally copied unchanged from pipeline-engine.js's proven barge-in
// constants. That engine fully suspends recognition while speaking, so a
// false trigger there just causes an early, silent cutoff with nothing
// following — invisible enough to never get reported as a problem. This
// engine's barge-in visibly kills Jarvis's live reply on a false positive,
// so it's exercised in a materially more exposed way than these numbers
// were ever validated for. Raised after real, repeated user reports of
// incidental noise (a bumped laptop, shifting around, a hand near the mic)
// falsely triggering an interrupt — sustain duration is the primary lever
// (incidental bumps/taps are typically short transients; intentional speech
// naturally sustains far longer), floor a smaller secondary adjustment.
// Honest caveat: these are reasoned starting values, not empirically
// re-tuned against real hardware (not possible in this environment) — may
// need one more real-world adjustment pass.
export const BARGE_SAMPLE_MS = 100;
export const BARGE_SUSTAIN_MS = 450;
export const BARGE_FLOOR = 0.08;

/**
 * Samples mic energy on a fixed clock and fires `onBargeIn` once it's been
 * continuously loud for BARGE_SUSTAIN_MS — same fixed-rate-sampling
 * reasoning as pipeline-engine.js's _startBargeInSampler() (sporadic
 * sampling, e.g. once per speech-recognition result, let two unrelated loud
 * instants look like one continuous utterance; a fixed clock doesn't).
 *
 * Unlike pipeline-engine.js, this can run continuously while Jarvis is
 * speaking without needing to suspend anything else — the duplex engine
 * never stops the mic at all (see mic-stream.js's header comment on why:
 * one real, AEC-enabled stream, not two separate captures).
 */
export class BargeInDetector {
  constructor(micMonitor) {
    this.monitor = micMonitor;
    this._timer = null;
  }

  /** Starts sampling; calls onBargeIn() once and stops itself (call start() again for the next turn). */
  start(onBargeIn) {
    this.stop();
    // Clear any "how long has this been loud" timestamp left over from a
    // previous reply — same real, confirmed bug pipeline-engine.js's
    // identical reset() call documents: a stale timestamp makes the very
    // next sustain check pass on its first sample.
    this.monitor.reset();
    this._timer = setInterval(() => {
      if (this.monitor.hasSustainedSpeech(BARGE_SUSTAIN_MS, BARGE_FLOOR)) {
        this.stop();
        onBargeIn();
      }
    }, BARGE_SAMPLE_MS);
  }

  stop() {
    clearInterval(this._timer);
    this._timer = null;
  }
}

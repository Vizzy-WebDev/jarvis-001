'use client';

/**
 * An amplitude envelope of the assistant's own speech, for the orb.
 *
 * Decoded OFFLINE from a separate copy of the same bytes — it never touches the
 * element that actually plays them. That separation is the whole point: routing
 * a playing element through an audio graph means it plays only via that graph,
 * and if the shared context happened to be suspended when a reply started, the
 * first moment of speech could come out clipped or silent. Too much risk to the
 * thing this app is for, for a cosmetic effect.
 */
const BUCKETS_PER_SECOND = 60;

export interface Envelope {
  /** 0..1 per bucket. */
  levels: Float32Array;
  seconds: number;
}

export async function buildEnvelope(blob: Blob): Promise<Envelope | null> {
  try {
    const context = new OfflineAudioContext(1, 1, 44_100);
    const decoded = await context.decodeAudioData(await blob.arrayBuffer());
    const samples = decoded.getChannelData(0);
    const buckets = Math.max(1, Math.ceil(decoded.duration * BUCKETS_PER_SECOND));
    const perBucket = Math.max(1, Math.floor(samples.length / buckets));
    const levels = new Float32Array(buckets);

    let peak = 0;
    for (let bucket = 0; bucket < buckets; bucket += 1) {
      let squares = 0;
      const start = bucket * perBucket;
      const end = Math.min(samples.length, start + perBucket);
      for (let i = start; i < end; i += 1) squares += samples[i]! * samples[i]!;
      const rms = Math.sqrt(squares / Math.max(1, end - start));
      levels[bucket] = rms;
      if (rms > peak) peak = rms;
    }
    // Normalised, so a quietly-mastered voice moves the orb as much as a loud
    // one — this drives a visual, not a meter.
    if (peak > 0) for (let i = 0; i < levels.length; i += 1) levels[i]! /= peak;
    return { levels, seconds: decoded.duration };
  } catch {
    // No envelope is a fine outcome: the orb falls back to its own motion and
    // playback is completely unaffected.
    return null;
  }
}

export function sampleEnvelope(envelope: Envelope | null, atSeconds: number): number {
  if (!envelope || !envelope.levels.length) return 0;
  const index = Math.floor(atSeconds * BUCKETS_PER_SECOND);
  return envelope.levels[Math.min(envelope.levels.length - 1, Math.max(0, index))] ?? 0;
}

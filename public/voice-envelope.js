// Builds a silent, offline amplitude envelope for a TTS audio clip so the
// orb can react to Jarvis's own (Gemini) voice — without touching the real
// playback path at all. audio-player.js's header comment explains why that
// matters: an earlier version tapped a Web Audio AnalyserNode directly onto
// every TTS <audio> element for this same purpose, and it was reverted
// because routing playback through that graph risked a suspended/async
// AudioContext clipping or silencing the first words of a reply. This file
// never plays anything and is never connected to speaker output — it just
// decodes a SEPARATE copy of the same bytes through an OfflineAudioContext
// (built specifically for offline, non-realtime rendering/analysis) into a
// plain array of RMS values, sampled by wall-clock time against whatever
// <audio> element is actually playing.

const STEP_MS = 20; // ~50 samples/sec — plenty smooth for the orb's own frame-rate smoothing, cheap to compute

/**
 * Decodes `blob` (the same bytes audio-player.js already fetched for real
 * playback) into `{stepMs, samples: Float32Array}` — one RMS value per
 * STEP_MS of audio. Never throws: resolves null on any failure, so a caller
 * can fall back to procedural motion exactly as it already did before this
 * existed.
 */
export async function buildEnvelope(blob) {
  try {
    const arrayBuffer = await blob.arrayBuffer();
    const OfflineCtx = window.OfflineAudioContext || window.webkitOfflineAudioContext;
    if (!OfflineCtx) return null;
    // The (1, 1, 44100) here is a throwaway render target — decodeAudioData()
    // only uses this context to decode, never to render or play anything
    // through it, so its own channel/length/sampleRate are irrelevant.
    const decodeCtx = new OfflineCtx(1, 1, 44100);
    const audioBuffer = await decodeCtx.decodeAudioData(arrayBuffer);
    const channelData = audioBuffer.getChannelData(0); // TTS output is mono
    const sampleRate = audioBuffer.sampleRate;
    const stepSamples = Math.max(1, Math.round((STEP_MS / 1000) * sampleRate));
    const stepCount = Math.max(1, Math.ceil(channelData.length / stepSamples));
    const samples = new Float32Array(stepCount);
    for (let i = 0; i < stepCount; i++) {
      const start = i * stepSamples;
      const end = Math.min(channelData.length, start + stepSamples);
      let sumSquares = 0;
      for (let j = start; j < end; j++) sumSquares += channelData[j] * channelData[j];
      samples[i] = Math.sqrt(sumSquares / Math.max(1, end - start));
    }
    return { stepMs: STEP_MS, samples };
  } catch (err) {
    console.warn('[voice-envelope] could not build an envelope for this clip:', err);
    return null;
  }
}

/** Reads `envelope` at `currentTimeSec` (an <audio> element's own .currentTime) — 0 if out of range or envelope is null. */
export function sampleEnvelope(envelope, currentTimeSec) {
  if (!envelope) return 0;
  const idx = Math.floor((currentTimeSec * 1000) / envelope.stepMs);
  if (idx < 0 || idx >= envelope.samples.length) return 0;
  return envelope.samples[idx];
}

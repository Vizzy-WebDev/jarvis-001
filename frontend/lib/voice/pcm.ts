/**
 * Turning a browser's microphone samples into what a recognition or realtime
 * service actually wants: 16 kHz, single channel, signed 16-bit, base64.
 *
 * Pure on purpose. Both sockets need exactly this, the original carried two
 * copies of it (one in the realtime engine, one in the microphone module, each
 * with a comment pointing at the other), and it is the only part of either
 * engine that can be checked without a real microphone — so it is the part that
 * gets real tests.
 */

/** Both the recognition provider and every realtime session ask for this. */
export const TARGET_SAMPLE_RATE = 16_000;

/**
 * Nearest-sample decimation, deliberately.
 *
 * It is not a good resampler — dropping samples aliases, and a proper one would
 * filter first. It is the technique the original used and the one every browser
 * example uses, and speech recognition is robust to it. Replacing it is an
 * audio-quality change that would need real speech through a real service to
 * judge, which is not something this port can honestly claim to have done.
 */
export function resampleTo16k(input: Float32Array, inputRate: number): Float32Array {
  if (inputRate === TARGET_SAMPLE_RATE) return input;
  const ratio = inputRate / TARGET_SAMPLE_RATE;
  const length = Math.round(input.length / ratio);
  const output = new Float32Array(length);
  for (let i = 0; i < length; i += 1) output[i] = input[Math.floor(i * ratio)] ?? 0;
  return output;
}

/** Clamped, because a sample slightly outside [-1, 1] wraps to the opposite
 *  extreme once truncated to 16 bits — a click, not a clip. */
export function floatTo16BitPCM(input: Float32Array): Int16Array {
  const output = new Int16Array(input.length);
  for (let i = 0; i < input.length; i += 1) {
    const sample = Math.max(-1, Math.min(1, input[i] ?? 0));
    output[i] = sample < 0 ? sample * 0x8000 : sample * 0x7fff;
  }
  return output;
}

// The buffer type is spelled out: without it the declared `Float32Array` widens
// to one that could be backed by shared memory, which `copyToChannel` refuses.
export function pcm16ToFloat(input: Int16Array): Float32Array<ArrayBuffer> {
  const output = new Float32Array(new ArrayBuffer(input.length * 4));
  for (let i = 0; i < input.length; i += 1) output[i] = (input[i] ?? 0) / 32768;
  return output;
}

/**
 * Chunked rather than one `String.fromCharCode(...bytes)`: spreading a whole
 * buffer into arguments overflows the call stack somewhere in the tens of
 * thousands of bytes, which a short test frame never reaches and a long one
 * does. The original built the string one character at a time, which is correct
 * but quadratic; this is neither.
 */
export function bytesToBase64(bytes: Uint8Array): string {
  let binary = '';
  const step = 0x8000;
  for (let i = 0; i < bytes.length; i += step) {
    binary += String.fromCharCode(...bytes.subarray(i, i + step));
  }
  return btoa(binary);
}

export function base64ToBytes(encoded: string): Uint8Array {
  const binary = atob(encoded);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
  return bytes;
}

/** Loudness of a frame, 0..1 — the same root-mean-square the level monitor
 *  computes from an analyser node, for a raw sample source instead. */
export function rmsOf(samples: Float32Array): number {
  if (!samples.length) return 0;
  let total = 0;
  for (const sample of samples) total += sample * sample;
  return Math.sqrt(total / samples.length);
}

/** One microphone frame, ready to send. */
export function frameToBase64(input: Float32Array, inputRate: number): string {
  const pcm = floatTo16BitPCM(resampleTo16k(input, inputRate));
  return bytesToBase64(new Uint8Array(pcm.buffer, pcm.byteOffset, pcm.byteLength));
}

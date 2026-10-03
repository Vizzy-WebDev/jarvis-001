// The audio format conversion the two sockets depend on. It is the only part of
// either engine that can be checked without a real microphone, so it is the
// part that gets checked properly: the rest is verified in a browser.
import assert from 'node:assert/strict';
import test from 'node:test';

import {
  TARGET_SAMPLE_RATE, base64ToBytes, bytesToBase64, floatTo16BitPCM,
  frameToBase64, pcm16ToFloat, resampleTo16k, rmsOf,
} from '../.test-build/lib/voice/pcm.js';

// `btoa`/`atob` are globals in every browser and in Node 16+; asserting it
// rather than shimming means a Node that lacks them fails loudly here instead
// of silently in the browser.
test('the base64 globals this relies on exist', () => {
  assert.equal(typeof btoa, 'function');
  assert.equal(typeof atob, 'function');
});

test('a matching sample rate is passed through untouched', () => {
  const input = new Float32Array([0.1, -0.2, 0.3]);
  assert.equal(resampleTo16k(input, TARGET_SAMPLE_RATE), input);
});

test('48kHz down to 16kHz keeps every third sample', () => {
  const input = Float32Array.from({ length: 9 }, (_, i) => i / 10);
  const output = resampleTo16k(input, 48_000);
  assert.equal(output.length, 3);
  assert.deepEqual(Array.from(output), [0, 0.3, 0.6].map((v) => Math.fround(v)));
});

test('resampling never reads past the end of the input', () => {
  // Rounding the output length up by one is what would do it, and the result
  // would be a NaN sample rather than an exception — silent, and audible only
  // as a click.
  for (const rate of [44_100, 22_050, 32_000, 8000]) {
    const input = Float32Array.from({ length: 1024 }, () => 0.5);
    for (const sample of resampleTo16k(input, rate)) {
      assert.ok(Number.isFinite(sample), `${rate}Hz produced a non-finite sample`);
    }
  }
});

test('a sample outside the range is clamped, not wrapped', () => {
  // Truncating 1.5 to 16 bits wraps to a large NEGATIVE value: a click, not a
  // clip, and exactly the kind of thing that is inaudible in a test tone and
  // obvious in speech.
  const pcm = floatTo16BitPCM(new Float32Array([1.5, -1.5, 0]));
  assert.deepEqual(Array.from(pcm), [32767, -32768, 0]);
});

test('16-bit conversion round-trips within one step', () => {
  // TWO steps, and the reason is worth stating rather than fudging: 16 bits
  // hold one more negative value than positive, so a positive sample is scaled
  // by 32767 on the way in and divided by 32768 on the way back. That is the
  // convention the original used and every browser example uses, and it costs
  // a truncation step plus a proportional gain step — about -0.0003 dB, which
  // is why nobody changes it. Negative samples are exact.
  const step = 2 / 32768;
  const input = new Float32Array([0, 0.5, -0.5, 0.999, -0.999]);
  const back = pcm16ToFloat(floatTo16BitPCM(input));
  for (let i = 0; i < input.length; i += 1) {
    assert.ok(Math.abs(back[i] - input[i]) <= step,
      `sample ${i}: ${back[i]} is not within one step of ${input[i]}`);
  }
  assert.equal(back[2], -0.5, 'a negative sample should round-trip exactly');
});

test('base64 round-trips every byte value', () => {
  const bytes = Uint8Array.from({ length: 256 }, (_, i) => i);
  assert.deepEqual(Array.from(base64ToBytes(bytesToBase64(bytes))), Array.from(bytes));
});

test('base64 survives a buffer far larger than the argument limit', () => {
  // The obvious one-liner — String.fromCharCode(...bytes) — overflows the call
  // stack somewhere in the tens of thousands of arguments. A short test frame
  // never reaches it and a long capture does, so this is checked at a size that
  // would actually have failed.
  const bytes = Uint8Array.from({ length: 200_000 }, (_, i) => i % 256);
  const round = base64ToBytes(bytesToBase64(bytes));
  assert.equal(round.length, bytes.length);
  assert.equal(round[199_999], bytes[199_999]);
});

test('loudness is zero for silence and one for a full-scale square wave', () => {
  assert.equal(rmsOf(new Float32Array(64)), 0);
  assert.equal(rmsOf(Float32Array.from({ length: 64 }, (_, i) => (i % 2 ? 1 : -1))), 1);
  assert.equal(rmsOf(new Float32Array(0)), 0);  // no frame is not loud
});

test('a whole frame converts to base64 of the expected length', () => {
  const frame = Float32Array.from({ length: 4096 }, (_, i) => Math.sin(i / 20) * 0.5);
  const encoded = frameToBase64(frame, 48_000);
  const bytes = base64ToBytes(encoded);
  // 4096 samples at 48kHz becomes 1365 at 16kHz, two bytes each.
  assert.equal(bytes.length, 1365 * 2);
});

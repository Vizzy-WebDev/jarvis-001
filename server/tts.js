// Text-to-speech via Gemini's TTS model. This is independent of which
// provider is driving the conversation (brain.js) — the user picked
// "Gemini voices" for output regardless of which model answers them, since
// it uses the same free key they already have.
//
// Verified against the installed @google/genai SDK source directly (its
// speechConfig/responseModalities/inlineData handling), since the public
// docs describe a newer "Interactions API" surface this SDK version doesn't
// actually expose. NOT live-verified against the real API (no quota left
// today) — the request/response shape is confirmed structurally correct,
// but an actual audio round-trip hasn't been observed yet.

import { GoogleGenAI } from '@google/genai';
import { getGeminiKey } from './gemini-key.js';

const TTS_MODEL = 'gemini-2.5-flash-preview-tts';
const DEFAULT_VOICE = 'Kore';

// A representative sample of Gemini's ~30 prebuilt voices, for a settings dropdown.
export const AVAILABLE_VOICES = [
  { name: 'Kore', description: 'Firm' },
  { name: 'Puck', description: 'Upbeat' },
  { name: 'Charon', description: 'Informative' },
  { name: 'Fenrir', description: 'Excitable' },
  { name: 'Aoede', description: 'Breezy' },
  { name: 'Leda', description: 'Youthful' },
  { name: 'Orus', description: 'Firm' },
  { name: 'Zephyr', description: 'Bright' },
];

function parseSampleRate(mimeType) {
  const match = /rate=(\d+)/.exec(mimeType || '');
  return match ? parseInt(match[1], 10) : 24000;
}

// Gemini's audio output is raw headerless PCM. Wrapping it in a standard
// 44-byte WAV header lets the browser play it with a plain <audio> element
// or decodeAudioData — no client-side PCM handling needed.
function pcmToWav(pcmBuffer, sampleRate, channels = 1, bitsPerSample = 16) {
  const byteRate = sampleRate * channels * (bitsPerSample / 8);
  const blockAlign = channels * (bitsPerSample / 8);
  const header = Buffer.alloc(44);
  header.write('RIFF', 0);
  header.writeUInt32LE(36 + pcmBuffer.length, 4);
  header.write('WAVE', 8);
  header.write('fmt ', 12);
  header.writeUInt32LE(16, 16);
  header.writeUInt16LE(1, 20); // PCM
  header.writeUInt16LE(channels, 22);
  header.writeUInt32LE(sampleRate, 24);
  header.writeUInt32LE(byteRate, 28);
  header.writeUInt16LE(blockAlign, 32);
  header.writeUInt16LE(bitsPerSample, 34);
  header.write('data', 36);
  header.writeUInt32LE(pcmBuffer.length, 40);
  return Buffer.concat([header, pcmBuffer]);
}

/**
 * Converts text to spoken audio using Gemini's TTS model.
 * Returns a Buffer containing a playable WAV file.
 */
export async function synthesizeSpeech(text, { voice = DEFAULT_VOICE } = {}) {
  // getGeminiKey (not getProviderKey) so a key added through Model Settings
  // as a connection secret counts too — see gemini-key.js. Voice output was
  // silently dead for anyone who only ever added Gemini that way.
  const apiKey = getGeminiKey();
  if (!apiKey) {
    const err = new Error('No Gemini API key configured.');
    err.code = 'NO_API_KEY';
    throw err;
  }

  const ai = new GoogleGenAI({ apiKey });
  const response = await ai.models.generateContent({
    model: TTS_MODEL,
    contents: [{ role: 'user', parts: [{ text }] }],
    config: {
      responseModalities: ['AUDIO'],
      speechConfig: voice,
    },
  });

  const part = response.candidates?.[0]?.content?.parts?.[0];
  const inlineData = part?.inlineData;
  if (!inlineData?.data) {
    throw new Error('Gemini TTS returned no audio data.');
  }

  const pcmBuffer = Buffer.from(inlineData.data, 'base64');
  const sampleRate = parseSampleRate(inlineData.mimeType);
  return pcmToWav(pcmBuffer, sampleRate);
}

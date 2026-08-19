// Optional layer-3 turn detection (see public/turn-detector.js): a quick,
// single classification call asking whether a transcript reads as a
// finished thought. Deliberately independent of the model registry / active
// model — like text-to-speech (server/tts.js), it always uses Gemini
// directly since it's a cheap one-off classifier, not a real conversation
// turn. Moved out of the old providers/gemini.js when the model system
// became pluggable.

import { GoogleGenAI } from '@google/genai';
import { getGeminiKey } from './gemini-key.js';

// Verified directly against the live API (2026-07-30): 'gemini-3.6-flash'
// free tier is capped at just 20 requests/day (too restrictive for real
// use), and 'gemini-2.5-flash' has been retired for new API keys. 3.5 Flash
// is the current stable choice with a workable free-tier quota.
const MODEL = 'gemini-3.5-flash';

export async function classifyTurnComplete(text) {
  // getGeminiKey (not getProviderKey) so a key added through Model Settings
  // as a connection secret counts too — see gemini-key.js.
  const apiKey = getGeminiKey();
  if (!apiKey) return true; // fail open — feature is optional anyway

  const ai = new GoogleGenAI({ apiKey });
  const response = await ai.models.generateContent({
    model: MODEL,
    contents: [
      {
        role: 'user',
        parts: [
          {
            text:
              'Reply with exactly one word, "yes" or "no": does this look like a complete ' +
              `thought, or does it sound like the speaker was cut off mid-sentence?\n\n"${text}"`,
          },
        ],
      },
    ],
  });
  return /yes/i.test(response.text || 'yes');
}

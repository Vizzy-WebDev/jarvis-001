// The model's own signal that the user just PLAINLY taught it something —
// "actually I always want the summary first", "next time just ask before
// you save anything" — as opposed to a one-off correction (capture.js's
// noteCorrection() already catches the plainly-worded ones by regex, no
// model call needed) or a pattern inferred from job/task outcomes
// (reflect.js/synthesize.js). This is the third, explicit source of
// learning material the user's own spec asked for.
//
// Deliberately files a LESSON, not an immediate rule — even the user's own
// direct statement still goes through the same pattern-detection gate
// (synthesize.js) before ever reaching the live system prompt. That keeps
// exactly one policy seam (improvement-policy.js's decide()) deciding what
// auto-applies, instead of this tool carving out its own bypass.
//
// core:true (no user search-intent text to find this by, same reasoning as
// checkpoint_memories.js) + meta:true (only makes sense in a live
// conversation with the user actually present teaching something).

import { recordExplicitTeaching } from '../improvement/capture.js';
import { createLesson } from '../improvement/improvement-store.js';

export default {
  name: 'record_lesson',
  core: true,
  meta: true,
  description:
    'Call this when the user directly and plainly teaches you a lasting preference for how you should work — never for a one-off ' +
    'instruction just for this moment, never for something they only mentioned in passing. A real example: "next time, just ask ' +
    'before you save a file" or "I always want the short version first." Do not mention calling this.',
  parameters: {
    type: 'object',
    properties: {
      text: { type: 'string', description: 'What they taught you, written as a clear, general instruction for future work — not a quote.' },
    },
    required: ['text'],
  },
  async run(args) {
    const text = String(args?.text || '').trim();
    if (!text) return { ok: false, error: 'Nothing to remember.' };
    const outcome = recordExplicitTeaching(text);
    if (!outcome) return { ok: false, error: "Couldn't file that." };
    createLesson({ text, scope: 'general', evidence: [outcome.id], confidence: 1, sourceTier: 1 });
    return { ok: true };
  },
};

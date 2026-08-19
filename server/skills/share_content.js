// Skill: register a piece of content the user is pointing at — a link, a
// file on their PC, or text they paste or dictate.
//
// This does exactly one thing: work out what the thing IS (a title, a rough
// kind, roughly how long/big it is) and store it. It makes NO model call and
// does NOT read, watch, or listen to anything — that only happens once the
// user says what they want (examine_content). Content on its own must never
// trigger an analysis; asking what's wanted is the correct behaviour here,
// not a fallback for when Jarvis doesn't understand.
//
// The one exception: if the user said what they want in the same breath
// they shared it ("fact-check the bit about margins in this video"), pass
// that as `instruction` and it's looked into right away — there is nothing
// to ask, they already said.

import { share } from '../content/investigator.js';
import { classifySource } from '../content/intake.js';

export default {
  name: 'share_content',
  meta: true,
  description:
    'Register a piece of content the user shares — a YouTube link, a web article, a file on their PC, or text ' +
    'they paste or dictate. Use this whenever they give you something to look at, whether or not they said what ' +
    'they want done with it. This only works out what the thing IS; it does not read or watch it. If they said ' +
    'what they want (fact-check it, is this realistic, what does it say about X), pass that through as ' +
    'instruction and it is looked into right away. If they did not say, leave instruction out — Jarvis asks.',
  parameters: {
    type: 'object',
    properties: {
      source: {
        type: 'string',
        description:
          'What they gave you: a URL, a Windows file path, or the text itself. Pass it through exactly as ' +
          'they said it — do not tidy up or shorten a link.',
      },
      instruction: {
        type: 'string',
        description:
          'What they want done with it, in their own words, ONLY if they already said so in the same breath. ' +
          'LEAVE THIS OUT otherwise. Do not invent one — Jarvis asking what they want is correct, not a failure.',
      },
    },
    required: ['source'],
  },
  async run({ source, instruction } = {}, ctx = {}) {
    const raw = String(source || '').trim();
    if (!raw) return { ok: false, error: "I didn't catch what you wanted me to look at." };

    const classified = classifySource(raw);

    try {
      const { record, identity } = await share({
        source: classified,
        instruction,
        sessionId: ctx.sessionId || 'main',
      });

      const instructionGiven = Boolean(String(instruction || '').trim());
      return {
        ok: true,
        contentId: record.id,
        what_it_is: `${identity.kind}${identity.detail ? `, ${identity.detail}` : ''}${identity.title ? ` — "${identity.title}"` : ''}`,
        unreachable: identity.unreachable || undefined,
        instruction_given: instructionGiven,
        spoken_hint: instructionGiven
          ? "Say you're looking into it now. Don't answer yet — you haven't looked. The answer arrives shortly."
          : 'Say plainly what it appears to be (kind and, if useful, its length or size) and ask what they\'d ' +
            'like you to do with it. Do NOT summarize, research, fact-check, or analyse it in any way — you ' +
            'have not read it, and choosing something to do with it yourself would be wrong here.',
      };
    } catch (err) {
      return { ok: false, error: err?.message || "I couldn't register that." };
    }
  },
};

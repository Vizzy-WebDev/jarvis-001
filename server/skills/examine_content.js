// Skill: ask something about content Jarvis has already been shown.
//
// The request is passed through in the user's own words on purpose — there
// is no `kind` enum here (the old build's judge/realism/save/answer
// routing is gone). Whatever they ask shapes what gets extracted; nothing
// is read with a fixed template. Checking whether something is TRUE is a
// separate tool (check_claim) which researches independently before it
// answers — use that when the truth of a specific claim is the actual
// question, not this one.
//
// The answer does not come back in this call. Looking into something can
// take a real model call (or, for video/audio, a genuine watch), so this
// starts the work and returns straight away; the answer arrives in the
// conversation shortly after.

import { examine } from '../content/investigator.js';
import { latestInSession, getContent } from '../content/content-store.js';

export default {
  name: 'examine_content',
  meta: true,
  description:
    'Ask something about a piece of content Jarvis already knows about (from share_content) — what it says, ' +
    'what happens in it, what it means, whether it\'s worth the time, how it compares to something, or ' +
    'anything else about it. Use this for any follow-up question about shared content, and whenever Jarvis ' +
    'asked what the user wants done with something and they answer. Do NOT use this to check whether a claim ' +
    'is true — use check_claim for that.',
  parameters: {
    type: 'object',
    properties: {
      request: {
        type: 'string',
        description: 'What they want to know, in their own words — pass it through as they said it.',
      },
      contentId: {
        type: 'string',
        description:
          'Which piece of content this is about. When Jarvis registered something it noted a reference id in ' +
          'the conversation — use that. Leave out only if there is genuinely just one thing in play.',
      },
    },
    required: ['request'],
  },
  async run({ request, contentId } = {}, ctx = {}) {
    const text = String(request || '').trim();
    if (!text) return { ok: false, error: "I didn't catch what you wanted to know." };

    const sessionId = ctx.sessionId || 'main';
    const record = (contentId && getContent(contentId)) || latestInSession(sessionId);
    if (!record) {
      return { ok: false, error: "There's nothing I've been shown recently. Share a link, a file, or some text first." };
    }

    try {
      examine(record.id, text, { sessionId });
      return {
        ok: true,
        contentId: record.id,
        about: record.identity?.title || 'that',
        spoken_hint:
          "Say you're looking into it. The answer, with its source of how it was taken in, appears in the " +
          "conversation when it's ready — read out the gist then, not the whole thing.",
      };
    } catch (err) {
      return { ok: false, error: err?.message || "I couldn't look into that." };
    }
  },
};

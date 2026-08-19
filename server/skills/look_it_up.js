// Skill: look something up and give a plain, sourced answer — no verdict
// attached.
//
// New in this rebuild. Before this, real research was only reachable
// THROUGH a fact-check (check_claim) or the planning research step, which
// meant "how much does this actually cost?" or "how does this tool work?"
// had nowhere honest to go — either it got answered from the model's own
// memory (unsourced, possibly stale) or it got forced through a verdict
// (checks-out/misleading/...) that doesn't fit a question with no claim to
// judge. The refined spec is explicit that investigation covers researching
// a tool or Skill, understanding how something works, and extracting
// information — none of which are fact-checks.
//
// Same underlying engine as check_claim (server/research.js) — free web
// search first, model-native search only if that comes back thin — just
// without the research-then-verdict framing on top.
//
// Runs synchronously and broadcasts its own `content_progress` event for the
// same reason check_claim.js does — see that file's header for why the
// tool-result channel alone can't deliver the write-up to the browser.

import { research } from '../research.js';
import { broadcast } from '../events.js';

function standaloneId() {
  return `lu${Date.now().toString(36)}${Math.random().toString(36).slice(2, 8)}`;
}

export default {
  name: 'look_it_up',
  description:
    'Look something up on the web and give a plain, sourced answer — how something works, what a tool or ' +
    'Skill actually does, typical costs, background on something, current information you would not ' +
    'reliably know from memory. Use this whenever the user wants information found, not a claim judged true ' +
    'or false — for that, use check_claim instead.',
  parameters: {
    type: 'object',
    properties: {
      question: {
        type: 'string',
        description: 'What to look up, as a clear, self-contained question.',
      },
    },
    required: ['question'],
  },
  async run({ question } = {}) {
    const text = String(question || '').trim();
    if (!text) return { ok: false, error: "I didn't catch what you wanted looked up." };

    try {
      const found = await research(text);
      if (!found.ok) return { ok: false, error: found.error };

      broadcast({
        type: 'content_progress',
        id: standaloneId(),
        title: text.length > 60 ? `${text.slice(0, 60)}…` : text,
        status: 'ready',
        document: found.answer,
        sources: found.sources || [],
      });

      return {
        ok: true,
        answer: found.answer,
        sources: (found.sources || []).map((s) => ({ title: s.title, url: s.url })),
        via: found.via,
        spoken_hint:
          'Give the gist in your own words, a sentence or two. The full answer with its sources is on screen; ' +
          'do not read it all out.',
      };
    } catch (err) {
      return { ok: false, error: err?.message || "I couldn't look that up." };
    }
  },
};

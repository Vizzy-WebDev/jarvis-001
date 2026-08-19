// Skill: check whether a specific claim actually holds up.
//
// Rebuild of the old fact_check.js — the one part of the old Content
// Analysis build that already matched the refined spec, carried forward to
// behave exactly the same way. What changed is its place in the system: it
// used to be the centre of gravity (most of what the old analyzer could do
// was some flavour of this); now it's one tool among several, reached for
// only when the truth of a specific claim is genuinely the question.
//
// The part that must never be lost: `judgeClaim()` calls research() before
// it judges, unconditionally, with no path around it. A model cannot decide
// it already knows and skip the lookup, because the lookup isn't its
// decision to make.
//
// Deliberately NOT a meta skill: checking a claim is just as valid inside a
// scheduled task or a morning briefing as it is in live conversation.
//
// This skill runs synchronously (a claim check is quick enough not to need
// backgrounding), but the tool-result channel the model gets back only ever
// carries a few whitelisted fields to the browser (see models/runner.js) —
// never a skill's full result. Without broadcasting the write-up itself, a
// document card the spoken_hint below promises simply wouldn't exist. So
// this broadcasts its own `content_progress` event the moment a verdict is
// ready, the same event shape examine_content's background job uses, just
// fired synchronously with a standalone id rather than a stored record's.

import { judgeClaim } from '../content/investigator.js';
import { broadcast } from '../events.js';

function standaloneId() {
  return `cc${Date.now().toString(36)}${Math.random().toString(36).slice(2, 8)}`;
}

export default {
  name: 'check_claim',
  description:
    'Check whether a specific claim is actually true, or whether something described is realistic rather than ' +
    'exaggerated. Looks the claim up independently before judging it, and answers with one of: checks out, ' +
    'partly true, misleading, false, or "can\'t tell". Use it for a claim from something the user shared, ' +
    'something they heard elsewhere, or anything where being wrong would matter. Prefer this over answering ' +
    'from your own knowledge whenever the truth of a specific claim is the actual question.',
  parameters: {
    type: 'object',
    properties: {
      claim: {
        type: 'string',
        description:
          'The single claim to check, stated plainly and on its own — for example "you can make $12,000 in ' +
          'your first month dropshipping", not "is that video right". Quote figures and timescales exactly ' +
          'as they were given; they are what gets looked up.',
      },
      realism: {
        type: 'boolean',
        description:
          'True when the question is "is this realistic / typical / just hype" rather than "is this ' +
          'factually true". Changes what gets researched: what actually happens for most people who try it, ' +
          'rather than whether the statement is literally accurate.',
      },
      context: {
        type: 'string',
        description:
          'Optional. Where the claim came from and anything that frames it — the video it was in, what the ' +
          'user is trying to decide. Helps the verdict speak to what they actually care about.',
      },
    },
    required: ['claim'],
  },
  async run({ claim, realism, context } = {}) {
    const text = String(claim || '').trim();
    if (!text) return { ok: false, error: "I didn't catch which claim you wanted checked." };

    try {
      const judged = await judgeClaim({ claim: text, context, realism: Boolean(realism) });
      if (!judged.ok) return { ok: false, error: judged.error };

      broadcast({
        type: 'content_progress',
        id: standaloneId(),
        title: `Checked: "${text.length > 60 ? `${text.slice(0, 60)}…` : text}"`,
        status: 'ready',
        document: judged.result,
        sources: judged.sources || [],
        verdict: judged.verdict,
      });

      return {
        ok: true,
        verdict: judged.verdict,
        // The full write-up is a document — it renders in the conversation.
        // What comes back here is what's worth saying out loud.
        detail: judged.result,
        sources: (judged.sources || []).map((s) => ({ title: s.title, url: s.url })),
        researched: judged.researched,
        spoken_hint:
          `Give the verdict — "${judged.verdict}" — and one sentence on why, in your own words. The full ` +
          'write-up with sources is on screen; do not read it out. ' +
          (judged.researched
            ? ''
            : "Say plainly that you couldn't look this up independently, so the verdict is weaker than usual."),
      };
    } catch (err) {
      return { ok: false, error: err?.message || "I couldn't check that." };
    }
  },
};

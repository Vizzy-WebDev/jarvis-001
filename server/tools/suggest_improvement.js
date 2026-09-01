// The model's own noticing, articulated once, mid-conversation — "I could
// do X better" or "a Skill for Y would help." Distinct from the lesson/
// pattern pipeline (reflect.js/synthesize.js): this has no evidence trail
// of its own (evidence: []), so improvement-policy.js's decide() always
// requires approval for it regardless of trust level — no special-casing
// needed here, the existing floor already covers it.
//
// Non-core: the model reaches this via find_capability when a real moment
// calls for it, not on every turn's declaration list — proposing something
// is rare enough that it doesn't need the same always-visible treatment as
// record_lesson.js's "the user just taught me something" signal.

import { createProposal } from '../improvement/improvement-store.js';

const KINDS = ['rule', 'setting', 'skill', 'code', 'idea'];

export default {
  name: 'suggest_improvement',
  description:
    "Propose a genuine improvement to how you work — a behaviour rule, a Skill idea, or a code change — when you've noticed a real, " +
    "specific opportunity, not a vague feeling. This never applies itself; it always waits for the user's own review on the " +
    'Self-Improvement screen. Use rarely, only for something concrete worth their attention.',
  parameters: {
    type: 'object',
    properties: {
      kind: { type: 'string', enum: KINDS, description: '"rule" for a behaviour rule you could follow yourself; "setting" for a tunable dial; "skill" or "code" for something that needs building; "idea" for anything looser.' },
      title: { type: 'string', description: 'A short, plain-language title.' },
      rationale: { type: 'string', description: 'Why, in a sentence or two — what you actually noticed.' },
      helps_jarvis: { type: 'string', description: 'How this would help Jarvis specifically.' },
      helps_user: { type: 'string', description: 'How this would help the user specifically.' },
      rule_text: { type: 'string', description: 'Only for kind "rule" — the exact instruction, phrased directly.' },
      setting_key: { type: 'string', description: 'Only for kind "setting" — which dial.' },
      setting_value: { type: 'string', description: 'Only for kind "setting" — the new value.' },
    },
    required: ['kind', 'title', 'rationale'],
  },
  summarize(args) {
    return `Suggest an improvement: "${args?.title || 'untitled'}".`;
  },
  async run(args) {
    const kind = KINDS.includes(args?.kind) ? args.kind : 'idea';
    let payload = null;
    if (kind === 'rule') {
      const text = String(args?.rule_text || '').trim();
      if (!text) return { ok: false, error: 'A "rule" suggestion needs rule_text.' };
      payload = { text, scope: 'general' };
    } else if (kind === 'setting') {
      if (!args?.setting_key) return { ok: false, error: 'A "setting" suggestion needs setting_key.' };
      payload = { key: args.setting_key, value: args?.setting_value };
    }

    const proposal = createProposal({
      kind,
      title: String(args?.title || 'Untitled suggestion').slice(0, 120),
      rationale: args?.rationale || null,
      helpsJarvis: args?.helps_jarvis || null,
      helpsUser: args?.helps_user || null,
      payload,
      evidence: [], // this is the model's own in-the-moment noticing, not evidence from tracked outcomes — decide() always asks for approval on an empty evidence array
      sourceTier: 1,
    });
    return { ok: true, proposal_id: proposal.id };
  },
};

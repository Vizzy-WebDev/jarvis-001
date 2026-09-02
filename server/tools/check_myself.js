// The pull path into the Self-Model (server/self/self-model.js) — real,
// grounded self-knowledge, never vague self-conception. Same spirit as
// review_improvements.js/prompt.js's connectorsSection(): a real question
// ("can you actually do this reliably?", "why are you doing that?", "how do
// you know that?") gets a real, evidence-backed answer instead of the model
// guessing at its own nature. See root CLAUDE.md's "Self-Model" section.
//
// core:true (no reliable search-intent text — "what can you actually do"
// doesn't map to a capability search the way "what time is it" maps to
// "time") + meta:true (a live-conversation-only check; a scheduled task or
// a Job worker's own turn has no audience to explain itself to).

import { buildSelfModel, DIMENSION_KEYS, extractCitableFields } from '../self/self-model.js';
import { saveSelfModelSnapshot, recordSelfModelCitation } from '../self/self-store.js';

export default {
  name: 'check_myself',
  core: true,
  meta: true,
  description:
    'Check your own real, evidence-backed state before making a claim about yourself — your actual track record on something ' +
    '(never estimate this from general impression), what you are currently doing and why, how you actually know something you\'re ' +
    'about to say, what is genuinely your call to make versus what needs the user, a known way you tend to fail at something, how ' +
    'you specifically work with this user, or the goal you recorded for this conversation. Never answer a question about your own ' +
    'reliability, authority, or current state from impression alone — check here first.',
  parameters: {
    type: 'object',
    properties: {
      about: {
        type: 'array',
        items: { type: 'string', enum: DIMENSION_KEYS },
        description:
          'Which aspect(s) to check: what_it_is, can_do, behavior, doing_now, how_it_knows, authority, failure_modes, works_with_you, goal.',
      },
      tools: { type: 'array', items: { type: 'string' }, description: 'Tool name(s) to check real reliability for — only used with can_do.' },
      jobKinds: { type: 'array', items: { type: 'string' }, description: 'Job kind(s) to check real reliability for — only used with can_do.' },
      taskTypes: { type: 'array', items: { type: 'string' }, description: 'Scheduled task action type(s) to check real reliability for — only used with can_do.' },
      memoryQuery: { type: 'string', description: 'A few words of the specific claim you\'re unsure how you know — only used with how_it_knows.' },
      scopes: {
        type: 'array',
        items: { type: 'string' },
        description: "Scope string(s) relevant to what you're about to do, e.g. 'tool:run_code' — only used with failure_modes.",
      },
    },
    required: ['about'],
  },
  async run(args, ctx = {}) {
    const only = Array.isArray(args?.about) ? args.about.filter((k) => DIMENSION_KEYS.includes(k)) : [];
    if (!only.length) return { ok: false, error: 'Name at least one real aspect to check — see the tool\'s own list.' };
    const capabilities = typeof ctx.listCapabilities === 'function' ? ctx.listCapabilities({ includeMeta: false }) : undefined;
    const self = buildSelfModel({
      only,
      capabilities,
      sessionId: ctx.sessionId,
      style: ctx.style,
      about: { tools: args?.tools, jobKinds: args?.jobKinds, taskTypes: args?.taskTypes },
      memoryQuery: args?.memoryQuery,
      scopesInPlay: args?.scopes,
    });

    // Utterance provenance (root CLAUDE.md's Self-Model section,
    // server/self/self-verify.js) — every real call is persisted, and every
    // NUMERIC, checkable fact in it is logged as a citation candidate right
    // now, before anyone knows whether the reply that follows will actually
    // use it. Never blocks or fails the tool call itself if it throws — a
    // provenance-logging failure must never break the underlying self-check
    // the model actually asked for.
    try {
      const snapshotId = saveSelfModelSnapshot({ conversationId: ctx.sessionId, turnId: ctx.turnId, toolCallId: ctx.toolCallId, snapshot: self });
      for (const { fieldName, value } of extractCitableFields(self)) {
        recordSelfModelCitation({ snapshotId, toolCallId: ctx.toolCallId, fieldName, fieldValue: value });
      }
    } catch (err) {
      console.error('[check_myself] provenance logging failed:', err);
    }

    return { ok: true, self };
  },
};

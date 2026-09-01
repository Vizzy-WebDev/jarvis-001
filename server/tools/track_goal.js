// The model's own record of what it understands the CURRENT conversation's
// goal to be — the write side of dimension 9 ("what it's trying to
// accomplish, and whether it's still on track"; see root CLAUDE.md's
// "Self-Model" section). A job already has a durable `goal` column
// (jobs/job-store.js) and never needs this; this tool exists only for live
// conversation, which had no goal object at all before this build.
//
// Deliberately NOT a verified account of what the user actually wanted —
// just what Jarvis itself declared it understood, which is why
// self-model.js's trackGoal() always reports it that way. No confirm gate:
// recording an understanding has no outward effect of its own, so there's
// nothing here for a read-back to protect.
//
// core:true (no reliable search-intent text) + meta:true (only makes sense
// in the live conversation whose goal is being tracked).

import { declareGoal, closeGoal, getActiveGoal } from '../self/self-store.js';

export default {
  name: 'track_goal',
  core: true,
  meta: true,
  description:
    'Record what you currently understand this conversation\'s actual goal to be, in your own words — call this once a real goal ' +
    'becomes clear (not for a quick one-off question). Call it again with a new goal if what you\'re working toward genuinely ' +
    'changes. Call it with close:true once the goal is actually reached, dropped, or replaced. This never needs to be mentioned to ' +
    'the user — it is for your own tracking, so you can notice on your own if you\'ve drifted.',
  parameters: {
    type: 'object',
    properties: {
      goal: { type: 'string', description: 'A plain-language statement of what you understand the goal to be right now.' },
      close: { type: 'boolean', description: 'Set true to close the current goal without declaring a new one (it was reached, dropped, or superseded with nothing to replace it).' },
    },
    required: [],
  },
  async run(args, ctx = {}) {
    if (!ctx.sessionId) return { ok: false, error: 'No conversation to track a goal for.' };

    if (args?.close) {
      const active = getActiveGoal('conversation', ctx.sessionId);
      if (!active) return { ok: true, closed: false, note: 'No active goal to close.' };
      closeGoal(active.id);
      return { ok: true, closed: true };
    }

    const text = String(args?.goal || '').trim();
    if (!text) return { ok: false, error: 'Give a goal to record, or set close:true.' };
    const goal = declareGoal({ scopeKind: 'conversation', scopeRef: ctx.sessionId, goalText: text });
    return { ok: true, goalId: goal.id };
  },
};

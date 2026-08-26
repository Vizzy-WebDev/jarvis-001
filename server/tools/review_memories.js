// Skill: "what do you have pending?" / "review my memories" — the explicit
// checkpoint from the spec ("whenever I explicitly ask to review pending
// memories"). No confirmation needed (nothing changes just by looking).
// Hands back a `ui_action` so the same in-chat review card the proactive
// checkpoints show (see server/memory/memory-review.js's broadcast) opens
// here too, on demand — one card component, two ways to trigger it.

import { listPendingCandidates } from '../memory/memory-store.js';

export default {
  name: 'review_memories',
  meta: true,
  description:
    'Show the user any memory suggestions Jarvis has been quietly collecting that are still waiting for approval. ' +
    'Use this when the user asks to review, see, or check pending memories.',
  parameters: { type: 'object', properties: {}, required: [] },
  async run() {
    const pending = listPendingCandidates();
    if (!pending.length) {
      return { ok: true, count: 0, spoken_hint: 'Tell the user there is nothing pending right now.' };
    }
    return {
      ok: true,
      count: pending.length,
      ui_action: { type: 'memory_review' },
      spoken_hint: `There ${pending.length === 1 ? 'is one memory' : `are ${pending.length} memories`} pending — say so briefly; the card itself shows the details, so don't list them out loud.`,
    };
  },
};

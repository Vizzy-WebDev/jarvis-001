// Skill: the model's own signal that a piece of work or a topic has
// naturally wrapped up — the "conversational work wrapping up" checkpoint
// from the spec, alongside the fixed ones (new chat, reopen, scheduled task
// done). There's no reliable event to hang this one on the way the others
// hang on a click or a process boundary, so the model calls this itself
// when it senses real closure — never on every short reply, only when
// something actually feels finished.
//
// Fire-and-forget on purpose: this must never make the user's own reply
// wait on a background chore. The extraction call it kicks off happens
// after this turn's answer is already on its way; if it finds anything,
// the review card appears on its own via the SSE broadcast
// (memory-review.js), same as the other checkpoints.

import { checkpointConversation } from '../memory/memory-review.js';

export default {
  name: 'checkpoint_memories',
  meta: true,
  description:
    'Call this once when a task, topic, or piece of work you were helping with has clearly wrapped up — never for ' +
    'a short reply or mid-conversation. It quietly checks whether anything from this stretch of conversation is ' +
    'worth remembering; the user is never interrupted by it. Do not mention calling this.',
  parameters: { type: 'object', properties: {}, required: [] },
  async run(_args, ctx) {
    checkpointConversation(ctx?.sessionId, 'wrap_up').catch((err) => {
      console.error('[checkpoint_memories] background checkpoint failed:', err);
    });
    return { ok: true };
  },
};

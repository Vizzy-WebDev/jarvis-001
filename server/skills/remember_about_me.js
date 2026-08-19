// Skill: saves a short note Jarvis should remember about the user — their
// work, goals, or preferences — so it can use it later (most visibly, to
// personalize the morning briefing and its suggestions). Always confirmed
// first, so a misheard note doesn't quietly become a wrong "fact" Jarvis
// repeats back later.
//
// Writes straight into Memory (server/memory/memory-store.js), bypassing
// the pending-candidate/checkpoint queue entirely — an explicit "remember
// that..." already got the user's real-time yes via the confirm read-back
// below, which IS the approval; queuing it for a later review batch would
// just be asking twice. Filed under a fixed 'About You' category rather
// than asking the model to pick one on every voice utterance — see root
// CLAUDE.md's Memory section for why that's a deliberate simplification.
// The Profile & Goals screen (public/screens/profile.js) reads this same
// category via server/profile.js, so nothing about that screen changed.

import { createMemory } from '../memory/memory-store.js';

const CATEGORY = 'About You';

export default {
  name: 'remember_about_me',
  // Meta skill: see schedule_task.js's comment — left out of the task/
  // briefing skill picker, still usable in conversation.
  meta: true,
  description:
    'Save a short note Jarvis should remember about the user — their work, goals, or preferences — ' +
    'for later use. Use this when the user says "remember that..." or shares something about their ' +
    'goals or work worth keeping.',
  confirm: 'always',
  parameters: {
    type: 'object',
    properties: {
      text: { type: 'string', description: 'The note to remember, as a short clear sentence.' },
    },
    required: ['text'],
  },
  summarize(args) {
    return `Remember this about you: "${args.text}".`;
  },
  async run(args) {
    const memory = createMemory({ category: CATEGORY, text: args.text, sourceKind: 'chat', sourceRef: null });
    return { ok: true, saved: memory.text };
  },
};

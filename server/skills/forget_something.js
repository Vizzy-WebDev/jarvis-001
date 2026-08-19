// Skill: deletes a memory the user asks Jarvis to forget — "forget that I
// mentioned X", "you don't need to remember my old job anymore". Always
// confirmed first, and the confirmation reads back the ACTUAL stored text
// that will be deleted (not just the user's vague description of it), so
// "forget my address" can never silently delete the wrong memory.
//
// A hard delete (server/memory/memory-store.js's deleteMemory), not an
// archive — "forget" is a plain, direct request; if the user meant
// something softer they'd ask to update it instead (see update_memory.js).

import { listMemories, deleteMemory } from '../memory/memory-store.js';

/** Best-effort case-insensitive match against stored text/category — simple substring search, not a model call, since this only needs to be "good enough to confirm," not exhaustive. */
function findMatch(query) {
  const q = String(query || '').trim().toLowerCase();
  if (!q) return null;
  return listMemories({}).find((m) => m.text.toLowerCase().includes(q) || m.category.toLowerCase().includes(q)) || null;
}

export default {
  name: 'forget_something',
  meta: true,
  description:
    'Delete something Jarvis currently remembers about the user. Use this when the user says "forget that...", ' +
    '"you don\'t need to remember...", or similar — never delete a memory without this being asked.',
  confirm: 'always',
  parameters: {
    type: 'object',
    properties: {
      query: { type: 'string', description: 'What to forget, in the user\'s own words — used to find the matching memory.' },
    },
    required: ['query'],
  },
  summarize(args) {
    const match = findMatch(args.query);
    if (!match) return `I couldn't find anything matching "${args.query}" to forget.`;
    return `Forget this: "${match.text}"?`;
  },
  async run(args) {
    const match = findMatch(args.query);
    if (!match) {
      return { ok: false, error: `I couldn't find anything matching "${args.query}" to forget.` };
    }
    deleteMemory(match.id);
    return { ok: true, forgot: match.text };
  },
};

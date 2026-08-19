// Skill: changes what a memory says — "actually I moved to Seattle, not
// Boston", "update my job title to...". Always confirmed first, reading
// back both the old and new text so the user can see exactly what's
// changing before it does. The edit is never a silent overwrite —
// server/memory/memory-store.js's updateMemory() records the prior text as
// a version row first, so "why did this change?" always has a real answer.

import { listMemories, updateMemory } from '../memory/memory-store.js';

function findMatch(query) {
  const q = String(query || '').trim().toLowerCase();
  if (!q) return null;
  return listMemories({}).find((m) => m.text.toLowerCase().includes(q) || m.category.toLowerCase().includes(q)) || null;
}

export default {
  name: 'update_memory',
  meta: true,
  description:
    'Change what Jarvis remembers about something — a fact that\'s changed, a correction, an update. ' +
    'Use this when the user corrects or updates something Jarvis already knows, rather than remember_about_me ' +
    'again (which would add a second, possibly conflicting note instead of fixing the existing one).',
  confirm: 'always',
  parameters: {
    type: 'object',
    properties: {
      query: { type: 'string', description: 'What to find and update, in the user\'s own words.' },
      new_text: { type: 'string', description: 'What it should say now.' },
    },
    required: ['query', 'new_text'],
  },
  summarize(args) {
    const match = findMatch(args.query);
    if (!match) return `I couldn't find anything matching "${args.query}" to update.`;
    return `Change "${match.text}" to "${args.new_text}"?`;
  },
  async run(args) {
    const match = findMatch(args.query);
    if (!match) {
      return { ok: false, error: `I couldn't find anything matching "${args.query}" to update.` };
    }
    const updated = updateMemory(match.id, { text: args.new_text }, 'Updated via conversation.');
    return { ok: true, before: match.text, after: updated.text };
  },
};

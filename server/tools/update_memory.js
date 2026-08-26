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
    'Use this ONLY when the user actually asks for the correction, rather than remember_about_me again ' +
    '(which would add a second, possibly conflicting note instead of fixing the existing one). Never call ' +
    'this just because something they said happens to disagree with an existing note — that kind of ' +
    'contradiction is caught separately in the background and always goes back to the user to decide, ' +
    'not something to interrupt the conversation about.',
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
    // origin: 'explicit' — the user just directly confirmed this exact
    // text via the read-back above, regardless of how the memory was
    // originally saved (approved, auto-saved, or otherwise).
    const updated = updateMemory(match.id, { text: args.new_text }, 'Updated via conversation.', 'explicit');
    return { ok: true, before: match.text, after: updated.text };
  },
};

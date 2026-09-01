// Conversational undo — "undo that change you made" — for a change the
// user just saw via review_improvements.js or the Self-Improvement screen.
// Non-core: reached via find_capability, since undoing something is rare
// enough not to need an always-declared slot. No confirm gate — undo is
// the SAFE direction (returning to how things were), the same reasoning
// forget_something.js/archiveMemory() apply; the real safety net is
// apply.js's own refuse-vs-clobber check below, surfaced honestly rather
// than silently overridden.

import { undoChange } from '../improvement/apply.js';

export default {
  name: 'undo_improvement',
  description:
    'Undo one specific self-improvement change, by its id (from review_improvements or the Self-Improvement screen). If it refuses ' +
    'because the value changed since, explain that plainly and ask whether to undo anyway before calling this again with force:true.',
  parameters: {
    type: 'object',
    properties: {
      change_id: { type: 'string', description: "The change's id." },
      force: { type: 'boolean', description: 'Only after the user confirms they want it undone despite it having changed since.' },
    },
    required: ['change_id'],
  },
  async run(args) {
    try {
      const result = undoChange(String(args.change_id), { force: Boolean(args?.force) });
      if (!result.ok) {
        return { ok: true, undone: false, reason: result.reason, current_value: result.current, value_it_would_restore: result.expected };
      }
      return { ok: true, undone: true, restored_to: result.restoredTo };
    } catch (err) {
      return { ok: false, error: err?.message || "Couldn't undo that." };
    }
  },
};

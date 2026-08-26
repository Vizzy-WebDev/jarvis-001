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

import { createMemory, listMemories } from '../memory/memory-store.js';

const CATEGORY = 'About You';

/**
 * Best-effort "does the user already have a memory saying roughly this?"
 * check — same spirit as forget_something.js's/update_memory.js's own
 * findMatch() (simple, not a model call, "good enough to confirm" rather
 * than exhaustive), duplicated here rather than shared for the same reason
 * those two don't share theirs: a few lines, not worth a new leaf module.
 * Exists because remember_about_me writes straight into Memory with NO
 * conflict check at all otherwise — the "a conflict always requires
 * approval" floor (memory-policy.js's decide()) only ever runs on the
 * quiet checkpoint path, never on this direct one. This doesn't force a
 * merge; it just gives the confirm read-back below a chance to flag it so
 * the user can say "no, update the existing one" instead of ending up with
 * two memories that say almost the same thing.
 */
function findSimilar(text) {
  const normalize = (s) =>
    String(s || '')
      .trim()
      .toLowerCase()
      .replace(/[^\w\s]/g, '')
      .replace(/\s+/g, ' ');
  const target = normalize(text);
  if (!target) return null;
  return (
    listMemories({}).find((m) => {
      const existing = normalize(m.text);
      return existing && (existing.includes(target) || target.includes(existing));
    }) || null
  );
}

export default {
  name: 'remember_about_me',
  // Meta skill: see schedule_task.js's comment — left out of the task/
  // briefing skill picker, still usable in conversation.
  meta: true,
  // Core tool — see get_time.js's comment (and open_section.js's on why
  // `meta` and `core` are orthogonal).
  core: true,
  description:
    'Save a note the user has DIRECTLY ASKED to be remembered about them — their work, goals, or preferences. ' +
    'Only for an explicit request: "remember that…", "note this down", "don\'t forget I…". Never call this ' +
    'because something merely came up in conversation and seemed worth keeping — facts mentioned in passing ' +
    'are captured separately in the background and must not be turned into a confirmation question.',
  confirm: 'always',
  parameters: {
    type: 'object',
    properties: {
      text: { type: 'string', description: 'The note to remember, as a short clear sentence.' },
    },
    required: ['text'],
  },
  summarize(args) {
    const similar = findSimilar(args.text);
    if (similar) {
      return `Remember this about you: "${args.text}". This looks similar to what I already have — "${similar.text}". Save it as a new note anyway?`;
    }
    return `Remember this about you: "${args.text}".`;
  },
  async run(args) {
    // origin: 'explicit' — the user said this directly and just confirmed
    // it via the read-back above; distinct from 'approved' (a candidate the
    // user reviewed from a card) or 'auto' (saved without being asked).
    const memory = createMemory({ category: CATEGORY, text: args.text, sourceKind: 'chat', sourceRef: null, origin: 'explicit' });
    return { ok: true, saved: memory.text };
  },
};

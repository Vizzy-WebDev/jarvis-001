// Skill: saves a new Skill from the model's own understanding of a
// conversation — what "Create with Jarvis" (public/screens/skills.js) leads
// to, and what makes "make me a skill that does X" work from plain
// conversation too, not just from that button. The button is only a
// shortcut: it pre-fills the composer with a starter line and focuses it
// (see app.js's `jarvis:start-skill-chat` handling) — the actual back-and-
// forth and the eventual save both go through this one skill either way,
// mirroring how Claude's own "Create with Claude" is a composer pre-fill
// into ordinary chat, not a separate feature.
//
// Always confirmed first (same reasoning as remember_about_me.js): the
// model should read the drafted name/description/instructions back before
// anything is written to disk, so the user can correct it in conversation
// rather than discover a wrong skill after the fact.

import { createSkill } from './store/skill-files.js';
// NOT `import { reservedSkillNames } from './index.js'` — this file is
// itself dynamically imported BY skills/index.js's loader loop at startup,
// so a top-level import of the loader back would deadlock it (see
// CLAUDE.md's circular-import invariant). runSkill() injects
// `ctx.reservedSkillNames` for exactly this reason — see index.js's
// runSkill() comment.

export default {
  name: 'create_skill',
  // Meta skill: see schedule_task.js's comment — creating a Skill only
  // makes sense in a live conversation with the user, never on an
  // unattended run.
  meta: true,
  description:
    'Create a new Skill for Jarvis from what the user wants it to do, once you understand it well ' +
    'enough to draft it. Use this after talking through what the skill should do — never on the ' +
    'first mention alone. Always read the drafted name, description, and instructions back to the ' +
    'user before this is confirmed.',
  confirm: 'always',
  parameters: {
    type: 'object',
    properties: {
      name: {
        type: 'string',
        description: 'A short slug for the skill, lowercase words separated by hyphens, e.g. "weekly-status-report".',
      },
      description: {
        type: 'string',
        description: 'One or two sentences: what the skill does AND when Jarvis should use it.',
      },
      instructions: {
        type: 'string',
        description: 'Clear, step-by-step instructions Jarvis follows when this skill is used.',
      },
    },
    required: ['name', 'description', 'instructions'],
  },
  summarize(args) {
    return `Create a new skill called "${args.name}": ${args.description}`;
  },
  async run(args, ctx = {}) {
    try {
      const reserved = ctx.reservedSkillNames ? ctx.reservedSkillNames() : new Set();
      const skill = createSkill(
        { name: args.name, description: args.description, instructions: args.instructions },
        reserved
      );
      return { ok: true, name: skill.name };
    } catch (err) {
      return { ok: false, error: err?.message || 'Could not create that skill.' };
    }
  },
};

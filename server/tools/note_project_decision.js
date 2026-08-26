// Skill: record something that's been settled while talking through a
// project.
//
// This is what makes conversation-driven planning actually work. There is
// no question queue any more (see start_project.js) — Jarvis just talks —
// so nothing else captures what was decided along the way unless this does.
// Call it whenever something genuinely gets settled: who it's for, what it
// must do first, whether money changes hands, a preference stated plainly.
// Do NOT call it for a passing remark, a question, or something still being
// weighed — only for an actual decision.
//
// No model call: this just appends to the project's record. Decisions
// persist to disk, so they survive the conversation itself resetting.

import { noteDecision } from '../projects/project-engine.js';
import { latestInSession } from '../projects/project-store.js';

export default {
  name: 'note_project_decision',
  meta: true,
  description:
    'Record something that has just been genuinely settled about a project being planned — who it\'s for, ' +
    'what it must do first, a budget, a preference, anything decided rather than merely discussed. Use this ' +
    'as decisions come up naturally in conversation, not as a batch at the end. Never call it for something ' +
    'still being weighed or a question that hasn\'t been answered yet.',
  parameters: {
    type: 'object',
    properties: {
      decision: {
        type: 'string',
        description: 'The decision, stated plainly and on its own — e.g. "no user accounts in the first version".',
      },
      projectId: {
        type: 'string',
        description: 'Which project this belongs to. Leave out if there is genuinely only one being discussed.',
      },
    },
    required: ['decision'],
  },
  async run({ decision, projectId } = {}, ctx = {}) {
    const text = String(decision || '').trim();
    if (!text) return { ok: false, error: 'Nothing to record.' };

    const sessionId = ctx.sessionId || 'main';
    const target = projectId || latestInSession(sessionId)?.id;
    if (!target) return { ok: false, error: "There's no project open to attach that to yet — start one first." };

    try {
      const project = noteDecision(target, text);
      return {
        ok: true,
        projectId: project.id,
        recorded: text,
        spoken_hint: 'No need to announce this out loud — just carry on the conversation naturally.',
      };
    } catch (err) {
      return { ok: false, error: err?.message || "I couldn't record that." };
    }
  },
};

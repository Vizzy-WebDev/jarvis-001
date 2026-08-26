// Skill: write the MVP plan document for a project.
//
// User-triggered only. The old build wrote the plan the instant its
// question queue emptied — with no go-ahead from the user at all, and in
// the worst case (zero generated questions) went from one sentence to a
// finished plan with no human input whatsoever. This never runs itself:
// call it only when the user clearly asks for the plan, or plainly means
// "write it up now." Runs in the background; the plan arrives as a document
// card in the conversation.

import { writePlan } from '../projects/project-engine.js';
import { latestInSession } from '../projects/project-store.js';

export default {
  name: 'write_project_plan',
  meta: true,
  description:
    'Write the first-version (MVP) plan document for a project being discussed. Use this ONLY when the user ' +
    'clearly asks for the plan to be written, or plainly means "let\'s write this up now" — never automatically ' +
    'just because discussion or research has happened. Research is not required first; write from whatever is ' +
    'known if the user wants to skip it.',
  parameters: {
    type: 'object',
    properties: {
      projectId: {
        type: 'string',
        description: 'Which project to write up. Leave out if there is genuinely only one being discussed.',
      },
    },
    required: [],
  },
  async run({ projectId } = {}, ctx = {}) {
    const sessionId = ctx.sessionId || 'main';
    const target = projectId || latestInSession(sessionId)?.id;
    if (!target) return { ok: false, error: "There's no project open to write up yet — start one first." };

    try {
      const project = writePlan(target);
      return {
        ok: true,
        projectId: project.id,
        spoken_hint:
          "Tell them you're writing it up now. Don't read the plan out when it lands — it's a document, and " +
          "it appears here in the conversation. Just say it's ready and give the one-line gist.",
      };
    } catch (err) {
      return { ok: false, error: err?.message || "I couldn't write that plan." };
    }
  },
};

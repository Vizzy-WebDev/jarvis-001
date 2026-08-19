// Skill: look into what building a project idea actually involves.
//
// User-triggered only — this never runs on its own. The old build researched
// the instant an idea was mentioned, before anyone had said research was
// wanted. Runs in the background (a real web lookup plus a synthesis call
// takes a few seconds); the findings arrive in the conversation shortly
// after, with their sources.

import { researchProject } from '../projects/project-engine.js';
import { latestInSession } from '../projects/project-store.js';

export default {
  name: 'research_project',
  meta: true,
  description:
    'Look into what building a project idea actually involves — practical steps, typical costs, common ' +
    'mistakes. Use this ONLY when the user clearly asks Jarvis to look into or research the project, or ' +
    'plainly means "go find out what this needs" — never automatically just because an idea was mentioned.',
  parameters: {
    type: 'object',
    properties: {
      projectId: {
        type: 'string',
        description: 'Which project to research. Leave out if there is genuinely only one being discussed.',
      },
    },
    required: [],
  },
  async run({ projectId } = {}, ctx = {}) {
    const sessionId = ctx.sessionId || 'main';
    const target = projectId || latestInSession(sessionId)?.id;
    if (!target) return { ok: false, error: "There's no project open to research yet — start one first." };

    try {
      const project = researchProject(target);
      return {
        ok: true,
        projectId: project.id,
        spoken_hint: "Say you're looking into it now. The findings, with sources, appear in the conversation shortly.",
      };
    } catch (err) {
      return { ok: false, error: err?.message || "I couldn't research that." };
    }
  },
};

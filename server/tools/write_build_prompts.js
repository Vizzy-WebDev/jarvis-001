// Skill: write the handoff build prompt(s) for a project.
//
// User-triggered only, and needs a written plan first (write_project_plan).
// Two things the old build got wrong, both fixed here:
//
//   1. Its final prompt was written from the plan document ALONE — anything
//      decided in conversation that hadn't made it into the plan text was
//      gone by this step. This reads the plan, every recorded decision, the
//      research, and the conversation itself.
//   2. It only ever produced a single prompt, regardless of size. This can
//      produce several, in sequence (Prompt 1, Prompt 2, ...), with the
//      recommended order explained, when the project is too large or risky
//      to hand over as one step — the model decides which fits.

import { writePrompts } from '../projects/project-engine.js';
import { listAssistants, getAssistant } from '../projects/assistants.js';
import { latestInSession, getProject } from '../projects/project-store.js';

const ASSISTANT_IDS = listAssistants().map((a) => a.id);

export default {
  name: 'write_build_prompts',
  meta: true,
  description:
    'Write the ready-to-paste build prompt(s) for a project that already has a plan. May produce one prompt ' +
    'or several in sequence, depending on how large the project is. Use this ONLY when the user clearly asks ' +
    'for the prompt, or plainly means "give me something to hand to the AI now" — never automatically just ' +
    'because the plan finished.',
  parameters: {
    type: 'object',
    properties: {
      projectId: {
        type: 'string',
        description: 'Which project. Leave out if there is genuinely only one being discussed.',
      },
      target: {
        type: 'string',
        enum: ASSISTANT_IDS,
        description:
          'Which AI will build this: claude-code (a terminal coding agent), chat (ChatGPT or Claude in a chat ' +
          'window), editor (Cursor, Windsurf, Copilot), builder (Lovable, v0, Bolt, Replit), or custom. If the ' +
          'user hasn\'t said and one wasn\'t set when the project started, ask them before calling this.',
      },
      targetName: {
        type: 'string',
        description: 'If target is "custom", the name of the tool the user actually said.',
      },
    },
    required: [],
  },
  async run({ projectId, target, targetName } = {}, ctx = {}) {
    const sessionId = ctx.sessionId || 'main';
    const projectRecord = (projectId && getProject(projectId)) || latestInSession(sessionId);
    if (!projectRecord) return { ok: false, error: "There's no project open yet — start one first." };

    const chosenTarget = target && getAssistant(target)
      ? { id: target, name: targetName || null }
      : projectRecord.target;

    if (!chosenTarget) {
      return {
        ok: false,
        error: 'no_target',
        spoken_hint: 'Ask which AI or coding assistant will actually receive and build this, then call this again with that answer.',
      };
    }

    try {
      const project = writePrompts(projectRecord.id, chosenTarget);
      return {
        ok: true,
        projectId: project.id,
        spoken_hint:
          "Tell them you're writing it up now. When it lands, say how many prompts there are and — only if " +
          "there's more than one — the recommended order in a sentence. Don't read the prompts out; they're on " +
          'screen with a copy button each.',
      };
    } catch (err) {
      return { ok: false, error: err?.message || "I couldn't write that." };
    }
  },
};

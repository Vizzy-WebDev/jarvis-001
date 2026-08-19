// Skill: open a project notepad for something the user wants to build.
//
// This is deliberately the ONLY thing this skill does — create the record
// and say so. No research, no questions generated, no background pipeline
// of any kind. The old build's plan_project immediately kicked off research
// and a fixed queue of up to six questions the instant this was called;
// that queue is what turned every later reply into "the answer to question
// 3" even when it was a challenge or a change of mind. There is no queue
// now, so there is nothing to fall into.
//
// After this, the model is told to just talk: ask whatever's genuinely
// unclear, in its own words, one thing at a time — and respond in place to
// a challenge or a tangent rather than pushing back onto a track. Settled
// decisions get recorded with note_project_decision as they come up.
// research_project / write_project_plan / write_build_prompts each need an
// explicit go-ahead; none of them follow on from this automatically.

import { startProject } from '../projects/project-engine.js';
import { listAssistants, getAssistant } from '../projects/assistants.js';
import { latestInSession } from '../projects/project-store.js';

const ASSISTANT_IDS = listAssistants().map((a) => a.id);

export default {
  name: 'start_project',
  // Meta skill: only meaningful in a live conversation — a scheduled task
  // opening a project nobody is present to discuss would just sit there.
  meta: true,
  description:
    'Open a project notepad for something the user wants to build — a website, an app, a business, a project ' +
    'of any kind. Use this when they first describe an idea, however rough. This ONLY creates the notepad — ' +
    'it does not research or ask questions on its own. After calling it, just talk with them about the idea ' +
    'normally; use note_project_decision to record things as they get settled.',
  parameters: {
    type: 'object',
    properties: {
      idea: {
        type: 'string',
        description:
          "The user's idea, in their own words, as fully as they described it. Do not summarize it down.",
      },
      target: {
        type: 'string',
        enum: ASSISTANT_IDS,
        description:
          'Which AI will build this, ONLY if the user already said: claude-code (a terminal coding agent), ' +
          'chat (ChatGPT or Claude in a chat window), editor (Cursor, Windsurf, Copilot), builder (Lovable, ' +
          'v0, Bolt, Replit), or custom if they named something else. Leave out if they haven\'t said yet — ' +
          'this can be set later, when the build prompt is actually written.',
      },
      targetName: {
        type: 'string',
        description: 'If target is "custom", the name of the tool the user actually said.',
      },
    },
    required: ['idea'],
  },
  async run({ idea, target, targetName } = {}, ctx = {}) {
    const text = String(idea || '').trim();
    if (!text) return { ok: false, error: 'I need to hear the idea first.' };

    const sessionId = ctx.sessionId || 'main';
    const existing = latestInSession(sessionId);

    const chosenTarget = target && getAssistant(target) ? { id: target, name: targetName || null } : null;
    const project = startProject({ idea: text, sessionId, target: chosenTarget });

    return {
      ok: true,
      projectId: project.id,
      title: project.title,
      spoken_hint:
        "Say you've opened it up and ask whatever's genuinely unclear about the idea — in your own words, one " +
        'thing at a time, like a normal conversation. Do not list a set of questions. If they challenge ' +
        'something, question why a part is needed, or change their mind, just respond to that — never push ' +
        'them back onto a script. Only research, write the plan, or write the build prompt when they clearly ' +
        "ask for that, or when it's unmistakably what they mean.",
      already_in_progress: existing ? { projectId: existing.id, title: existing.title } : undefined,
    };
  },
};

// Skill: lets Jarvis actually operate the user's computer toward a stated
// goal — the look/act/verify loop in server/control/session.js. Always
// confirmed first (confirm: 'always'): the first call generates a short
// plan and reads it back for approval (the user's chosen autonomy level,
// "confirm the plan, then work"); only the second call, carrying the
// confirm_token, actually starts the session.
//
// Imports only control/session.js's public entry points — never
// skills/index.js itself, or the dynamic-import loader that loads this very
// file would deadlock on itself (see CLAUDE.md's circular-import invariant).

import { preparePlan, runControlSession } from '../control/session.js';

// The plan generated in summarize() is cached here, keyed by the same
// argsKey shape skills/index.js's own confirmation layer uses internally
// (JSON.stringify with sorted keys) — so run() reuses the exact plan the
// user actually approved instead of asking the model to plan a second time.
const planCache = new Map(); // key -> { plan, createdAt }
const PLAN_TTL_MS = 5 * 60 * 1000;

function keyFor(args) {
  try {
    return JSON.stringify(args || {}, Object.keys(args || {}).sort());
  } catch {
    return String(args);
  }
}

export default {
  name: 'control_computer',
  description:
    'Take control of the user\'s mouse, keyboard, and screen to actually carry out a task on their computer — ' +
    'opening apps, clicking, typing, and checking the result. Use this only when the user clearly wants Jarvis to ' +
    'DO something on the computer itself (not just answer a question or use a normal built-in ability like opening ' +
    'a website or an app by name — those have their own simpler tools). Always confirms a short plan first.',
  confirm: 'always',
  parameters: {
    type: 'object',
    properties: {
      goal: {
        type: 'string',
        description: 'A clear, specific description of what to accomplish on the computer, e.g. "open Notepad, type a shopping list, and save it to the Desktop as list.txt".',
      },
    },
    required: ['goal'],
  },
  async summarize(args) {
    const plan = await preparePlan(args.goal);
    planCache.set(keyFor(args), { plan, createdAt: Date.now() });
    return `Here's my plan: ${plan}`;
  },
  async run(args) {
    const key = keyFor(args);
    const cached = planCache.get(key);
    planCache.delete(key);
    const plan = cached && Date.now() - cached.createdAt < PLAN_TTL_MS ? cached.plan : null;

    const result = await runControlSession(args.goal, plan);

    if (result.status === 'done') return { ok: true, summary: result.summary };
    if (result.status === 'stopped') return { ok: false, error: `Stopped — ${result.reason}` };
    if (result.status === 'blocked') return { ok: false, error: result.reason };
    if (result.status === 'error') return { ok: false, error: result.reason };
    return { ok: false, error: `I couldn't finish that — ${result.reason}` };
  },
};

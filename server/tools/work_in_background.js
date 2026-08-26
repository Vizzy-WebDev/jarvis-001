// Starts a background Job — the Conversation Manager's own decision to
// background something (on its own judgment, or because the user asked),
// per root CLAUDE.md's Jobs section. Deliberately imports only
// jobs/job-actions.js (a leaf) and ai.js (also leaf-safe — see that file's
// own header comment) — never jobs/orchestrator.js directly, which would
// recreate the circular-import deadlock job-actions.js's split exists to
// avoid. Capacity is checked BEFORE spending the admission model call below,
// so an already-full owner doesn't cost a wasted call for a job that won't
// be created anyway.

import { hasBackgroundCapacityNow, runningSummaries, createJobIfCapacity } from '../jobs/job-actions.js';
import { askModel } from '../ai.js';

const VALID_KINDS = new Set(['research', 'files', 'computer', 'generic']);

const ADMISSION_SYSTEM =
  'You are the planning stage of a background task system. Given a goal, decide which KIND of worker fits ' +
  'best, produce a short concrete title, and a brief plan for how it will be approached. You are NOT doing ' +
  'the work — only planning it. Be concrete and specific to the actual goal, never generic.\n\n' +
  'Kinds:\n' +
  '- "research": web/information lookup, fact-checking, gathering and cross-referencing sources.\n' +
  '- "files": running code, analyzing spreadsheets or documents, local file work.\n' +
  '- "computer": operating apps/windows on the desktop directly (clicking, typing) — ONLY when the goal ' +
  'genuinely requires that, since a computer job always asks the owner to confirm before it actually starts.\n' +
  '- "generic": anything else, or a genuine mix of the above.\n' +
  'Pick exactly one existing kind — never invent a new one.';

function admissionPrompt(goal) {
  return (
    `Goal for a background job: ${goal}\n\n` +
    'Respond with JSON only: {"kind": "research"|"files"|"computer"|"generic", "title": "...", ' +
    '"plan": {"summary": "...", "steps": ["...", "..."]}}. ' +
    'title: max 8 words. plan.summary: one sentence describing the approach. ' +
    'plan.steps: 2-5 short phrases, or a single step if the goal genuinely does not decompose further.'
  );
}

/** Best-effort planning — a hiccup here must never block the owner's ability to background something; the worker gets its own real model calls once it actually runs. */
async function planAdmission(goal) {
  const admission = await askModel({ system: ADMISSION_SYSTEM, prompt: admissionPrompt(goal), json: true, background: true });
  if (admission.ok && admission.data?.title) {
    return {
      kind: VALID_KINDS.has(admission.data.kind) ? admission.data.kind : 'generic',
      title: String(admission.data.title).slice(0, 80),
      plan:
        admission.data.plan && typeof admission.data.plan === 'object'
          ? { summary: String(admission.data.plan.summary || goal), steps: Array.isArray(admission.data.plan.steps) ? admission.data.plan.steps.map(String).slice(0, 5) : [] }
          : { summary: goal, steps: [] },
    };
  }
  return { kind: 'generic', title: goal.slice(0, 80), plan: { summary: goal, steps: [] } };
}

export default {
  name: 'work_in_background',
  core: true,
  meta: true,
  description:
    'Start a long-running task in the BACKGROUND while you keep talking with the user about anything else. ' +
    "Use this when a request will genuinely take a while (real research, building something, processing files, " +
    "an involved multi-step task) and shouldn't make the user wait, OR when they explicitly say to keep working " +
    'on something in the background. After calling this, you MUST tell the user right away, clearly and in your ' +
    "own words, that you're now working on this in the background — never switch to background silently — and " +
    'briefly mention the plan you were given back. If at_capacity comes back true, tell the user what is already ' +
    'running and ask which to prioritize instead of starting this. If status comes back "awaiting_decision" ' +
    '(only happens when the job needs to control the computer directly), it has NOT started yet — tell the user ' +
    "you'd like to do this by taking control of their computer and ask them to confirm before it begins; once " +
    'they agree, use check_on_work with respond:"keep_going" on this job_id to actually start it.',
  parameters: {
    type: 'object',
    properties: {
      goal: {
        type: 'string',
        description: 'A clear, complete, self-contained description of what the background job should accomplish — it will not have the rest of this conversation to refer back to.',
      },
    },
    required: ['goal'],
  },
  async run(args, ctx) {
    const goal = String(args?.goal || '').trim();
    if (!goal) return { ok: false, error: 'A background job needs a clear goal.' };

    if (!hasBackgroundCapacityNow()) {
      return { ok: true, at_capacity: true, running: runningSummaries() };
    }

    const { kind, title, plan } = await planAdmission(goal);
    const result = createJobIfCapacity({ title, goal, kind, conversationId: ctx?.sessionId || null, plan });
    if (result.atCapacity) {
      // Capacity changed between the check above and now (another job was
      // admitted in the moment spent on the admission call) — rare, but
      // handled the same honest way rather than assumed away.
      return { ok: true, at_capacity: true, running: result.running };
    }
    return { ok: true, job_id: result.job.id, title, plan, kind, status: result.job.status };
  },
};

// Internal, job-only tool — a background Job's own worker turn requesting
// that its goal be broken into separate, independent pieces (root
// CLAUDE.md's Jobs section: "a research task deciding it actually needs
// three separate searches"). Self-contained by design: judging the request
// (one model call — the Orchestrator's own rare reasoning boundary, not a
// steady-state cost) and creating the approved pieces both happen right
// here in run(), rather than threading a signal back through worker.js's
// core loop the way report_job_done/report_job_stuck's simpler true/false
// signals do — there was nothing for that loop to add.
//
// The depth ceiling is enforced structurally, not by a rule this file has
// to remember to check: `rootParentId` below always resolves to the ROOT
// ancestor in one hop (a level-2 job's OWN parentId already IS the root),
// so every piece this ever creates becomes a peer under that same root —
// never a child of the requesting job. A tree can therefore never exceed
// depth 2, full stop, no matter how many times a job at either level
// requests a further split.
//
// Same `internal`-not-`meta` reasoning as report_job_done.js: every job
// turn runs with background:true, and meta tools are stripped before
// allowedTools is even considered, so this could never be meta and still
// reach a job's own turn. See that file for the ctx.sessionId guard too.

import * as jobStore from '../jobs/job-store.js';
import { createJobIfCapacity } from '../jobs/job-actions.js';
import { jobEvents } from '../jobs/job-events.js';
import { addNotification } from '../notifications.js';
import { askModel } from '../ai.js';
import { getPrefs } from '../prefs.js';

const MAX_PIECES = 5;

const JUDGE_SYSTEM =
  'You are the Orchestrator of a background task system, judging whether a Worker\'s request to split its ' +
  'own job into separate pieces is proportionate — NOT whether the underlying goal is a good idea (that was ' +
  'already decided when the job was created). Approve only if the pieces are genuinely independent and doing ' +
  'them separately is a real improvement over one job handling all of it. Deny if the split is unnecessary, ' +
  'the pieces overlap significantly, or there are more pieces than the goal actually calls for. If you are ' +
  'not genuinely sure, deny — uncertainty is itself a reason not to approve.';

function judgePrompt(job, reason, pieces) {
  return (
    `Original job goal: ${job.goal}\n` +
    `Reason given for splitting: ${reason || '(none given)'}\n` +
    `Proposed pieces:\n${pieces.map((p, i) => `${i + 1}. ${p.goal}`).join('\n')}\n\n` +
    'Respond with JSON only: {"approved": true|false, "reason": "one sentence"}.'
  );
}

function notify(job, level, title, body) {
  try {
    addNotification({ kind: 'job', level, title, body, action: { label: 'View Jobs', section: 'jobs' }, meta: { jobId: job.id } });
  } catch (err) {
    console.error('[jobs] notification failed:', err);
  }
}

export default {
  name: 'request_job_split',
  internal: true,
  description:
    "Call this if the background job's goal would genuinely go better broken into separate, independent " +
    "pieces of work rather than done as one — e.g. three separate searches instead of one broad one. This is " +
    'NOT automatic: give a reason and describe each piece as its own complete, self-contained goal, and the ' +
    "Orchestrator will judge whether splitting is actually proportionate. If approved, each piece becomes its " +
    'own background job and THIS job is considered done — its work is now those pieces, so stop here rather ' +
    'than also continuing the original goal yourself. If denied, keep working on the goal directly instead.',
  parameters: {
    type: 'object',
    properties: {
      reason: { type: 'string', description: 'Why splitting this up would genuinely help.' },
      pieces: {
        type: 'array',
        minItems: 2,
        items: {
          type: 'object',
          properties: {
            title: { type: 'string' },
            goal: { type: 'string', description: 'A clear, complete, self-contained goal for this one piece.' },
          },
          required: ['goal'],
        },
        description: 'Each independent piece of work, at least two.',
      },
    },
    required: ['pieces'],
  },
  async run(args, ctx) {
    if (!ctx?.sessionId?.startsWith('job:')) {
      return { ok: false, error: 'request_job_split can only be called while working on a background job.' };
    }
    const jobId = ctx.sessionId.slice('job:'.length);
    const job = jobStore.getJob(jobId);
    if (!job) return { ok: false, error: 'Unknown job.' };

    const pieces = (Array.isArray(args?.pieces) ? args.pieces : [])
      .map((p) => ({ title: p?.title ? String(p.title).slice(0, 80) : null, goal: String(p?.goal || '').trim() }))
      .filter((p) => p.goal);
    const reason = String(args?.reason || '').trim();

    if (pieces.length < 2) {
      return { ok: true, approved: false, reason: 'At least two distinct pieces are needed to justify splitting.' };
    }
    if (pieces.length > MAX_PIECES) {
      return { ok: true, approved: false, reason: `Too many pieces (${pieces.length}, max ${MAX_PIECES}) for one split.` };
    }
    // Cheap, code-only check before spending the judgment model call — same
    // "check capacity before paying for reasoning" discipline as
    // work_in_background.js's hasBackgroundCapacityNow().
    const active = jobStore.listActiveJobs();
    if (active.length + pieces.length > getPrefs().maxBackgroundJobs) {
      return { ok: true, approved: false, reason: `Not enough capacity for ${pieces.length} more jobs right now.` };
    }

    const judgement = await askModel({ system: JUDGE_SYSTEM, prompt: judgePrompt(job, reason, pieces), json: true, background: true });
    const approved = judgement.ok && judgement.data?.approved === true;
    const judgeReason = judgement.ok ? String(judgement.data?.reason || '') : 'Could not confidently judge whether this split is proportionate.';

    if (!approved) {
      return { ok: true, approved: false, reason: judgeReason };
    }

    // Root ancestor in one hop — see this file's header comment.
    const rootParentId = job.parentId || job.id;
    const createdIds = [];
    for (const piece of pieces) {
      const result = createJobIfCapacity({
        title: piece.title || piece.goal.slice(0, 80),
        goal: piece.goal,
        kind: 'generic', // no nested admission call per piece — keeps the cost of a split bounded to one judgment call total
        parentId: rootParentId,
        conversationId: job.conversationId,
      });
      if (result.atCapacity) break; // capacity ran out mid-creation — stop rather than exceed it
      if (result.job) createdIds.push(result.job.id);
    }

    jobStore.appendTrace(jobId, {
      phase: 'outcome',
      effect: 'read',
      kind: 'decision',
      summary: `Split approved (${judgeReason}) — created ${createdIds.length} piece(s).`,
    });
    jobStore.updateJob(jobId, { status: 'done', result: `Split into ${createdIds.length} separate job(s): ${createdIds.join(', ')}` });
    jobEvents.emit('status', { jobId, status: 'done', title: job.title });
    notify(job, 'success', `"${job.title}" was split into ${createdIds.length} jobs`, judgeReason);

    return { ok: true, approved: true, summary: `Approved — split into ${createdIds.length} piece(s).`, new_job_ids: createdIds };
  },
};

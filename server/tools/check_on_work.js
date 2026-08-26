// Tier 3 pull ("what are you working on?" / "what's unfinished?") plus the
// one way a live conversation can act on a job parked `awaiting_decision`
// for `reason:'stuck'` — resuming it with fresh guidance. Deliberately
// imports only jobs/job-store.js and jobs/job-actions.js (both leaves) —
// never jobs/orchestrator.js; see job-actions.js's header comment for why.
// Resuming an `orphaned` job (a real crash, not a live stall) stays
// route-only for now — a rarer, crash-specific case this tool doesn't cover.

import * as jobStore from '../jobs/job-store.js';
import { resumeStuckJob } from '../jobs/job-actions.js';

function summarizeJob(job, outbox) {
  const pending = outbox.filter((o) => o.jobId === job.id && !o.deliveredAt);
  return {
    id: job.id,
    title: job.title,
    status: job.status,
    kind: job.kind,
    // Set only when this job is one piece of an approved split
    // (request_job_split.js) — lets the model say "this is part of a
    // larger job" rather than reporting each piece as unrelated work.
    parent_job_id: job.parentId,
    createdAt: job.createdAt,
    finishedAt: job.finishedAt,
    result: job.result,
    error: job.error,
    plan: job.plan,
    waiting_on_you: pending.length ? pending[0].summary : null,
  };
}

export default {
  name: 'check_on_work',
  core: true,
  meta: true,
  description:
    "Check what background work is running, finished, or waiting on the owner — use this when they ask what " +
    "you're working on, what's unfinished, or for an update on something backgrounded earlier. Pass job_id for " +
    "detail on one specific job; omit it to list everything recent. To act on a job that's waiting on a decision " +
    '(status "awaiting_decision"), pass job_id with respond:"keep_going" — optionally with guidance describing ' +
    'what to try differently — only after the owner has actually said to keep going.',
  parameters: {
    type: 'object',
    properties: {
      job_id: { type: 'string', description: 'A specific job to check or act on; omit to list all recent jobs.' },
      respond: { type: 'string', enum: ['keep_going'], description: 'Only with job_id: tells a job waiting on a decision to resume.' },
      guidance: { type: 'string', description: 'With respond:"keep_going" — what the owner said to try instead, in their own words.' },
    },
    required: [],
  },
  async run(args) {
    const jobId = args?.job_id ? String(args.job_id) : null;

    if (jobId && args?.respond === 'keep_going') {
      try {
        const job = resumeStuckJob(jobId, args?.guidance ? String(args.guidance) : null);
        return { ok: true, job_id: job.id, status: job.status };
      } catch (err) {
        return { ok: false, error: err?.message || 'Could not resume that job.' };
      }
    }

    if (jobId) {
      const job = jobStore.getJob(jobId);
      if (!job) return { ok: false, error: 'No job with that id.' };
      return { ok: true, job: summarizeJob(job, jobStore.getOutboxForJob(job.id)) };
    }

    const all = jobStore.listJobs({});
    const recent = all.slice(-20).reverse();
    const outboxByJob = recent.flatMap((j) => jobStore.getOutboxForJob(j.id));
    return { ok: true, jobs: recent.map((j) => summarizeJob(j, outboxByJob)) };
  },
};

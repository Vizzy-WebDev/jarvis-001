// Internal, job-only tool — the model's OWN signal that it is genuinely
// blocked and cannot proceed without the owner's input. Distinct from
// server/jobs/job-policy.js's diagnoseStall() (which infers a stall from
// repeated/failing/looping behavior without being told) — this is the
// worker self-reporting, which server/jobs/worker.js treats as a stronger
// signal: it escalates immediately (no automatic retry spent), since
// pushing a "try a different approach" nudge on a job that just explicitly
// said it needs the owner's input would be presumptuous. See this
// directory's report_job_done.js for why `internal` (not `meta`) is the
// right flag and why the ctx.sessionId check exists.

export default {
  name: 'report_job_stuck',
  internal: true,
  description:
    "Call this if you are genuinely blocked on the background job and cannot proceed without the owner's " +
    "input — e.g. a decision only they can make, missing access or information, or you've already tried " +
    'multiple different approaches and none worked. Explain what you tried and what you need.',
  parameters: {
    type: 'object',
    properties: {
      summary: { type: 'string', description: 'What is blocking you and what you already tried.' },
    },
    required: ['summary'],
  },
  async run(args, ctx) {
    if (!ctx?.sessionId?.startsWith('job:')) {
      return { ok: false, error: 'report_job_stuck can only be called while working on a background job.' };
    }
    return { ok: true, summary: String(args?.summary || '').trim() || 'Blocked.' };
  },
};

// Internal, job-only tool — a background Job's own worker turn (see
// server/jobs/worker.js) is the only thing that ever includes this name in
// its runTurn call's `allowedTools`, which is what actually makes it visible
// (see runner.js's runOnEntry: an explicit allowlist pre-unlocks exactly the
// names it lists, regardless of `core`). `internal: true` additionally keeps
// it out of the task/briefing action picker (capabilities.js's
// listCapabilities()/listStepCandidates()) even though it isn't `meta` —
// it CAN'T be meta, since every job turn runs with background:true, and
// meta tools are stripped before allowedTools is even considered.
//
// The ctx.sessionId check is defense in depth, not the primary guard: even
// if a live chat model somehow discovered this via find_capability's
// keyword search, calling it outside a job session is a harmless no-op.

export default {
  name: 'report_job_done',
  internal: true,
  description:
    "Call this ONLY when you have FULLY completed the background job's stated goal — not when you're " +
    "partway there or think it's close enough. Give a clear, specific summary of what you actually did " +
    'and what the result is; this is what the owner will be told.',
  parameters: {
    type: 'object',
    properties: {
      summary: { type: 'string', description: 'What was done and what the result is, in plain language.' },
    },
    required: ['summary'],
  },
  async run(args, ctx) {
    if (!ctx?.sessionId?.startsWith('job:')) {
      return { ok: false, error: 'report_job_done can only be called while working on a background job.' };
    }
    return { ok: true, summary: String(args?.summary || '').trim() || 'Done.' };
  },
};

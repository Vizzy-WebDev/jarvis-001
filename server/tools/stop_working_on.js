// Cancels a background Job outright, at the owner's request. Deliberately
// imports only jobs/job-store.js and jobs/job-actions.js (both leaves) —
// never jobs/orchestrator.js; see job-actions.js's header comment for why.

import * as jobStore from '../jobs/job-store.js';
import { cancelJob } from '../jobs/job-actions.js';

export default {
  name: 'stop_working_on',
  core: true,
  meta: true,
  description:
    'Stop a background job the owner no longer wants — works whether it is still running, waiting on a ' +
    'decision, or anything else. Use check_on_work first if you need to find the right job_id.',
  parameters: {
    type: 'object',
    properties: {
      job_id: { type: 'string', description: 'The job to stop.' },
    },
    required: ['job_id'],
  },
  async run(args) {
    const jobId = String(args?.job_id || '').trim();
    if (!jobId) return { ok: false, error: 'Which job? Use check_on_work to find the job_id.' };
    if (!jobStore.getJob(jobId)) return { ok: false, error: 'No job with that id.' };
    const job = cancelJob(jobId);
    return { ok: true, job_id: job.id, title: job.title, status: job.status };
  },
};

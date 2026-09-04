// The pull path into Environment awareness (root CLAUDE.md's Operational
// Awareness item 5) — a live, real picture of the machine Jarvis runs on
// and what it's currently able to reach, so a decision about whether to
// START something can be made BEFORE committing to it rather than finding
// out mid-task. Distinct from check_myself.js (that's about whether Jarvis
// ITSELF is malfunctioning; this is about the surrounding system).
//
// core:true (no reliable search-intent text — "can you handle a heavy task
// right now" doesn't map to a capability search) + meta:true
// (live-conversation-only; a background Job's own turn making this decision
// for itself would need a different, narrower check, not this one).

import { sampleNow } from '../ops/environment/sampler.js';
import { fullReachability } from '../ops/environment/reachability.js';
import { checkForAnomaly } from '../ops/environment/baseline.js';

export default {
  name: 'check_environment',
  core: true,
  meta: true,
  description:
    'Check the real, live state of the machine and services you depend on before starting something that needs them — CPU/memory ' +
    'load right now, whether a specific model/connector/voice service is actually reachable, and whether load has been unusually ' +
    'high or climbing lately. Use this before committing to a heavy or long-running task, not just when asked directly about system health.',
  parameters: {
    type: 'object',
    properties: {},
    required: [],
  },
  async run() {
    const load = sampleNow({ record: false });
    const reachability = fullReachability();
    const anomaly = checkForAnomaly();

    return {
      ok: true,
      loadRightNow: {
        cpuPct: load.cpuPct,
        memFreePct: load.memFreePct !== null ? Math.round(load.memFreePct * 10) / 10 : null,
        jarvisOwnMemoryBytes: load.rssBytes,
      },
      unusualLoad: anomaly ? { summary: anomaly.summary } : null,
      reachability,
    };
  },
};

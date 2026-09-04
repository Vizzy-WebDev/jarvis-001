// The conversational read path into self-diagnosis (root CLAUDE.md's
// Operational Awareness item 1) — "has anything gone wrong with you
// lately?", "are you working correctly?" Runs every registered check live,
// right now, AND surfaces recent real history from the activity trace —
// distinct from check_environment.js (the surrounding MACHINE) and
// check_myself.js (Self-Model's own reliability/provenance/goal dimensions,
// nothing about malfunction or possible compromise).
//
// core:true (no reliable search-intent text) + meta:true
// (live-conversation-only). No confirm gate — every check here is
// read-only; the one thing that ever WRITES anything (a check's own
// remedy()) already ran, if at all, on the Heartbeat's own background tick,
// never triggered by this tool.

import { listChecks } from '../ops/diagnostics/registry.js';
import { listRecentForSource } from '../ops/ops-trace.js';

export default {
  name: 'check_my_health',
  core: true,
  meta: true,
  description:
    'Check whether you are actually working correctly right now, and whether anything real has gone wrong recently — Memory writing, ' +
    'background Jobs and the Scheduler actually progressing, the database itself, the voice pipeline\'s server-side half, and signs of ' +
    'possible tampering (an unexplained change to a secret/config file, a spike in auth failures or confirm-gate bypass attempts, an ' +
    'unexpected new local listener). This is about YOUR OWN malfunction or compromise, not the surrounding machine — use ' +
    'check_environment for that. Never answer "am I working fine" from impression — check here first.',
  parameters: {
    type: 'object',
    properties: {},
    required: [],
  },
  async run() {
    const checks = listChecks();
    const results = [];
    for (const c of checks) {
      let result;
      try {
        result = await c.probe();
      } catch (err) {
        result = { ok: false, detail: `Probe threw: ${err?.message || err}` };
      }
      results.push({ id: c.id, ok: result.ok !== false, detail: result.detail || null });
    }

    const failing = results.filter((r) => !r.ok);
    const recentHistory = listRecentForSource('diagnosis', { limit: 20 });

    return {
      ok: true,
      allHealthy: failing.length === 0,
      checkedJustNow: results,
      recentActivity: recentHistory.map((t) => ({ checkId: t.sourceRef, phase: t.phase, kind: t.kind, summary: t.summary, at: t.createdAt })),
      note:
        failing.length > 0
          ? 'One or more checks are failing right now — say so plainly, don\'t soften it, and name which ones.'
          : undefined,
    };
  },
};

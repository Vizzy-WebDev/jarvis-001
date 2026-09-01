// "What have you learned/changed recently?" — the honest, current answer,
// same spirit as prompt.js's connectorsSection()/skillsSection(): a real
// question with a real answer available, rather than the model having to
// guess or fold this into vague self-conception. core:true (no reliable
// user search-intent text — "what have you learned" doesn't obviously map
// to a capability search the way "what time is it" maps to "time") +
// meta:true (only makes sense asked by the user, live).

import { listLessons, listRules, listProposals, listChanges } from '../improvement/improvement-store.js';

export default {
  name: 'review_improvements',
  core: true,
  meta: true,
  description:
    "Look up what Jarvis has actually learned, changed, or is currently suggesting about how it works — use this whenever the user " +
    'asks what you\'ve learned, what you\'ve changed recently, or what suggestions are pending. Always use this instead of answering ' +
    'from memory — the real log is here.',
  parameters: { type: 'object', properties: {}, required: [] },
  async run() {
    const rules = listRules({ activeOnly: true });
    const pendingProposals = listProposals({ status: 'pending' });
    const recentChanges = listChanges({ limit: 10 });
    const recentLessons = listLessons({ status: 'active' }).slice(0, 10);
    return {
      ok: true,
      active_rules: rules.map((r) => ({ id: r.id, text: r.text, scope: r.scope })),
      pending_suggestions: pendingProposals.map((p) => ({ id: p.id, kind: p.kind, title: p.title, rationale: p.rationale })),
      recent_changes: recentChanges.map((c) => ({ id: c.id, kind: c.kind, target: c.target, reason: c.reason, appliedAt: c.appliedAt, undone: Boolean(c.undoneAt) })),
      recently_noticed: recentLessons.map((l) => ({ text: l.text, scope: l.scope })),
    };
  },
};

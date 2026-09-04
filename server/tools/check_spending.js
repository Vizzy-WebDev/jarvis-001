// The conversational read path into cost tracking (root CLAUDE.md's
// Operational Awareness item 2) — "how much have I spent this month",
// "which model do I use most", "what's left on ElevenLabs". Deliberately no
// dedicated screen for this version (owner's own explicit choice) — this
// tool IS the interface.
//
// core:true (no reliable search-intent text — "how much have I spent"
// doesn't map to a capability search the way "what time is it" maps to
// "time") + meta:true (a live-conversation-only question; a scheduled task
// or a background Job's own turn has no reason to ask this about itself).
// No confirm gate — purely read-only, including the optional balance
// refresh (a GET request against a provider's own account endpoint, same
// risk class as check_myself's own reads).

import * as report from '../cost/report.js';
import { refreshAllBalances } from '../cost/balances.js';

export default {
  name: 'check_spending',
  core: true,
  meta: true,
  description:
    'Check real, measured spend and usage on the paid services you depend on — model API usage, ElevenLabs, Deepgram, or anything ' +
    'else that costs money to run. Answers "how much have I spent," "which model do I use most," "what\'s my ElevenLabs balance." ' +
    'Every number is labelled by kind: MEASURED (what was actually counted), PROVIDER-REPORTED (a real balance figure straight from ' +
    'the provider), or CALCULATED (measured usage × a known price) — never blend these together, and never state a dollar figure ' +
    'for anything this tool reports as having no known price.',
  parameters: {
    type: 'object',
    properties: {
      period: {
        type: 'string',
        enum: ['today', 'month', 'all'],
        description: 'Which window to report on. Defaults to "month" (the natural "how much have I spent this month" scope).',
      },
      refreshBalances: {
        type: 'boolean',
        description:
          'Set true to poll providers for a fresh real balance reading before answering (a real network call to each provider\'s ' +
          'own account endpoint) — otherwise the last known reading is used, which may be stale or entirely absent if none has ' +
          'ever run.',
      },
    },
    required: [],
  },
  async run(args = {}) {
    const period = ['today', 'month', 'all'].includes(args.period) ? args.period : 'month';

    if (args.refreshBalances) {
      try {
        await refreshAllBalances();
      } catch (err) {
        console.error('[check_spending] balance refresh failed — falling back to the last known reading:', err);
      }
    }

    const breakdown =
      period === 'today' ? report.today() : period === 'all' ? report.usageBreakdown('1970-01-01T00:00:00.000Z') : report.monthToDate();
    const mostUsed = report.mostUsed(breakdown.since);
    const balances = report.providerBalances();

    return {
      ok: true,
      period,
      measured: breakdown.measured,
      calculated: breakdown.calculated,
      pricelessGroups: breakdown.pricelessGroups,
      mostUsed,
      providerBalances: balances,
      note:
        balances.length === 0
          ? 'No provider balance has ever been read yet — pass refreshBalances:true to poll now, or wait for the next automatic refresh (every 6 hours).'
          : undefined,
    };
  },
};

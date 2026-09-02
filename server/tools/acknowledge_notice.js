// Marks a Heartbeat/Trigger finding (server/heartbeat/) as delivered once
// Jarvis has actually told the owner about it in a live turn — the
// resolving action a source:'heartbeat' outbox row has no equivalent of
// otherwise (a background Job's own tier1/2 rows resolve via
// check_on_work/stop_working_on instead; see prompt.js's jobsSection() for
// the full wording). No confirm gate: recording that something was said has
// no outward effect of its own to protect.
//
// Leaf-safe: imports only heartbeat/outbox-store.js, itself a leaf.

import { getById, markDelivered } from '../heartbeat/outbox-store.js';

export default {
  name: 'acknowledge_notice',
  core: true,
  meta: true,
  description:
    'Call this right after you have actually told the owner about a background notice mentioned in your own instructions ' +
    '(a notice_id shown alongside it) — marks it delivered so it is not brought up again. Never call this for a background ' +
    'Job (use check_on_work/stop_working_on for those instead).',
  parameters: {
    type: 'object',
    properties: {
      notice_id: { type: 'string', description: 'The notice_id shown alongside the finding you just mentioned.' },
    },
    required: ['notice_id'],
  },
  async run(args) {
    const id = Number(args?.notice_id);
    if (!Number.isFinite(id)) return { ok: false, error: 'That notice_id was not valid.' };
    const entry = getById(id);
    if (!entry) return { ok: false, error: 'No notice with that id — it may already be resolved.' };
    if (entry.source !== 'heartbeat') return { ok: false, error: 'That was a background Job, not a notice — use check_on_work or stop_working_on instead.' };
    if (!entry.deliveredAt) markDelivered(id);
    return { ok: true };
  },
};

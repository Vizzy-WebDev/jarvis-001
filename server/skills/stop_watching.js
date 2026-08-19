// Skill: stops an active watch by voice — the spoken counterpart to the
// Stop control on each monitor in the app (POST /api/monitors/:id/stop,
// same underlying engine.stopWatching()). Only ever touches monitors
// currently 'watching'; one that's already triggered/expired/stopped is
// left alone.

import { listActiveMonitors } from '../monitor/monitor-store.js';
import { stopWatching } from '../monitor/engine.js';
import { broadcast } from '../events.js';

export default {
  name: 'stop_watching',
  meta: true, // same reasoning as watch_for.js — only makes sense live
  description: 'Stop an active watch the user previously started with watch_for. Use when they ask to stop watching, or cancel a watch.',
  parameters: {
    type: 'object',
    properties: {
      description: { type: 'string', description: "Which watch to stop, in the user's own words — matched against what's currently active. Leave out if there's clearly only one." },
    },
    required: [],
  },
  async run({ description } = {}) {
    const active = listActiveMonitors();
    if (!active.length) return { ok: false, error: "Nothing is being watched right now." };

    let target = active[0];
    if (active.length > 1) {
      const needle = String(description || '').trim().toLowerCase();
      target = needle ? active.find((m) => m.description.toLowerCase().includes(needle)) : null;
      if (!target) {
        return {
          ok: false,
          error: `More than one watch is active — which one? ${active.map((m) => `"${m.description}"`).join(', ')}.`,
        };
      }
    }

    stopWatching(target.id);
    // Mirrors the UI's own Stop button (server.js's POST /api/monitors/:id/stop
    // → stopWatching() directly) — this is the voice path to the same place,
    // so it needs to tell the amber bar too, not just the store.
    broadcast({ type: 'monitor_stopped', monitorId: target.id, description: target.description });
    return { ok: true, stopped: target.description };
  },
};

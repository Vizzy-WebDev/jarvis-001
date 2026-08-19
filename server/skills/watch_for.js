// Skill: starts watching for a specific condition, acting once it's met.
// Only ever starts because the user explicitly asked to watch for something
// — never something Jarvis runs on its own in the background.
//
// Imports only monitor/monitor-store.js (a leaf module) and monitor/
// engine.js (imports ps-bridge/ai/events/monitor-store, never runner.js or
// skills/index.js) — never models/runner.js directly, so this stays safe
// under CLAUDE.md's circular-import invariant. The actual follow-up action
// for an 'act' monitor is dispatched by server.js when it sees the
// engine's 'monitor_triggered' broadcast — kept out of this file and out of
// engine.js specifically so neither ever has to import runner.js.

import { createMonitor } from '../monitor/monitor-store.js';
import { startWatching } from '../monitor/engine.js';
import { askModel } from '../ai.js';
import { broadcast } from '../events.js';

const TRANSLATE_SYSTEM = `You turn a plain-language "watch for X" request into one structured check Jarvis's monitor engine can evaluate. Reply with JSON only, one of two shapes:

{"kind": "...", "description": "short plain description of what's being watched for", ...params}

Valid kinds and their params (include only the ones listed for that kind):
- window_appears: processName?, titleContains — a matching window shows up
- window_gone: processName?, titleContains — a matching window closes
- window_title_matches: processName?, titlePattern — an existing window's title changes to include this text
- process_running: processName
- process_gone: processName
- file_exists: path — an exact, full file path
- file_size_stable: path — an exact, full file path; prefer this over file_exists whenever the request is about something finishing being WRITTEN (a download, an export, a render) rather than merely appearing
- element_text_matches: processName?, titleContains?, textContains
- screen_looks_like: description, processName?, titleContains? — LAST RESORT, only when nothing else could possibly answer it (a genuine visual judgment call)

Or, if you cannot determine a concrete, complete set of params for any kind (e.g. no file path, process, or window name was actually given or clearly implied):
{"needsClarification": "<one short, specific question to ask the user>"}

Never invent a file path, process name, or window title that wasn't actually stated or strongly implied by the request.`;

export default {
  name: 'watch_for',
  // Meta skill — watching only makes sense as something started live, the
  // same reasoning schedule_task.js/cancel_task.js already apply; a
  // scheduled task has its own separate "wait, then do something"
  // mechanism (recurrence), so offering this as a task/briefing action
  // would just be a confusing second way to do the same thing.
  meta: true,
  description:
    'Watch for a specific condition and act once it\'s met — e.g. "tell me when this download finishes" or ' +
    '"when this finishes, open the folder". Only use this when the user explicitly asks Jarvis to watch for ' +
    'something specific; never start one on your own.',
  parameters: {
    type: 'object',
    properties: {
      condition: { type: 'string', description: "What to watch for, in the user's own words." },
      action: {
        type: 'string',
        description: 'Optional — what to do once the condition is met, in the user\'s own words. Leave out if they just want to be told.',
      },
    },
    required: ['condition'],
  },
  async run({ condition, action }) {
    const text = String(condition || '').trim();
    if (!text) return { ok: false, error: "I didn't catch what to watch for." };

    const result = await askModel({ prompt: `Watch for: "${text}"`, system: TRANSLATE_SYSTEM, json: true });
    if (!result.ok) return { ok: false, error: result.error };

    const data = result.data || {};
    if (data.needsClarification) {
      return { ok: false, error: data.needsClarification };
    }
    if (!data.kind) {
      return { ok: false, error: "I couldn't work out a concrete way to watch for that — could you be more specific?" };
    }

    const { kind, description, ...params } = data;
    const monitor = createMonitor({
      description: description || text,
      check: { kind, ...params },
      onTrigger: action ? { mode: 'act', instruction: action } : { mode: 'notify' },
    });
    startWatching(monitor.id);
    // The amber "Watching for" bar (public/app.js) has no other way to know
    // a watch just started — engine.js only ever broadcasts at the END of
    // one (triggered/expired), since it has no reason to fire an event on
    // every unchanged tick.
    broadcast({ type: 'monitor_started', monitorId: monitor.id, description: monitor.description });

    return {
      ok: true,
      monitorId: monitor.id,
      watching: monitor.description,
      spoken_hint: "Confirm briefly that you're watching for it now. Don't describe the technical check — just say what you're watching for in plain words.",
    };
  },
};

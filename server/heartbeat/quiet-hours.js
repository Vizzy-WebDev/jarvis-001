// Quiet hours — a fixed schedule the user sets (prefs.js's `quietHours`),
// read by decision.js before anything Tier 1 is delivered live. This file
// only answers "is it quiet right now" — the emergency override that can
// still break through IS the reasoning in decision.js's own model call, not
// a second mechanism here (see root CLAUDE.md's Heartbeat section on why
// the bar for that must be noticeably higher than daytime "important").
//
// Leaf-adjacent: imports only prefs.js (itself a leaf).

import { getPrefs } from '../prefs.js';

function toMinutes(hhmm) {
  const m = /^(\d{1,2}):(\d{2})$/.exec(String(hhmm || '').trim());
  if (!m) return null;
  const h = Number(m[1]);
  const min = Number(m[2]);
  if (h < 0 || h > 23 || min < 0 || min > 59) return null;
  return h * 60 + min;
}

/** True if `now` falls inside the configured quiet window. A malformed or disabled setting is treated as "never quiet" — the safe direction is to under-suppress a setting the user hasn't actually configured correctly, not to silently withhold everything. */
export function isQuietNow(now = new Date(), prefsOverride) {
  const quietHours = prefsOverride ?? getPrefs().quietHours;
  if (!quietHours?.enabled) return false;
  const start = toMinutes(quietHours.start);
  const end = toMinutes(quietHours.end);
  if (start === null || end === null || start === end) return false;
  const nowMin = now.getHours() * 60 + now.getMinutes();
  if (start < end) return nowMin >= start && nowMin < end;
  return nowMin >= start || nowMin < end; // wraps midnight, e.g. 23:00 -> 08:00
}

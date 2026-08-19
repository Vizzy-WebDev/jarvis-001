// Pure, dependency-free scheduling math — no state, no I/O, directly
// testable with a one-off `node --input-type=module` script (see
// CLAUDE.md's testing guidance). Deliberately hand-rolled instead of
// reaching for a cron library: the shape set below is small, and computing
// it ourselves means describe() can produce the exact plain-English
// sentence the UI and voice read-back need, for free.
//
// A recurrence spec is one of:
//   { type: 'once',     at: isoString }
//   { type: 'daily',    time: 'HH:MM' }
//   { type: 'weekdays', time: 'HH:MM' }                  // Mon-Fri
//   { type: 'weekly',   time: 'HH:MM', days: [0-6] }     // 0 = Sunday
//   { type: 'interval', everyMs: number, anchor?: isoString }
//
// All "time of day" math uses the server's local clock — this runs on the
// user's own PC, so their local time is the only one that makes sense.

const DAY_NAMES = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'];
const WEEKDAY_NUMS = [1, 2, 3, 4, 5];

function parseTime(time) {
  const [h, m] = String(time || '00:00').split(':').map((n) => parseInt(n, 10));
  return { h: Number.isFinite(h) ? h : 0, m: Number.isFinite(m) ? m : 0 };
}

function atTime(date, time) {
  const { h, m } = parseTime(time);
  const d = new Date(date);
  d.setHours(h, m, 0, 0);
  return d;
}

function nextDailyLike(from, time, allowedDays) {
  let candidate = atTime(from, time);
  for (let i = 0; i < 8; i++) {
    if (candidate.getTime() >= from.getTime() && (!allowedDays || allowedDays.includes(candidate.getDay()))) {
      return candidate;
    }
    candidate = new Date(candidate.getTime() + 24 * 60 * 60 * 1000);
    candidate = atTime(candidate, time); // re-pin the clock time after the day rolls over (handles DST)
  }
  return null; // shouldn't happen with a valid allowedDays list
}

/** Next occurrence at or after `from` (a Date), or null if the spec has no more occurrences. */
export function nextRunAt(spec, from = new Date()) {
  if (!spec || !spec.type) return null;

  switch (spec.type) {
    case 'once': {
      const at = new Date(spec.at);
      if (Number.isNaN(at.getTime())) return null;
      return at.getTime() >= from.getTime() ? at : null;
    }
    case 'daily':
      return nextDailyLike(from, spec.time, null);
    case 'weekdays':
      return nextDailyLike(from, spec.time, WEEKDAY_NUMS);
    case 'weekly': {
      const days = Array.isArray(spec.days) && spec.days.length ? spec.days : [from.getDay()];
      return nextDailyLike(from, spec.time, days);
    }
    case 'interval': {
      const everyMs = Number(spec.everyMs);
      if (!everyMs || everyMs <= 0) return null;
      const anchor = spec.anchor ? new Date(spec.anchor).getTime() : from.getTime();
      if (Number.isNaN(anchor)) return null;
      const elapsed = from.getTime() - anchor;
      const steps = Math.max(0, Math.ceil(elapsed / everyMs));
      return new Date(anchor + steps * everyMs);
    }
    default:
      return null;
  }
}

function formatTime(time) {
  const { h, m } = parseTime(time);
  const d = new Date();
  d.setHours(h, m, 0, 0);
  return d.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' });
}

/** A plain-English description, for the UI and for Jarvis's voice read-back. */
export function describe(spec) {
  if (!spec || !spec.type) return 'never';
  switch (spec.type) {
    case 'once': {
      const at = new Date(spec.at);
      return Number.isNaN(at.getTime())
        ? 'once'
        : `once, on ${at.toLocaleDateString('en-US', { weekday: 'long', month: 'long', day: 'numeric' })} at ${at.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' })}`;
    }
    case 'daily':
      return `every day at ${formatTime(spec.time)}`;
    case 'weekdays':
      return `weekdays at ${formatTime(spec.time)}`;
    case 'weekly': {
      const days = Array.isArray(spec.days) && spec.days.length ? spec.days : [];
      const names = days.map((d) => DAY_NAMES[d]).filter(Boolean);
      return `every ${names.join(', ') || 'week'} at ${formatTime(spec.time)}`;
    }
    case 'interval': {
      const mins = Math.round((Number(spec.everyMs) || 0) / 60000);
      if (mins > 0 && mins % 1440 === 0) return `every ${mins / 1440} day${mins / 1440 === 1 ? '' : 's'}`;
      if (mins > 0 && mins % 60 === 0) return `every ${mins / 60} hour${mins / 60 === 1 ? '' : 's'}`;
      return `every ${mins} minute${mins === 1 ? '' : 's'}`;
    }
    default:
      return 'never';
  }
}

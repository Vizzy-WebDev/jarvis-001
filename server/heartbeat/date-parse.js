// A small, hand-written, zero-dependency deterministic date/time extractor
// over free text — the always-on, zero-quota path for
// sources/commitments-source.js. Deliberately NOT exhaustive: this covers
// the common, plainly-worded cases ("by Friday," "tomorrow," an explicit
// date) and is the SAFETY NET a budgeted model call sits behind for
// phrasing this misses, not a claim of complete coverage — see root
// CLAUDE.md's Heartbeat section. Pure function, no imports, directly
// testable with a bare `node --input-type=module -e` script.

const MONTHS = [
  'january', 'february', 'march', 'april', 'may', 'june',
  'july', 'august', 'september', 'october', 'november', 'december',
];
const MONTH_ABBR = MONTHS.map((m) => m.slice(0, 3));
const WEEKDAYS = ['sunday', 'monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday'];

/** Cheap prefilter — is this text worth running the full parser (or a model call) over at all? */
export function looksTimeReferencing(text) {
  const t = String(text || '').toLowerCase();
  if (!t) return false;
  if (/\b(today|tonight|tomorrow|next week|by|before|due|deadline)\b/.test(t)) return true;
  if (WEEKDAYS.some((d) => t.includes(d))) return true;
  if (MONTHS.some((m) => t.includes(m)) || MONTH_ABBR.some((m) => new RegExp(`\\b${m}\\b`).test(t))) return true;
  if (/\b\d{1,2}\/\d{1,2}(\/\d{2,4})?\b/.test(t)) return true;
  if (/\b\d{4}-\d{2}-\d{2}\b/.test(t)) return true;
  if (/\bthe\s+\d{1,2}(st|nd|rd|th)\b/.test(t)) return true;
  return false;
}

function endOfDay(d) {
  const x = new Date(d);
  x.setHours(23, 59, 59, 999);
  return x;
}

function dateOnly(year, monthIndex, day) {
  const d = new Date(year, monthIndex, day, 23, 59, 59, 999);
  return Number.isNaN(d.getTime()) ? null : d;
}

/**
 * Extracts the single most likely deadline from `text`, relative to `now`
 * (injected for testability — never `new Date()` inside a pure module read
 * by anything that might run inside a Workflow script, per this project's
 * own convention). Returns `{ iso, confidence }` or `null`.
 *
 * Checked in priority order — the first pattern that matches wins, since a
 * more specific / explicit phrasing (an actual date) is more trustworthy
 * than a vague one ("next week") if a memory happens to contain both.
 */
export function parseDeadline(text, now = new Date()) {
  const t = String(text || '').toLowerCase();
  if (!t) return null;

  // Explicit ISO date: 2026-09-15
  let m = t.match(/\b(\d{4})-(\d{2})-(\d{2})\b/);
  if (m) {
    const d = dateOnly(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
    if (d) return { iso: d.toISOString(), confidence: 'high' };
  }

  // MM/DD or MM/DD/YYYY
  m = t.match(/\b(\d{1,2})\/(\d{1,2})(?:\/(\d{2,4}))?\b/);
  if (m) {
    const year = m[3] ? (m[3].length === 2 ? 2000 + Number(m[3]) : Number(m[3])) : now.getFullYear();
    const d = dateOnly(year, Number(m[1]) - 1, Number(m[2]));
    if (d) return { iso: d.toISOString(), confidence: 'high' };
  }

  // "tonight" / "today"
  if (/\btonight\b/.test(t) || /\btoday\b/.test(t)) {
    return { iso: endOfDay(now).toISOString(), confidence: 'medium' };
  }

  // "tomorrow"
  if (/\btomorrow\b/.test(t)) {
    const d = new Date(now);
    d.setDate(d.getDate() + 1);
    return { iso: endOfDay(d).toISOString(), confidence: 'medium' };
  }

  // "by next week" / "next week" — fuzzy, low confidence: 7 days out.
  if (/\bnext week\b/.test(t)) {
    const d = new Date(now);
    d.setDate(d.getDate() + 7);
    return { iso: endOfDay(d).toISOString(), confidence: 'low' };
  }

  // "by/before/due/on <weekday>" — the next occurrence of that weekday,
  // treating a same-day mention as today (a commitment noted on the day it's
  // due almost always means "by end of today," not "a week from now").
  const weekdayIdx = WEEKDAYS.findIndex((d) => new RegExp(`\\b${d}\\b`).test(t));
  if (weekdayIdx !== -1) {
    const d = new Date(now);
    const diff = (weekdayIdx - d.getDay() + 7) % 7;
    d.setDate(d.getDate() + diff);
    return { iso: endOfDay(d).toISOString(), confidence: 'medium' };
  }

  // Month name + day: "march 15", "15th of march", "mar 15"
  const monthPattern = `(${MONTHS.join('|')}|${MONTH_ABBR.join('|')})`;
  m = t.match(new RegExp(`\\b${monthPattern}\\s+(\\d{1,2})(?:st|nd|rd|th)?\\b`)) || t.match(new RegExp(`\\b(\\d{1,2})(?:st|nd|rd|th)?\\s+of\\s+${monthPattern}\\b`));
  if (m) {
    const monthText = MONTHS.includes(m[1]) || MONTH_ABBR.includes(m[1]) ? m[1] : m[2];
    const dayText = MONTHS.includes(m[1]) || MONTH_ABBR.includes(m[1]) ? m[2] : m[1];
    const monthIndex = MONTHS.indexOf(monthText) !== -1 ? MONTHS.indexOf(monthText) : MONTH_ABBR.indexOf(monthText);
    let year = now.getFullYear();
    let d = dateOnly(year, monthIndex, Number(dayText));
    // A bare month+day that already passed this year almost always means
    // NEXT year's occurrence, not last year's — e.g. "March 15" mentioned in
    // November.
    if (d && d.getTime() < now.getTime()) d = dateOnly(year + 1, monthIndex, Number(dayText));
    if (d) return { iso: d.toISOString(), confidence: 'high' };
  }

  // Bare "the 15th" — no month named, assume current month, roll to next
  // month if that day already passed.
  m = t.match(/\bthe\s+(\d{1,2})(st|nd|rd|th)\b/);
  if (m) {
    let d = dateOnly(now.getFullYear(), now.getMonth(), Number(m[1]));
    if (d && d.getTime() < now.getTime()) d = dateOnly(now.getFullYear(), now.getMonth() + 1, Number(m[1]));
    if (d) return { iso: d.toISOString(), confidence: 'medium' };
  }

  return null;
}

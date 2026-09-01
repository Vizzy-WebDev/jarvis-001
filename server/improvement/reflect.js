// Reflection — one batched model call over recently completed work,
// extracting individual OBSERVATIONS ("lessons"). A lesson never changes
// behaviour by itself; it's raw material for synthesize.js, which looks
// ACROSS the growing lesson set for something that genuinely recurred
// before ever proposing a rule (see db.js's migration-7 comment for the
// full lessons-vs-rules reasoning). Same batched-call discipline as
// memory/memory-review.js: one askModel call per cycle, never per outcome.
//
// Not a leaf module (imports ai.js) — safe to import from cycle.js only.

import { askModel } from '../ai.js';
import * as store from './improvement-store.js';
import { getPrefs } from '../prefs.js';

// A single call never reviews more than this many outcomes — keeps the
// prompt bounded regardless of how large the unreviewed backlog grows
// (e.g. after the improvement system was off for a while).
const MAX_OUTCOMES_PER_BATCH = 40;

// The hard cadence floor, independent of how many outcomes are waiting —
// this is what actually prevents the runaway the design's own cost audit
// flagged: a 15-minute tick with only a count-based gate could fire on
// nearly every tick on a busy day. Nothing about reflection ever runs
// faster than this, regardless of trust level, evidence backlog, or
// anything else.
const MIN_HOURS_BETWEEN_REFLECTIONS = 4;
// The "even one is worth reviewing eventually" floor — a lone outcome
// doesn't justify spending a call right away, but waiting indefinitely for
// a fifth one on a quiet week isn't right either.
const WORTH_RUNNING_AFTER_HOURS = 24;
const WORTH_RUNNING_AT_COUNT = 5;

function hoursSince(iso) {
  if (!iso) return Infinity;
  const parsed = Date.parse(iso);
  return Number.isFinite(parsed) ? (Date.now() - parsed) / 3600000 : Infinity;
}

/** Pure decision, no side effects — whether a reflection pass is even worth attempting right now. Exported for tests. */
export function shouldReflect() {
  if (!getPrefs().improvementEnabled) return false;
  const hrs = hoursSince(store.getLastRunAt('reflect'));
  if (hrs < MIN_HOURS_BETWEEN_REFLECTIONS) return false; // hard floor, no override
  const unreviewed = store.countUnreviewedOutcomes();
  if (unreviewed === 0) return false;
  return unreviewed >= WORTH_RUNNING_AT_COUNT || hrs >= WORTH_RUNNING_AFTER_HOURS;
}

function normalizeText(s) {
  return String(s || '')
    .trim()
    .toLowerCase()
    .replace(/[^\w\s]/g, '')
    .replace(/\s+/g, ' ');
}

function isDuplicateOfExisting(text, existingLessons) {
  const normalized = normalizeText(text);
  return existingLessons.some((l) => {
    const other = normalizeText(l.text);
    return Boolean(normalized && other && (normalized.includes(other) || other.includes(normalized)));
  });
}

function describeOutcome(o) {
  const bits = [`[${o.id}]`, `source=${o.source}`];
  if (o.kind) bits.push(`kind=${o.kind}`);
  bits.push(`status=${o.status}`);
  if (o.retries) bits.push(`retries=${o.retries}`);
  if (o.escalations) bits.push(`escalations=${o.escalations}`);
  if (o.entityRef && o.source === 'task') bits.push(`task_id=${o.entityRef}`);
  if (o.error) bits.push(`error="${String(o.error).slice(0, 200)}"`);
  if (o.goal) bits.push(`goal="${String(o.goal).slice(0, 200)}"`);
  return `- ${bits.join(' ')}`;
}

/** Only the scopes reflect.js can HONESTLY infer from the raw outcome fields it was given — never lets the model invent a tool/task id it wasn't handed. */
function validScope(scope, outcomes) {
  if (scope === 'general') return true;
  const jobKindMatch = /^job_kind:(.+)$/.exec(scope || '');
  if (jobKindMatch) return outcomes.some((o) => o.kind === jobKindMatch[1]);
  const taskMatch = /^task:(.+)$/.exec(scope || '');
  if (taskMatch) return outcomes.some((o) => o.entityRef === taskMatch[1]);
  return false;
}

/**
 * The actual work — spends ONE model call if (and only if) shouldReflect()
 * says it's worth it AND the daily budget has room. Returns
 * `{ranAt, lessonsCreated}` or null if it skipped (either gate failed, or
 * the budget was already spent — the caller, cycle.js, never needs to
 * distinguish why; both mean "nothing happened this tick").
 */
export async function maybeReflect() {
  if (!shouldReflect()) return null;
  if (!store.tryConsumeDailyBudget()) return null;
  store.setLastRunAt('reflect');

  const outcomes = store.listUnreviewedOutcomes({ limit: MAX_OUTCOMES_PER_BATCH });
  if (!outcomes.length) return { ranAt: new Date().toISOString(), lessonsCreated: 0 };

  const existingLessons = store.listLessons({ status: 'active' });
  const lessonsBlock = existingLessons.length ? existingLessons.map((l) => `- (${l.scope}) ${l.text}`).join('\n') : '(none yet)';
  const outcomesBlock = outcomes.map(describeOutcome).join('\n');

  const prompt = [
    "Below is a batch of Jarvis's own recently completed work — background jobs, scheduled tasks, and moments where the user corrected Jarvis in conversation.",
    'Look for anything genuinely worth noting about how a SPECIFIC KIND of work tends to go, or a specific recurring task — not a passing detail, something that would actually help next time similar work comes up. A single outcome CAN be worth noting on its own if it is a clear, concrete miss.',
    '',
    'Already-noted observations — do NOT repeat one of these, even reworded:',
    lessonsBlock,
    '',
    'Outcomes:',
    outcomesBlock,
    '',
    'Reply with JSON: {"lessons": [{"text": "...", "scope": "...", "evidenceIds": ["..."], "confidence": 0.0-1.0}]}.',
    '"scope": "general" for something true regardless of what kind of work it is; "job_kind:<kind>" ONLY using a kind value that actually appears above; "task:<task_id>" ONLY using a task_id value that actually appears above. Never invent a kind or id that is not literally present in the outcomes.',
    '"evidenceIds": the bracketed id(s) of the outcome(s) above that actually support this observation — never invent one, never list an id not shown above.',
    '"confidence": 0.8+ only when the outcomes clearly and repeatedly show the same thing; lower for a single instance or something you are inferring rather than seeing directly.',
    'An empty lessons array is a completely normal answer if nothing stands out. Never invent a pattern the outcomes do not actually support.',
  ].join('\n');

  const result = await askModel({
    prompt,
    system:
      "You review Jarvis's own recent work outcomes and note real, concrete observations worth remembering for next time — never a vague generality, never something the data does not actually show.",
    json: true,
    background: true,
  });

  let lessonsCreated = 0;
  if (result.ok && result.data && Array.isArray(result.data.lessons)) {
    for (const raw of result.data.lessons) {
      const text = String(raw?.text || '').trim();
      if (!text) continue;
      if (isDuplicateOfExisting(text, existingLessons)) continue;

      const outcomeIds = new Set(outcomes.map((o) => o.id));
      const evidence = Array.isArray(raw?.evidenceIds) ? raw.evidenceIds.filter((id) => outcomeIds.has(id)) : [];
      if (!evidence.length) continue; // never file an observation with no real evidence behind it

      const scope = validScope(raw?.scope, outcomes) ? raw.scope : 'general';
      const confidenceRaw = Number(raw?.confidence);
      const confidence = Number.isFinite(confidenceRaw) ? Math.max(0, Math.min(1, confidenceRaw)) : null;

      store.createLesson({ text, scope, evidence, confidence, sourceTier: 1 });
      lessonsCreated++;
    }
  }

  // A REAL bug, found live during Phase 7's own regression pass: this used
  // to mark every outcome reviewed unconditionally, even when result.ok
  // was false (no model was available at all, or none produced parseable
  // JSON — askModel()'s own contract per ai.js). Since this user's models
  // are routinely ALL rate-limited at once (a documented, expected state
  // for this project, not an edge case), that would have permanently
  // discarded real learning material on nothing more than "quota was tight
  // this exact tick" — the batch would never be looked at again, since
  // pruneReviewedOutcomes() would eventually delete it too. Only a genuine
  // attempt (the model actually replied with something parseable, even an
  // empty lessons array) earns marking these reviewed; a failed attempt
  // leaves them exactly as they were so the NEXT tick, once a model is
  // healthy again, gets a real chance at them. The daily budget unit is
  // still spent either way — a failed call still occupies this tick's one
  // shot, which is itself the intended brake against hammering retries
  // back to back while every model is down.
  if (result.ok) {
    store.markOutcomesReviewed(outcomes.map((o) => o.id));
    store.pruneReviewedOutcomes();
  }
  return { ranAt: new Date().toISOString(), lessonsCreated, reviewed: result.ok };
}

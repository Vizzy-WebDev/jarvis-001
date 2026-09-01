// Tiers 2-4 — official documentation, technical communities, general web.
// Reuses server/research.js's own research()/toSearchQuery() rather than
// reimplementing a search path; that module's free-web-first, model-search-
// second ordering is already the quota-correct design (see its own header
// comment). This module's only job is deciding WHAT to look up and what to
// do with the answer.
//
// Topics are NEVER aimless — always derived from a real, already-noticed,
// well-evidenced lesson (never a keyword the model invents fresh), and
// never applied automatically regardless of trust level: every lesson this
// module creates carries sourceTier 2-4, which improvement-policy.js's
// decide() (via synthesize.js's own worst-tier computation) always routes
// to require-approval. This file only ever adds a LESSON to the same pool
// reflect.js feeds — synthesize.js is still the one and only place a lesson
// becomes a proposal, so there is exactly one pattern-detection pipeline,
// not two.
//
// Shares the WEEKLY budget with life-patterns.js (improvement-store.js's
// WEEKLY_BUDGET) — one combined "how much does Jarvis spend looking outside
// itself" cap. Not a leaf module (imports research.js, which imports ai.js)
// — safe to import from cycle.js only.

import { research } from '../research.js';
import * as store from './improvement-store.js';
import { getPrefs } from '../prefs.js';

const MIN_HOURS_BETWEEN_RESEARCH = 24 * 7; // never more than once a week, full stop
const MIN_EVIDENCE_TO_RESEARCH = 2; // only a lesson that already looks like a real pattern is worth spending outside research on

// A crude, deliberately conservative "this looks like real documentation"
// check on the FIRST corroborating source's own URL — used only to decide
// tier 2 vs tier 3 framing for the user, never to change what's allowed to
// auto-apply (nothing from this module ever does, regardless of tier).
const DOC_HOST_HINTS = /\b(docs\.|developer\.|readthedocs\.io|\/docs\/|\/documentation\/)/i;

function hoursSince(iso) {
  if (!iso) return Infinity;
  const parsed = Date.parse(iso);
  return Number.isFinite(parsed) ? (Date.now() - parsed) / 3600000 : Infinity;
}

export function shouldResearch() {
  const prefs = getPrefs();
  if (!prefs.improvementEnabled || prefs.improvementResearch === 'off') return false;
  if (hoursSince(store.getLastRunAt('research')) < MIN_HOURS_BETWEEN_RESEARCH) return false;
  return pickTopic() != null;
}

/** The single best not-yet-researched, well-evidenced active lesson — or null if nothing qualifies. Highest evidence count first, so the most-confirmed pattern gets looked into before a thinner one. */
function pickTopic() {
  const candidates = store
    .listLessons({ status: 'active' })
    .filter((l) => (l.evidence || []).length >= MIN_EVIDENCE_TO_RESEARCH && !store.wasLessonResearched(l.id));
  if (!candidates.length) return null;
  candidates.sort((a, b) => (b.evidence || []).length - (a.evidence || []).length);
  return candidates[0];
}

/**
 * The actual work — spends ONE weekly-budget unit (which can itself cost up
 * to two askModel calls, per research.js's own free-then-model-search
 * fallback) if there's a real topic AND the weekly budget has room. Returns
 * `{ranAt, lessonCreated}` or null if it skipped.
 */
export async function maybeResearch() {
  if (!shouldResearch()) return null;
  const topic = pickTopic();
  if (!topic) return null;
  if (!store.tryConsumeWeeklyBudget()) return null;
  store.setLastRunAt('research');
  store.markLessonResearched(topic.id); // marked regardless of outcome — a lookup that came back thin still counts as "already tried"

  const result = await research(topic.text);
  if (!result.ok) return { ranAt: new Date().toISOString(), lessonCreated: false };

  const sources = Array.isArray(result.sources) ? result.sources : [];
  // The corroboration floor — an answer with only one source (or the
  // model's own uncorroborated recall via 'model-search' with a thin
  // source list) never becomes a lesson at all. This is what "tier 4
  // requires a second corroborating source" actually means in code, not
  // just in the design doc.
  if (sources.length < 2) return { ranAt: new Date().toISOString(), lessonCreated: false, corroborated: false };

  const firstUrl = sources[0]?.url || '';
  const sourceTier = result.via === 'model-search' ? 3 : DOC_HOST_HINTS.test(firstUrl) ? 2 : 4;

  store.createLesson({
    text: `Regarding "${topic.text}": ${String(result.answer || '').slice(0, 400)}`,
    scope: topic.scope,
    evidence: topic.evidence, // the same underlying outcomes that made this worth researching in the first place
    confidence: null, // outside information — no first-hand confidence score to give it
    sourceTier,
    sourceUrl: firstUrl || null,
  });

  return { ranAt: new Date().toISOString(), lessonCreated: true, sourceTier };
}

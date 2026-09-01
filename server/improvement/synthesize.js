// Synthesis — looks ACROSS the whole active lesson set (not one batch of
// outcomes, the way reflect.js does) for something that genuinely
// RECURRED, and turns that into a proposal. This is the one place
// "detect a pattern, don't just patch a one-off mistake" actually happens:
// a proposal is only ever created here when at least two INDEPENDENT
// outcomes support it — a single lesson, however clear, is never enough on
// its own (see MIN_SUPPORTING_OUTCOMES below). Also the one place a
// contradiction between two already-active rules, or between a fresh
// pattern and an existing rule, gets flagged — never resolved silently.
//
// Rarer and more expensive to run than reflect.js (see shouldSynthesize()),
// and shares the SAME daily budget bucket with it (improvement-store.js's
// DAILY_BUDGET) — the two are one combined "how much does Jarvis spend
// reviewing itself today" cap, not two separate ones.
//
// Not a leaf module (imports ai.js, apply.js, notifications.js, events.js)
// — safe to import from cycle.js only.

import { askModel } from '../ai.js';
import * as store from './improvement-store.js';
import { decide } from './improvement-policy.js';
import { applyProposal } from './apply.js';
import { getPrefs } from '../prefs.js';
import { addNotification } from '../notifications.js';
import { broadcast } from '../events.js';

const MIN_NEW_LESSONS = 3;
const MIN_HOURS_BETWEEN_SYNTHESES = 24;
// The code-level floor beneath the model's own judgment — a 'rule'
// proposal is only ever CREATED when at least this many DISTINCT outcomes
// (not lessons — the same outcome cited by two lessons only counts once)
// support it. improvement-policy.js's decide() applies its OWN,
// trust-level-dependent evidence floor on top of this at apply time; this
// is the floor beneath even asking.
const MIN_SUPPORTING_OUTCOMES = 2;

function hoursSince(iso) {
  if (!iso) return Infinity;
  const parsed = Date.parse(iso);
  return Number.isFinite(parsed) ? (Date.now() - parsed) / 3600000 : Infinity;
}

export function shouldSynthesize() {
  if (!getPrefs().improvementEnabled) return false;
  const hrs = hoursSince(store.getLastRunAt('synthesize'));
  if (hrs < MIN_HOURS_BETWEEN_SYNTHESES) return false;
  const lastRunIso = store.getLastRunAt('synthesize');
  const newLessons = store.listLessons({ status: 'active' }).filter((l) => !lastRunIso || l.createdAt > lastRunIso);
  return newLessons.length >= MIN_NEW_LESSONS;
}

function normalizeText(s) {
  return String(s || '')
    .trim()
    .toLowerCase()
    .replace(/[^\w\s]/g, '')
    .replace(/\s+/g, ' ');
}

function isDuplicateOfExisting(text, rules, pendingProposals) {
  const normalized = normalizeText(text);
  const matches = (other) => {
    const o = normalizeText(other);
    return Boolean(normalized && o && (normalized.includes(o) || o.includes(normalized)));
  };
  return rules.some((r) => matches(r.text)) || pendingProposals.some((p) => matches(p.payload?.text || p.title));
}

/** Distinct outcome ids underlying a set of lessons (union of each lesson's own evidence array) — the true "how many independent real events support this" count. */
function supportingOutcomeIds(lessons) {
  const ids = new Set();
  for (const l of lessons) {
    for (const id of l.evidence || []) ids.add(id);
  }
  return [...ids];
}

/**
 * The actual work — spends ONE model call if shouldSynthesize() says it's
 * worth it AND the (shared, with reflect.js) daily budget has room.
 * Returns `{ranAt, proposalsCreated, autoApplied}` or null if it skipped.
 */
export async function maybeSynthesize() {
  if (!shouldSynthesize()) return null;
  if (!store.tryConsumeDailyBudget()) return null;
  store.setLastRunAt('synthesize');

  const lessons = store.listLessons({ status: 'active' });
  const rules = store.listRules({ activeOnly: true });
  const pendingProposals = store.listProposals({ status: 'pending' });
  if (!lessons.length) return { ranAt: new Date().toISOString(), proposalsCreated: 0, autoApplied: 0 };

  const lessonsBlock = lessons.map((l) => `- [${l.id}] (${l.scope}) ${l.text} — evidence: ${(l.evidence || []).length} outcome(s)`).join('\n');
  const rulesBlock = rules.length ? rules.map((r) => `- [${r.id}] (${r.scope}) ${r.text}`).join('\n') : '(none yet)';

  const prompt = [
    "Below are the observations Jarvis has noted about its own work (\"lessons\") and the behaviour rules already live in its own system prompt.",
    'Two jobs:',
    '1. Find a genuine PATTERN — two or more lessons pointing at the SAME real recurring issue (not just a similar topic) — and propose turning it into ONE clear, concrete behaviour rule Jarvis should follow going forward.',
    '2. Flag any CONTRADICTION you notice: two of the live rules that disagree with each other, or a lesson that clearly contradicts a live rule. Never silently prefer one — flag it for the owner to decide.',
    '',
    'Lessons:',
    lessonsBlock,
    '',
    'Live rules:',
    rulesBlock,
    '',
    'Reply with JSON: {"proposals": [',
    '  {"kind": "rule", "title": "...", "rationale": "why, in one sentence", "text": "the exact rule text, phrased as a direct instruction", "scope": "...", "supportingLessonIds": ["..."], "confidence": 0.0-1.0},',
    '  {"kind": "conflict", "title": "...", "rationale": "explain BOTH sides of the contradiction clearly", "conflictsWithRuleId": "rule_id", "supportingLessonIds": ["..."]}',
    ']}',
    '"scope": "general", or "job_kind:<kind>"/"task:<id>" ONLY copied exactly from a lesson\'s own scope above — never invented.',
    '"supportingLessonIds": the bracketed lesson id(s) above that actually support this — a "rule" proposal needs at least TWO lessons backed by DIFFERENT outcomes to count as a real pattern; a single lesson is not enough on its own, however clear it seems.',
    '"conflictsWithRuleId": for a "conflict" proposal, the bracketed rule id it contradicts (if two RULES contradict each other, pick either one and explain the other by id in the rationale).',
    'An empty proposals array is completely normal if nothing genuinely recurs yet or nothing conflicts. Never propose a rule from a single occurrence, and never invent a lesson or rule id not shown above.',
  ].join('\n');

  const result = await askModel({
    prompt,
    system:
      "You look across Jarvis's own noted observations for genuine recurring patterns worth turning into a real behaviour rule, and for real contradictions worth flagging — never from a single occurrence, never inventing what the data does not show.",
    json: true,
    background: true,
  });

  let proposalsCreated = 0;
  let autoApplied = 0;
  const batchId = `batch${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`;
  const autoAppliedTitles = [];
  const askTitles = [];

  if (result.ok && result.data && Array.isArray(result.data.proposals)) {
    const lessonById = new Map(lessons.map((l) => [l.id, l]));
    for (const raw of result.data.proposals) {
      const kind = raw?.kind === 'conflict' ? 'conflict' : raw?.kind === 'rule' ? 'rule' : null;
      if (!kind) continue;

      const supportingIds = Array.isArray(raw?.supportingLessonIds) ? raw.supportingLessonIds.filter((id) => lessonById.has(id)) : [];
      const supportingLessons = supportingIds.map((id) => lessonById.get(id));

      if (kind === 'rule') {
        const text = String(raw?.text || '').trim();
        if (!text) continue;
        if (isDuplicateOfExisting(text, rules, pendingProposals)) continue;
        const evidence = supportingOutcomeIds(supportingLessons);
        // The real floor: fewer than two lessons, or fewer than two
        // DISTINCT underlying outcomes, means this isn't a recurring
        // pattern yet — never created as a proposal at all, regardless of
        // how confidently the model phrased it.
        if (supportingLessons.length < 2 || evidence.length < MIN_SUPPORTING_OUTCOMES) continue;

        const scopeCandidate = raw?.scope || 'general';
        const scope = scopeCandidate === 'general' || supportingLessons.some((l) => l.scope === scopeCandidate) ? scopeCandidate : 'general';
        const confidenceRaw = Number(raw?.confidence);
        const confidence = Number.isFinite(confidenceRaw) ? Math.max(0, Math.min(1, confidenceRaw)) : null;

        // The WORST (highest-numbered, least-trusted) tier among the
        // lessons actually backing this pattern — never hardcoded to 1.
        // reflect.js-derived lessons are always tier 1, but improve-
        // research.js's lessons carry tier 2-4; a pattern that leans on
        // even ONE outside-sourced lesson must never look tier-1 to
        // improvement-policy.js's decide() (which is the one thing that
        // gates auto-apply), or an outside idea could slip into "own task
        // history" and auto-apply against the user's explicit "outside
        // ideas always ask" decision.
        const sourceTier = supportingLessons.reduce((worst, l) => Math.max(worst, Number(l.sourceTier) || 1), 1);

        const proposal = store.createProposal({
          kind: 'rule',
          title: String(raw?.title || text.slice(0, 80)),
          rationale: raw?.rationale || null,
          payload: { text, scope },
          evidence,
          sourceTier,
          batchId,
        });
        proposalsCreated++;

        const policyResult = decide({ kind: 'rule', sourceTier, evidence, conflictWith: null }, {});
        if (policyResult === 'auto-apply') {
          applyProposal(proposal.id);
          autoApplied++;
          autoAppliedTitles.push(proposal.title);
        } else {
          askTitles.push(proposal.title);
        }
        void confidence; // kept on the lesson/proposal record itself, not otherwise branched on here
      } else {
        // kind === 'conflict' — ALWAYS requires approval (improvement-policy.js's
        // decide() refuses any proposal carrying conflictWith, unconditionally),
        // so this never touches applyProposal() at all.
        const targetRuleId = rules.some((r) => r.id === raw?.conflictsWithRuleId) ? raw.conflictsWithRuleId : null;
        if (!targetRuleId) continue;
        const proposal = store.createProposal({
          kind: 'conflict',
          title: String(raw?.title || 'A contradiction was found'),
          rationale: raw?.rationale || null,
          evidence: supportingOutcomeIds(supportingLessons),
          sourceTier: 1,
          batchId,
          conflictWith: targetRuleId,
        });
        proposalsCreated++;
        askTitles.push(proposal.title);
      }
    }
  }

  if (autoAppliedTitles.length) {
    addNotification({
      kind: 'improvement',
      level: 'info',
      title: autoAppliedTitles.length === 1 ? 'Jarvis adjusted how it works' : `Jarvis adjusted ${autoAppliedTitles.length} things about how it works`,
      body: autoAppliedTitles.join(' · '),
      action: { label: 'Review Self-Improvement', section: 'improvement' },
    });
    broadcast({ type: 'improvement_applied', count: autoAppliedTitles.length });
  }
  if (askTitles.length) {
    addNotification({
      kind: 'improvement',
      level: 'info',
      title: askTitles.length === 1 ? 'A self-improvement suggestion is waiting for you' : `${askTitles.length} self-improvement suggestions are waiting for you`,
      body: askTitles.join(' · '),
      action: { label: 'Review Self-Improvement', section: 'improvement' },
    });
    broadcast({ type: 'improvement_proposals_ready', count: askTitles.length, batchId });
  }

  return { ranAt: new Date().toISOString(), proposalsCreated, autoApplied };
}

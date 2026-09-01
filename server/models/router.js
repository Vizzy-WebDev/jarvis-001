// Picks which model should handle a turn when auto-select is on. Profiles
// the task, hard-filters to models that can actually do it, then ranks the
// survivors by the balance dial (fast / balanced / quality). Manual
// override and mid-turn failover both skip straight past this ranking and
// go to server/models/runner.js's candidate list directly.

import { listModels, isReady } from './registry.js';
import { isHealthy } from './health.js';
import { classifyTaskType, scoringLeanForType } from './task-types.js';

// Mirrors health.js's COOLDOWNS_MS tiers, but keyed on the STATE persisted to
// disk (registry.js's `availability.state`, written by runner.js) rather
// than the transient error `kind` health.js tracks in memory. The two exist
// for different lifetimes: health.js's breaker is what skips a model within
// the current process and resets on every restart; this is what stops a
// FRESH restart from re-discovering the same 20 dead models the slow way —
// by failing against each one for a full round-trip — when yesterday's
// failure is still sitting right there on disk. 'unreachable' covers both
// health.js's 'network' and 'other' kinds (that distinction isn't preserved
// once collapsed into availability.state — see error-kind.js's
// AVAILABILITY_STATE_FOR_KIND) — the longer of the two cooldowns is used
// here, since a stale 'unreachable' model is usually stale by hours or days
// anyway, not seconds, so which exact value is picked rarely matters in
// practice.
const AVAILABILITY_COOLDOWNS_MS = {
  quota: 30 * 60 * 1000,
  auth: 6 * 60 * 60 * 1000,
  no_access: 6 * 60 * 60 * 1000,
  // Was 5 minutes — far shorter than health.js's in-memory breaker even
  // gives the SAME kind (network: 1min, other: 5min) despite this being the
  // cooldown for a state that's already been persisted across a restart, so
  // it's necessarily at least that stale already. Confirmed live: with 47
  // of 62 enabled models sitting 'unreachable' (wrong model name, retired,
  // no access on this key — not transient), every `checkedAt` on disk was
  // DAYS old, so this 5-minute window was never actually protecting
  // anything — it let every one of those 47 models back into the ranked
  // list on every single turn, each costing a full round-trip before the
  // runner could cross it off (see runner.js's MAX_FALLBACK_ATTEMPTS,
  // which bounds how many of them one turn will pay for, but does nothing
  // for how often they're offered in the first place). 6 hours matches the
  // auth/no_access tiers — a genuinely-fixed model (key added, name
  // corrected) is still reachable via Model Settings' own "Check all" in
  // the meantime, which re-probes directly rather than waiting on this.
  unreachable: 6 * 60 * 60 * 1000,
};

/** False only for a model whose PERSISTED availability is a known-bad state and still inside its cooldown. A model never checked, or last seen 'working', always passes — this only ever narrows the field, never requires a state to be present. */
function passesAvailabilityCooldown(entry) {
  const av = entry.availability;
  if (!av || av.state === 'working') return true;
  const cooldown = AVAILABILITY_COOLDOWNS_MS[av.state];
  if (!cooldown) return true; // unknown state — don't invent a filter for it
  const checkedAt = av.checkedAt ? Date.parse(av.checkedAt) : 0;
  if (!Number.isFinite(checkedAt)) return true; // can't tell how stale — don't block on unparseable data
  return Date.now() - checkedAt >= cooldown;
}

// A loose signal for "this needs real thinking", not a precise classifier —
// good enough to nudge the balance, not meant to gate anything on its own.
const REASONING_HINTS =
  /\b(write|essay|code|debug|analy[sz]e|plan|compare|explain in depth|think through|design|refactor|summar(y|ize)|research|draft)\b/i;

export function profileTask({ text = '', source = 'text', background = false, estimatedTokens = 0, type } = {}) {
  const trimmed = String(text || '');
  const wordCount = trimmed.trim().split(/\s+/).filter(Boolean).length;
  const looksLikeReasoning = REASONING_HINTS.test(trimmed) || wordCount > 60;
  // `type` (task domain: coding/research/vision/simple-question/general) is
  // a second, independent axis from `profile` below (which orchestration
  // loop is asking) — caller-declared if given, else inferred from `text`
  // the same loose way `complexity` always has been. Its scoring lean is
  // folded additively into `complexity`: either signal alone is enough to
  // weight scoring toward quality, neither replaces the other.
  const resolvedType = type || classifyTaskType(trimmed);

  return {
    source, // 'voice' | 'text'
    background: Boolean(background),
    type: resolvedType,
    complexity: (looksLikeReasoning || scoringLeanForType(resolvedType) === 'reasoning') ? 'reasoning' : 'quick',
    estimatedTokens: estimatedTokens || Math.ceil(trimmed.length / 3),
    needsTools: true, // any turn might call a skill; only tool-capable models qualify
    profile: 'chat',
  };
}

/**
 * Profile for a computer-control session (control/session.js) — needs tools
 * (it decides actions via tool calls, same as chat) and, unlike an ordinary
 * turn, benefits from a stronger model since a wrong click is more costly
 * than a slightly-off sentence. Vision is a *preference*, not a hard
 * requirement — most apps expose a good enough UI Automation tree that a
 * text-only model does fine; screenshots are only the fallback path, and
 * session.js checks a chosen model's own caps.vision before ever sending one
 * rather than assuming every candidate here can see images.
 */
export function controlTaskProfile() {
  return { source: 'text', background: false, complexity: 'reasoning', estimatedTokens: 0, needsTools: true, profile: 'control' };
}

function hardFilter(entries, task) {
  return entries.filter((e) => {
    if (!e.enabled) return false;
    if (!isReady(e)) return false;
    if (!isHealthy(e.id)) return false;
    if (!passesAvailabilityCooldown(e)) return false;
    if (task.needsTools && !e.caps?.tools) return false;
    if (task.estimatedTokens && e.caps?.contextTokens && task.estimatedTokens > e.caps.contextTokens) return false;
    return true;
  });
}

function scoreFor(entry, task, balance) {
  const speed = entry.tier?.speed ?? 3;
  const quality = entry.tier?.quality ?? 3;
  const cost = entry.tier?.cost ?? 2;

  if (task.profile === 'control') {
    // Quality-weighted regardless of the balance dial — a wrong click costs
    // more than a slightly-off sentence, and this isn't a latency-sensitive
    // voice turn. A vision-capable model gets a flat bonus (a genuine
    // preference, not a requirement — see controlTaskProfile()'s comment).
    const visionBonus = entry.caps?.vision ? 3 : 0;
    return quality * 3 - cost * 0.5 + visionBonus;
  }

  // Background jobs (e.g. the morning briefing) care about cost, not
  // latency — nobody's waiting on them in real time.
  if (task.background) return quality * 2 - cost * 2;

  if (balance === 'fast') return speed * 3 - cost;
  if (balance === 'quality') return quality * 3 - cost * 0.5;

  // Balanced (the default): fast/cheap for quick voice/chat turns, reward
  // quality once the task looks like real reasoning, writing, or code.
  if (task.complexity === 'reasoning') return quality * 2.5 - cost * 0.5;
  return speed * 2 - cost;
}

// scoreFor() above is purely tier/speed/cost math — it has NO awareness of
// whether a model is actually known to work. hardFilter() only excludes a
// known-bad model while it's still within its cooldown; once that cooldown
// lapses (making the model eligible again), it used to compete on tier
// score alone, so a fast/cheap/high-tier model that was actually DEAD could
// — and, confirmed live, did — outrank the only 1-2 models actually marked
// 'working', filling every one of runner.js's limited fallback attempts
// with models known to fail ("switches through 2-3 models and dies").
// Sized to exceed scoreFor's largest possible spread across every
// task.profile branch (~18, worked out from each branch's own min/max) so
// this ordering is guaranteed, not just likely, regardless of tier:
// working > never-checked > known-bad-but-retry-eligible.
const AVAILABILITY_SCORE_BONUS = 20;

function availabilityScore(entry) {
  const state = entry.availability?.state;
  if (state === 'working') return AVAILABILITY_SCORE_BONUS;
  if (state && AVAILABILITY_COOLDOWNS_MS[state]) return -AVAILABILITY_SCORE_BONUS;
  return 0; // never checked — no signal either way, unchanged from before
}

/**
 * Ranks enabled, healthy, capable models best-first for this task. Empty
 * array means nothing qualifies.
 *
 * `scoreFor` alone leaves wide ties — e.g. every `speed:5, cost:1` model
 * scores identically regardless of quality, and Array.sort is stable, so the
 * winner used to be whichever entry happened to appear first in
 * data/models.json (found live: a free code-completion model was winning
 * every voice turn purely on file order, ahead of two dozen tied Gemini
 * chat models). Ties now break on quality (higher wins), then id
 * (alphabetical) as the final, fully deterministic tiebreaker — never file
 * order.
 */
export function rankCandidates(task, { balance = 'balanced' } = {}) {
  const entries = hardFilter(listModels(), task);
  return entries.sort((a, b) => {
    const scoreDiff =
      (scoreFor(b, task, balance) + availabilityScore(b)) - (scoreFor(a, task, balance) + availabilityScore(a));
    if (scoreDiff !== 0) return scoreDiff;
    const qualityDiff = (b.tier?.quality ?? 3) - (a.tier?.quality ?? 3);
    if (qualityDiff !== 0) return qualityDiff;
    return a.id < b.id ? -1 : a.id > b.id ? 1 : 0;
  });
}

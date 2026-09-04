// Picks which model should handle a turn when auto-select is on. Profiles
// the task, hard-filters to models that can actually do it, then ranks the
// survivors by the balance dial (fast / balanced / quality). Manual
// override and mid-turn failover both skip straight past this ranking and
// go to server/models/runner.js's candidate list directly.

import { listModels, isReady } from './registry.js';
import { isHealthy, getHealthStatus } from './health.js';
import { classifyTaskType, scoringLeanForType } from './task-types.js';
import { observedCostTier } from '../cost/advisor.js';

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
  // A provider-side overload (503/"high demand") — confirmed live to clear
  // within seconds, not hours. See error-kind.js's 'transient' kind.
  busy: 2 * 60 * 1000,
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
  // for how often they're offered in the first place). Shortened again from
  // 6 hours to 10 minutes once 'unreachable' stopped being the dumping
  // ground for every unclassified failure (see error-kind.js's 'other' ->
  // 'error' split) — what's left in THIS bucket is a real network-layer
  // failure (our own connection, DNS, a refused local server), which is
  // usually a "this minute" problem, not a "this week" one. A
  // genuinely-fixed model is still reachable via Model Settings' own
  // "Check all" in the meantime, which re-probes directly rather than
  // waiting on this.
  unreachable: 10 * 60 * 1000,
  // An unclassified failure ('other' in error-kind.js) — not confidently
  // known to be transient, permanent, or auth-related. Deliberately NOT the
  // harshest tier just because nothing else matched it; a genuine unknown
  // deserves a moderate cooldown, not the same 6-hour ban auth/no_access get
  // for a KNOWN-permanent problem.
  error: 20 * 60 * 1000,
  // Deliberately absent from this map: 'unsupported' has no cooldown at
  // all — see passesAvailabilityCooldown()'s own check below, which excludes
  // it outright rather than looking it up here. A model that can never
  // serve chat doesn't get "better" after any amount of waiting.
};

/** False only for a model whose PERSISTED availability is a known-bad state and still inside its cooldown, OR a model marked structurally unable to serve this kind of request at all (no cooldown ever clears that one — see error-kind.js's 'unsupported' kind). A model never checked, or last seen 'working', always passes — this only ever narrows the field, never requires a state to be present. */
function passesAvailabilityCooldown(entry) {
  const av = entry.availability;
  if (!av || av.state === 'working') return true;
  if (av.state === 'unsupported') return false;
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

// The one place a model gets excluded from routing, and WHY — hardFilter()
// below and explainExclusions() (used to build a real "here's why nothing
// can answer" message — see runner.js) both call this so the two can never
// disagree about what's excluded. Returns null when the model passes every
// check; otherwise a short reason key. Where the reason is a persisted
// availability state (quota/auth/no_access/busy/unreachable/error/
// unsupported), the key IS that state's own name, so a caller can turn it
// into a human sentence with the same vocabulary the Model Settings badges
// already use — never a second, drifting copy of that wording.
function excludeReason(entry, task) {
  if (!entry.enabled) return 'disabled';
  if (!isReady(entry)) return 'needs_key';
  // The in-memory breaker (health.js) is a SEPARATE signal from the
  // persisted availability state below — it can fire on its own short
  // cooldown even when availability.state still says 'working' (e.g. right
  // after a timeout — see runner.js's Part B). Named distinctly so a
  // "why can't you answer" message doesn't conflate the two.
  if (!isHealthy(entry.id)) return 'recent_failure';
  if (!passesAvailabilityCooldown(entry)) return entry.availability?.state || 'cooldown';
  if (task.needsTools && !entry.caps?.tools) return 'no_tools';
  if (task.estimatedTokens && entry.caps?.contextTokens && task.estimatedTokens > entry.caps.contextTokens) return 'context_too_small';
  return null;
}

function hardFilter(entries, task) {
  return entries.filter((e) => excludeReason(e, task) === null);
}

/**
 * For a real "here's why nothing can answer" message (runner.js's empty-
 * candidate and exhausted-fallback 'paused' events) instead of one generic
 * sentence naming none of the actual reasons. Walks every model (not just
 * enabled ones) through the exact same excludeReason() hardFilter() uses,
 * so the counts can never drift from what actually got filtered. `soonestRetryMs`
 * is the smallest known wait across every excluded model that has one (a
 * cooldown or an in-memory breaker) — null if nothing excluded has a known
 * expiry (e.g. everything is disabled, needs a key, or 'unsupported').
 */
export function explainExclusions(task) {
  const all = listModels();
  const counts = {};
  let soonestRetryMs = null;
  const considerRetry = (ms) => {
    if (!Number.isFinite(ms) || ms <= 0) return;
    if (soonestRetryMs === null || ms < soonestRetryMs) soonestRetryMs = ms;
  };
  const health = getHealthStatus(); // modelId -> { retryInMs, ... }

  for (const entry of all) {
    const reason = excludeReason(entry, task);
    if (!reason) continue;
    counts[reason] = (counts[reason] || 0) + 1;
    if (reason === 'recent_failure') {
      considerRetry(health[entry.id]?.retryInMs);
      continue;
    }
    const cooldown = AVAILABILITY_COOLDOWNS_MS[reason];
    const checkedAt = entry.availability?.checkedAt ? Date.parse(entry.availability.checkedAt) : NaN;
    if (cooldown && Number.isFinite(checkedAt)) considerRetry(checkedAt + cooldown - Date.now());
  }

  return { total: all.length, counts, soonestRetryMs };
}

function scoreFor(entry, task, balance) {
  const speed = entry.tier?.speed ?? 3;
  const quality = entry.tier?.quality ?? 3;
  // A real, measured price (cost/advisor.js's observedCostTier()) replaces
  // the catalog's own name-regex guess when one is on record — same 0-4
  // domain the guess already used, so every branch below is unaffected in
  // shape; only which integer lands in `cost` can change. Falls back to the
  // guess exactly as before when no real price is known yet. See root
  // CLAUDE.md's Operational Awareness item 6.
  const cost = observedCostTier(entry.provider || entry.adapter, entry.model) ?? entry.tier?.cost ?? 2;

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

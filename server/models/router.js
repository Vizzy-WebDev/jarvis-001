// Picks which model should handle a turn when auto-select is on. Profiles
// the task, hard-filters to models that can actually do it, then ranks the
// survivors by the balance dial (fast / balanced / quality). Manual
// override and mid-turn failover both skip straight past this ranking and
// go to server/models/runner.js's candidate list directly.

import { listModels, isReady } from './registry.js';
import { isHealthy } from './health.js';
import { classifyTaskType, scoringLeanForType } from './task-types.js';

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

/** Ranks enabled, healthy, capable models best-first for this task. Empty array means nothing qualifies. */
export function rankCandidates(task, { balance = 'balanced' } = {}) {
  const entries = hardFilter(listModels(), task);
  return entries.sort((a, b) => scoreFor(b, task, balance) - scoreFor(a, task, balance));
}

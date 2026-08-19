// A small, dependency-free vocabulary for "what kind of task is this" —
// coding / research / vision / simple-question / general. Deliberately
// distinct from router.js's `profile` field ('chat' | 'control'), which is
// about which orchestration loop is asking (an ordinary chat turn vs. a
// computer-control session), not what domain the task itself is in — a
// control session can just as easily be "coding" or "vision" shaped. Kept as
// two independent axes on purpose (see CLAUDE.md-style rationale in
// router.js's profileTask()): folding one into the other would make
// controlTaskProfile()'s always-quality-weighted scoring conditional on task
// type instead of a flat rule, which isn't what was asked for here.
//
// Zero imports on purpose — pure data + pure functions, same posture as
// router.js's own "no I/O" purity. That makes this file automatically safe
// for server/skills/* to depend on (transitively, via ai.js) with no risk to
// the circular-import invariant (see CLAUDE.md): there is nothing here that
// could ever lead back to skills/index.js, models/runner.js, or any of the
// other forbidden targets, because it doesn't import anything at all.
//
// This does NOT replace router.js's REASONING_HINTS regex or ai.js's
// need{video,audio,vision,webSearch} bag — it relates them. A caller can
// declare `type` once; classifyTaskType() infers it from text the same loose
// way complexity always has been when a caller doesn't. needForType() seeds
// ai.js's capability gate and scoringLeanForType() seeds router.js's scoring
// (folded additively into the existing `complexity` field, not replacing
// it), without either file needing to know about the other's mechanism.

export const TASK_TYPES = {
  coding: {
    need: null,
    scoringLean: 'reasoning',
  },
  research: {
    need: { webSearch: true },
    scoringLean: 'reasoning',
  },
  vision: {
    need: { vision: true },
    scoringLean: 'reasoning',
  },
  'simple-question': {
    need: null,
    scoringLean: 'quick',
  },
  general: {
    need: null,
    scoringLean: 'quick',
  },
};

// Loose keyword hints, checked in this order — first match wins. Same spirit
// as router.js's REASONING_HINTS: good enough to nudge a default, not a
// precise classifier. Coding/research/vision are checked before the generic
// reasoning hint so e.g. "debug this" lands on the more specific 'coding'
// default (no forced need{}) rather than the generic 'general' bucket.
const CODING_HINTS = /\b(code|coding|debug|refactor|function|bug|script|program(ming)?|compile|syntax|stack trace)\b/i;
const RESEARCH_HINTS = /\b(research|look up|find out|search for|what'?s the latest|news about|current events|who is|when did)\b/i;
const VISION_HINTS = /\b(this image|this photo|this picture|this screenshot|what'?s in this|describe this|look at this)\b/i;
const REASONING_HINTS =
  /\b(write|essay|analy[sz]e|plan|compare|explain in depth|think through|design|summar(y|ize)|draft)\b/i;

/** Guesses a task type from raw text — a loose signal, not a precise classifier. */
export function classifyTaskType(text) {
  const trimmed = String(text || '');
  if (CODING_HINTS.test(trimmed)) return 'coding';
  if (VISION_HINTS.test(trimmed)) return 'vision';
  if (RESEARCH_HINTS.test(trimmed)) return 'research';
  const wordCount = trimmed.trim().split(/\s+/).filter(Boolean).length;
  if (REASONING_HINTS.test(trimmed) || wordCount > 60) return 'general';
  return 'simple-question';
}

/** The task type's default capability `need` bag, or null if it has none. */
export function needForType(type) {
  return TASK_TYPES[type]?.need ?? null;
}

/** 'reasoning' | 'quick' — how this type should lean router.js's scoring. */
export function scoringLeanForType(type) {
  return TASK_TYPES[type]?.scoringLean ?? 'quick';
}

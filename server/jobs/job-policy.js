// Every steady-state decision the Orchestrator makes about a background Job
// — crash-recovery classification, live-stall diagnosis, capacity,
// resource contention, retry-vs-escalate — as PURE functions. No imports at
// all, deliberately: same discipline as memory/memory-policy.js's decide(),
// which is a pure function specifically so it can be exercised with a
// plain `node -e` truth table, no server or database needed. These run on
// every supervisor tick for every active job, so "zero model calls, zero
// side effects" is the actual cost budget this file exists to hold to —
// see root CLAUDE.md's Jobs section.
//
// What's deliberately NOT here: admission planning (decomposing a goal,
// picking a worker kind, judging split proportionality) and the
// effect-based "must the owner decide" classifier (which needs real tool
// metadata, arriving with the tools themselves in Phase 3). Both are rare,
// boundary-only decisions — this file is the continuous, free part.

// ---------------------------------------------------------------------------
// Crash-recovery classification
// ---------------------------------------------------------------------------

/**
 * Derives an HONEST resumability verdict for an orphaned job (found
 * `status === 'running'` at startup — no process is running it, so no
 * heuristic is needed to know it crashed) from its write-ahead trace, never
 * from the job declaring one about itself. The default is the pessimistic
 * reading at every branch:
 *
 * - Already parked on a decision when it died -> still needs that decision.
 * - ANY trace row (intent or outcome) marked `effect: 'external'` -> treat
 *   as unrecoverable, whether that action actually completed or only got as
 *   far as being logged as about-to-happen. A crash between logging the
 *   intent and running the action is indistinguishable from one between
 *   running it and logging the outcome — in both cases retrying risks doing
 *   it twice, so the write-ahead intent row alone is enough to condemn it.
 * - Nothing beyond `effect: 'read'` -> safe to just pick back up.
 * - Otherwise (touched its own workspace, nothing external) -> the write
 *   itself might not be trustworthy after an unclean stop, but nothing
 *   irreversible happened outside the job's own scope -> safe to start over.
 */
export function classifyRecovery(job, trace = []) {
  if (job?.status === 'awaiting_decision') return 'needs_input';
  if (trace.some((t) => t.effect === 'external')) return 'unrecoverable';
  if (!trace.some((t) => t.effect === 'workspace')) return 'resumable';
  return 'restartable';
}

// ---------------------------------------------------------------------------
// Live-stall diagnosis — "alive but not going anywhere"
// ---------------------------------------------------------------------------

export const DIAGNOSE_TAIL_SIZE = 8;

/** Deterministic step budget per worker kind — the backstop for a worker that never repeats but never converges either. */
export const STEP_BUDGET_BY_KIND = {
  research: 30,
  files: 20,
  computer: 25, // same figure as control/session.js's own MAX_STEPS
  generic: 30,
};

const NEAR_DUPLICATE_SIMILARITY = 0.85;

function stableStringify(value) {
  if (value === null || typeof value !== 'object') return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(stableStringify).join(',')}]`;
  const keys = Object.keys(value).sort();
  return `{${keys.map((k) => `${JSON.stringify(k)}:${stableStringify(value[k])}`).join(',')}}`;
}

function parseDetail(detail) {
  if (!detail) return null;
  try {
    return JSON.parse(detail);
  } catch {
    return null;
  }
}

/** `tool:stableArgs` for a trace entry whose detail carries `{name, args}` (an 'intent' row); null if not a recognizable tool-call entry. */
function toolSignature(entry) {
  const d = parseDetail(entry?.detail);
  if (!d || !d.name) return null;
  return `${d.name}:${stableStringify(d.args ?? {})}`;
}

/** `{name, ok}` for a trace entry whose detail carries a tool outcome; null if not one. */
function toolOutcome(entry) {
  const d = parseDetail(entry?.detail);
  if (!d || !d.name || !('ok' in d)) return null;
  return d;
}

/** The full text of a 'note' entry (a worker's own non-tool reasoning) — prefers detail (untruncated) over summary. */
function noteText(entry) {
  return entry?.detail || entry?.summary || '';
}

/** Dice coefficient over lowercase character bigrams — cheap, no dependency, good enough to catch a model restating itself near-verbatim. */
function textSimilarity(a, b) {
  const bigrams = (s) => {
    const norm = String(s || '').toLowerCase().replace(/\s+/g, ' ').trim();
    const set = new Map();
    for (let i = 0; i < norm.length - 1; i++) {
      const bg = norm.slice(i, i + 2);
      set.set(bg, (set.get(bg) || 0) + 1);
    }
    return set;
  };
  const setA = bigrams(a);
  const setB = bigrams(b);
  if (!setA.size || !setB.size) return 0;
  let shared = 0;
  for (const [bg, countA] of setA) {
    const countB = setB.get(bg);
    if (countB) shared += Math.min(countA, countB);
  }
  const totalA = [...setA.values()].reduce((s, v) => s + v, 0);
  const totalB = [...setB.values()].reduce((s, v) => s + v, 0);
  return (2 * shared) / (totalA + totalB);
}

/**
 * Checks a matching-period repeat over the LAST `period * cycles` entries of
 * `sigs` (already the newest-last tail) — e.g. period 2, cycles 3 checks the
 * last 6 signatures form A,B,A,B,A,B. Returns false if any repeated
 * signature is null (an unrecognizable entry never counts as a match) or if
 * there aren't enough entries.
 */
function hasRepeatingPeriod(sigs, period, cycles) {
  const window = period * cycles;
  if (sigs.length < window) return false;
  const tail = sigs.slice(-window);
  for (let i = 0; i < window; i++) {
    if (tail[i] == null || tail[i] !== tail[i % period]) return false;
  }
  return true;
}

/**
 * Diagnoses whether a `running` job is stalled DESPITE still emitting
 * events — heartbeat silence alone only catches a hung or dead process; a
 * worker calling invoke() on schedule but making zero real progress needs
 * this instead. Pure function over a trace tail (job-store.js's
 * getTraceTail(jobId, DIAGNOSE_TAIL_SIZE)) — checked in order, first match
 * wins:
 *
 * 1. exact_repeat       — last 3 tool-call signatures identical
 * 2. oscillation        — last 4-6 signatures match a period-2/3 repeat
 * 3. repeated_failure   — last 3 tool outcomes all ok:false
 * 4. near_duplicate_reasoning — the worker's last two non-tool notes read
 *    as near-identical text — the one failure mode with no tool calls at
 *    all, so the first three signals can't see it
 *
 * Returns `{cause, detail}` or `null`. Step-budget exhaustion is a separate,
 * simpler check — see stepBudgetExceeded() below — since it needs a live
 * step counter, not trace inspection.
 */
export function diagnoseStall(tailTrace = []) {
  const toolIntents = tailTrace.filter((t) => t.kind === 'tool' && t.phase === 'intent');
  const toolSigs = toolIntents.map(toolSignature);

  const lastN = (arr, n) => arr.slice(-n);

  const last3Sigs = lastN(toolSigs, 3);
  if (last3Sigs.length === 3 && last3Sigs[0] != null && last3Sigs.every((s) => s === last3Sigs[0])) {
    return { cause: 'exact_repeat', detail: `Repeated ${last3Sigs[0]} 3 times in a row with no new result.` };
  }

  if (hasRepeatingPeriod(toolSigs, 2, 3) || hasRepeatingPeriod(toolSigs, 3, 2)) {
    const period = hasRepeatingPeriod(toolSigs, 2, 3) ? 2 : 3;
    const cyclePattern = lastN(toolSigs, period).join(' -> ');
    return { cause: 'oscillation', detail: `Cycling between the same ${period} actions (${cyclePattern}) without new progress.` };
  }

  const toolOutcomes = tailTrace.filter((t) => t.kind === 'tool' && t.phase === 'outcome').map(toolOutcome);
  const last3Outcomes = lastN(toolOutcomes, 3);
  if (last3Outcomes.length === 3 && last3Outcomes.every((o) => o && o.ok === false)) {
    return { cause: 'repeated_failure', detail: 'The last 3 attempts all failed, none of them different approaches that worked.' };
  }

  const notes = tailTrace.filter((t) => t.kind === 'note');
  if (notes.length >= 2) {
    const [a, b] = lastN(notes, 2);
    const similarity = textSimilarity(noteText(a), noteText(b));
    if (similarity >= NEAR_DUPLICATE_SIMILARITY) {
      return { cause: 'near_duplicate_reasoning', detail: 'The last two reasoning steps say essentially the same thing — no tool calls, no new direction.' };
    }
  }

  return null;
}

/** The step-budget backstop — a worker that never repeats but never converges either. `stepCount` is the caller's own live counter, not derived from the trace. */
export function stepBudgetExceeded(stepCount, kind) {
  const budget = STEP_BUDGET_BY_KIND[kind] ?? STEP_BUDGET_BY_KIND.generic;
  return stepCount >= budget;
}

// ---------------------------------------------------------------------------
// Steady-state supervision — capacity, resource contention, retry budget
// ---------------------------------------------------------------------------

/** `now - heartbeatAt` exceeding `timeoutMs` on a `running` job means the process is hung or dead — genuine silence, not "thinking hard." */
export function isHung(job, { now, timeoutMs }) {
  if (job?.status !== 'running' || !job.heartbeatAt) return false;
  return now - new Date(job.heartbeatAt).getTime() > timeoutMs;
}

/** `activeJobs` from job-store.js's listActiveJobs(). Never silently queue past this — the caller reports at_capacity and asks the user how to prioritize. */
export function hasCapacity(activeJobs, maxJobs) {
  return activeJobs.length < maxJobs;
}

/** A `resource` (e.g. 'computer') may be held by at most one active job at a time; `resource: null` means no exclusive claim. */
export function resourceAvailable(activeJobs, resource) {
  if (!resource) return true;
  return !activeJobs.some((j) => j.resource === resource);
}

/** One automatic attempt, full stop — a crash-retry and a live stall-recovery retry share this same counter. */
export function canAutoRetry(job) {
  return (job?.retries ?? 0) === 0;
}

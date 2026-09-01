// Turns real, already-happened events into improvement_outcomes rows — the
// raw material reflect.js later reviews. ZERO model calls anywhere in this
// file, on purpose: capture must never cost quota, since it fires on every
// job/task completion and every conversational turn, far more often than
// the budget-gated reflection cycle that reads what it wrote.
//
// Leaf-adjacent: imports improvement-store.js (leaf) and jobs/job-store.js
// (leaf) only — safe for cycle.js to call from a jobEvents subscription,
// and safe for models/runner.js (not itself leaf, but this import direction
// is fine either way) to call per conversational turn.

import { recordOutcome } from './improvement-store.js';
import * as jobStore from '../jobs/job-store.js';

const TERMINAL_JOB_STATUSES = new Set(['done', 'failed', 'cancelled', 'orphaned']);

/**
 * True for a job whose 'done' status is actually a SPLIT completion
 * (server/tools/request_job_split.js marks the original job 'done' with
 * `result: "Split into N separate job(s): ..."` and emits it on jobEvents
 * exactly like a real completion) — recording that as finished work would
 * be a lie: the work didn't finish, it got divided into new jobs that
 * haven't run yet. Checked two ways, not just the result-string prefix
 * alone, since a job's own OWN wording could theoretically change: a job
 * with children (`parent_id` pointing at it) is structurally a split
 * regardless of what its result text says.
 */
function isSplitCompletion(job) {
  if (typeof job.result === 'string' && job.result.startsWith('Split into')) return true;
  return jobStore.listJobs({ parentId: job.id }).length > 0;
}

/**
 * Called from cycle.js's jobEvents subscription (and its own orphan-polling
 * tick) whenever a job reaches a TERMINAL status. Idempotent by
 * construction (improvement-store.js's UNIQUE(source, source_ref) on
 * job.id) — orchestrator.js and worker.js both have real, confirmed-live
 * paths that can emit the same job's terminal status twice; this must
 * never record that as two outcomes.
 */
export function recordJobOutcome(jobId) {
  const job = jobStore.getJob(jobId);
  if (!job || !TERMINAL_JOB_STATUSES.has(job.status)) return null;
  if (isSplitCompletion(job)) return null;

  const trace = jobStore.getTrace(jobId);
  const toolCalls = trace.filter((t) => t.kind === 'tool' && t.phase === 'outcome');
  const failedTools = toolCalls.filter((t) => {
    try {
      return JSON.parse(t.detail || '{}').ok === false;
    } catch {
      return false;
    }
  });
  const escalations = jobStore.getOutboxForJob(jobId).length;

  return recordOutcome({
    source: 'job',
    sourceRef: job.id,
    entityRef: job.id, // a job never recurs — its entity IS the event
    title: job.title,
    goal: job.goal,
    kind: job.kind,
    status: job.status,
    retries: job.retries || 0,
    error: job.error || null,
    toolSummary: { totalToolCalls: toolCalls.length, failedToolCalls: failedTools.length },
    escalations,
  });
}

/** Called from scheduler.js right beside its existing memory checkpoint hook — see scheduler.js's own runTaskNow() for the exact call site. `run` is the just-recorded task-store.js run record; `task` is its parent task definition. */
export function recordTaskOutcome(run, task) {
  if (!run?.id) return null;
  return recordOutcome({
    source: 'task',
    sourceRef: run.id, // this ONE run — unique per call, what dedupe keys on
    entityRef: task?.id || null, // the SAVED task itself — stable across every future run, what reflect.js groups a recurring task's own pattern under
    title: task?.title || run.title,
    goal: task?.action?.text || task?.action?.skillName || task?.title || null,
    kind: task?.action?.type || null,
    status: run.ok ? 'done' : 'failed',
    retries: 0,
    error: run.error || null,
    toolSummary: null,
    escalations: 0,
  });
}

// Deliberately narrow and word-matching, same discipline as
// personality.js's DISTRESS_PATTERNS — a floor backstopped by the model's
// own record_lesson tool call for anything phrased less plainly than this.
// A false positive here just files a low-signal outcome that later needs
// >=2 pieces of evidence before it can even become a proposal (see
// improvement-policy.js's decide()), so the cost of over-matching is small;
// still kept reasonably tight so ordinary conversation doesn't constantly
// trip it.
const CORRECTION_PATTERNS = [
  /\bno,?\s+that'?s not what i (asked|meant|wanted|said)\b/i,
  /\bdon'?t do that again\b/i,
  /\byou keep (doing|making|getting) (that|this|the same)( mistake| thing)?\s*(wrong)?\b/i,
  /\bthat'?s not (right|correct|it)\b/i,
  /\bthat'?s (wrong|incorrect)\b/i,
  /\bnot what i (asked|meant|wanted|said)\b/i,
  /\bplease stop (doing|saying) that\b/i,
  /\bi (already |just )?told you (not to|to)\b/i,
  /\bactually,?\s+i (meant|wanted|asked for)\b/i,
  /\bnext time,?\s+(please\s+|just\s+|remember to\s+|make sure (?:you |to )?|don'?t\s+)/i,
];

function looksLikeCorrection(text) {
  return CORRECTION_PATTERNS.some((re) => re.test(text));
}

/**
 * Called from models/runner.js's runTurn(), live-conversation turns only
 * (the caller gates on `!opts.background` — a scheduled task or a Job
 * worker talking to itself was never corrected by anyone). No model call,
 * no dedupe key needed: unlike a job/task's terminal status, each
 * correction is a genuinely NEW event, never a duplicate of an earlier one,
 * so sourceRef is left null (SQLite's UNIQUE constraint treats every NULL
 * as distinct, so this never collides with a previous correction).
 */
export function noteCorrection(sessionId, userText) {
  const text = String(userText || '');
  if (!looksLikeCorrection(text)) return null;
  return recordOutcome({
    source: 'correction',
    sourceRef: null,
    title: 'User correction',
    goal: text.slice(0, 200),
    kind: 'conversation',
    status: 'failed', // a correction IS a signal that something went wrong
    retries: 0,
    error: null,
    toolSummary: null,
    escalations: 0,
  });
}

/**
 * Called from tools/record_lesson.js — the user PLAINLY taught Jarvis
 * something directly ("actually I always want the summary first"), not a
 * correction of a mistake and not an inferred pattern from job/task
 * outcomes. Filed as an 'explicit' outcome (status:'noted', not a job
 * status) purely so record_lesson.js's own lesson has a real outcome id to
 * cite as evidence — every lesson's evidence array is meant to trace back
 * to something real, and a direct statement from the user is exactly that,
 * just not a job/task completion. No model call here either.
 */
export function recordExplicitTeaching(text) {
  const trimmed = String(text || '').trim();
  if (!trimmed) return null;
  return recordOutcome({
    source: 'explicit',
    sourceRef: null,
    title: 'User taught Jarvis something directly',
    goal: trimmed.slice(0, 200),
    kind: 'conversation',
    status: 'noted',
    retries: 0,
    error: null,
    toolSummary: null,
    escalations: 0,
  });
}

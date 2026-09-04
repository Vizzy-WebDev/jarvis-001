// Drives ONE background Job to completion (or to a paused/escalated state),
// on its own session id `job:<id>` — never the live chat session, never
// bound to chat-store.js, so a worker structurally cannot write into the
// conversation the owner is looking at (see root CLAUDE.md's Jobs section).
//
// A "step" here means one runTurn() call — internally that can still run
// several rounds of tool calls (runner.js's own default maxToolSteps), but
// the WORKER only gets to inspect what happened and decide whether to
// continue, nudge-and-retry, or stop between whole runTurn calls, never
// mid-call. That's a deliberate, load-bearing choice: two runTurn calls on
// the same sessionId at once would race on conversation.js's session Map,
// so nothing here ever starts a second call while one is still in flight.
//
// The model signals completion itself, via two internal-only tools
// (server/tools/report_job_done.js / report_job_stuck.js) rather than a
// heuristic over its final prose — see those files for why they're
// `internal`, not `meta`, and how runner.js's allowedTools seeding is what
// actually makes them visible to a job's own turn.

import * as jobStore from './job-store.js';
import { jobEvents } from './job-events.js';
import { runTurn } from '../models/runner.js';
import * as conversation from '../conversation.js';
import { diagnoseStall, stepBudgetExceeded, canAutoRetry, STEP_BUDGET_BY_KIND, DIAGNOSE_TAIL_SIZE } from './job-policy.js';
import { classifyToolEffect } from './tool-effects.js';
import { addNotification } from '../notifications.js';
import { preparePlan, runControlSession } from '../control/session.js';
import { verifySemanticMatch } from '../ops/verify.js';

function nowIso() {
  return new Date().toISOString();
}

/**
 * Tier 3 ambient signal — never blocks, never forces a conversational
 * relay (that's prompt.js's jobsSection(), for Tier 1/2 only), just makes
 * "proactively tell me about unfinished work" true even if the owner never
 * starts another chat turn. `action` points at the Jobs screen (Phase 4).
 */
function notify(job, level, title, body) {
  try {
    addNotification({ kind: 'job', level, title, body, action: { label: 'View Jobs', section: 'jobs' }, meta: { jobId: job.id } });
  } catch (err) {
    console.error('[jobs] notification failed:', err);
  }
}

/** Every status-changing update also tells the orchestrator (via jobEvents, never events.js directly — see this directory's module-layout note in root CLAUDE.md's Jobs section: worker.js must not import events.js). */
function setStatus(jobId, patch) {
  const job = jobStore.updateJob(jobId, patch);
  jobEvents.emit('status', { jobId, status: job.status, title: job.title });
  return job;
}

/**
 * Drives ONE runTurn call to completion, translating its event stream into
 * write-ahead job_trace rows (tool_start -> 'intent' BEFORE invoke() runs,
 * tool_result -> 'outcome' after — see db.js's migration 4 comment for why
 * that ordering is the whole point) and a running heartbeat. Non-tool
 * assistant text between tool calls is flushed as a 'note' row each time a
 * tool call starts (and once more at the end) — what gives diagnoseStall's
 * near-duplicate-reasoning signal real per-step text to compare, for the
 * one failure mode (a model rambling in place with no tool calls at all)
 * the other signals can't see.
 */
async function driveOneTurn(jobId, sessionId, userText, allowedTools, kindByName) {
  let reasoningBuffer = '';
  let reportedDone = null;
  let reportedStuck = null;
  let finalText = '';
  let pausedReason = null;

  const flushReasoning = () => {
    const text = reasoningBuffer.trim();
    if (text) {
      jobStore.appendTrace(jobId, {
        phase: 'outcome',
        effect: 'read',
        kind: 'note',
        summary: text.length > 200 ? `${text.slice(0, 200)}…` : text,
        detail: text,
      });
    }
    reasoningBuffer = '';
  };

  // A confirm-gated tool (capability.confirm === 'always'/'ifUnclear') calls
  // this instead of minting a token nobody will ever resend — see
  // capabilities.js's invoke() and root CLAUDE.md's Jobs section on the
  // third confirm mode. Parks the job and raises the SAME Tier-1
  // outbox/awaiting_decision path a stall escalation does; driveJob's own
  // liveCheck (below) is what notices the status changed out from under it
  // and stops looping, rather than this callback trying to interrupt
  // anything mid-call.
  async function onEscalate({ name, args, summary }) {
    const job = jobStore.getJob(jobId);
    // A confirm-gated tool has no memory of having already asked once this
    // turn — runner.js's own internal loop can retry it several times
    // before this outer step ever gets a chance to notice the status
    // changed (worker.js's own liveCheck only runs BETWEEN whole runTurn
    // calls). Confirmed live: an unmodified model retrying the same
    // confirm-gated call produced 11 duplicate outbox rows for one decision
    // before this guard existed. Once already parked, later escalations
    // this same step are redundant — the first one already recorded
    // everything the owner needs to see.
    if (job?.status === 'awaiting_decision') return;
    jobStore.appendTrace(jobId, {
      phase: 'outcome',
      effect: 'read',
      kind: 'decision',
      summary: `Needs the owner's OK before continuing: ${summary}`,
    });
    setStatus(jobId, { status: 'awaiting_decision' });
    jobStore.addOutboxEntry(jobId, {
      tier: 1,
      reason: 'permission',
      summary: `I need your OK before I can continue "${job?.title || jobId}": ${summary}`,
      confirmPayload: { name, args },
    });
  }

  // Self-Improvement scope for THIS job's own turn — a rule learned
  // specifically for this job kind (e.g. 'job_kind:research') is injected
  // into volatile (see prompt.js's improvementScopedSection()), never
  // stable, since a different job kind's own turn must never see it.
  const scopeJob = jobStore.getJob(jobId);
  for await (const ev of runTurn(sessionId, userText, {
    source: 'text',
    background: true,
    autoConfirm: false,
    allowedTools,
    onEscalate,
    improvementScope: scopeJob?.kind ? [`job_kind:${scopeJob.kind}`] : undefined,
  })) {
    jobStore.touchHeartbeat(jobId);

    if (ev.type === 'chunk') {
      reasoningBuffer += ev.text;
    } else if (ev.type === 'tool_start') {
      flushReasoning();
      const effect = classifyToolEffect(ev.name, kindByName);
      jobStore.appendTrace(jobId, {
        phase: 'intent',
        effect,
        kind: 'tool',
        summary: `Calling ${ev.name}`,
        detail: JSON.stringify({ name: ev.name, args: ev.args }),
      });
    } else if (ev.type === 'tool_result') {
      const effect = classifyToolEffect(ev.name, kindByName);
      jobStore.appendTrace(jobId, {
        phase: 'outcome',
        effect,
        kind: 'tool',
        summary: `${ev.name} ${ev.ok ? 'succeeded' : 'failed'}`,
        detail: JSON.stringify({ name: ev.name, ok: ev.ok }),
      });
      if (ev.name === 'report_job_done' && ev.ok) reportedDone = ev.summary || 'Done.';
      if (ev.name === 'report_job_stuck' && ev.ok) reportedStuck = ev.summary || 'Blocked.';
    } else if (ev.type === 'model_switch') {
      jobStore.appendTrace(jobId, {
        phase: 'outcome',
        effect: 'read',
        kind: 'decision',
        summary: `Switched models mid-job: ${ev.reason}`,
      });
    } else if (ev.type === 'done') {
      finalText = ev.text;
    } else if (ev.type === 'paused') {
      pausedReason = ev.reason;
    }
    // 'restart' — a partial reply discarded after a mid-stream model
    // failure; nothing new to trace, the model_switch/paused that follows
    // already explains what happened.
  }
  flushReasoning();

  // Whole-snapshot overwrite after every completed step — see
  // conversation.js's loadSnapshot() and db.js's migration 4 comment. What
  // lets a job classified `resumable` continue with its real prior context
  // after a crash or a live hang, instead of restarting from just the goal.
  jobStore.updateJob(jobId, { transcript: conversation.getMessages(sessionId) });

  return { reportedDone, reportedStuck, finalText, pausedReason };
}

/**
 * Drives a `computer`-kind job — the one kind that bypasses the runTurn
 * tool-calling loop entirely and drives control/session.js's own
 * perceive/decide/act engine instead. By the time this runs, the job has
 * ALREADY gotten the owner's explicit go-ahead (job-actions.js's
 * createJobIfCapacity parks a fresh computer job straight into
 * `awaiting_decision` rather than `queued` — see that file — and
 * resumeStuckJob is what actually starts it, the same mechanism every other
 * "keep trying" resolution uses). Nothing here mints its own confirm_token
 * or reuses control_computer.js's tool-level confirm gate; that gate exists
 * for a LIVE turn with nobody else to ask — a background job already has a
 * durable, explicit answer on record before this ever runs.
 *
 * Every crash-recovery/hang-recovery mechanic built for the runTurn-based
 * kinds already generalizes here for free: an intent/outcome trace pair
 * still brackets the session (effect:'external', since operating the real
 * desktop is never safely repeatable), so a crash mid-session still
 * classifies `unrecoverable` via the exact same classifyRecovery() logic —
 * no special-casing needed. There is no transcript/session to snapshot or
 * rehydrate at all (control/session.js owns its own internal state), so a
 * computer job is never actually `resumable` after a real crash — a fact
 * the write-ahead trace surfaces honestly rather than one this file has to
 * assert.
 */
async function driveComputerJob(jobId, job, resumeText) {
  setStatus(jobId, { status: 'running', startedAt: job.startedAt || nowIso() });
  jobStore.touchHeartbeat(jobId);

  // A computer job has no conversation.js session to carry guidance in —
  // resumeText (the owner's own words from check_on_work's 'keep_going', or
  // the initial go-ahead) folds straight into the goal text instead.
  const goal = resumeText ? `${job.goal}\n\n${resumeText}` : job.goal;

  let plan = null;
  try {
    plan = await preparePlan(goal);
  } catch {
    // runControlSession derives its own plan when planText is falsy — same
    // fallback control_computer.js's own tool already relies on when its
    // cached plan has expired.
  }

  jobStore.appendTrace(jobId, {
    phase: 'intent',
    effect: 'external',
    kind: 'tool',
    summary: 'Starting a computer-control session',
    detail: JSON.stringify({ name: 'control_computer', args: { goal } }),
  });

  // control/session.js's own loop can legitimately run for many minutes
  // (MAX_STEPS=25, each step a real screenshot + model call) with no hook
  // to touch heartbeat mid-call. Without this, the orchestrator's hang
  // detector would eventually mistake a genuinely-still-working session for
  // a hung one and try to "recover" it — colliding with control/session.js's
  // own activeSession singleton (a second runControlSession call while one
  // is already running just returns a clean {status:'error'}, which would
  // wrongly fail a job that was never actually stuck). The honest tradeoff:
  // this can't distinguish "still working" from "hung inside one low-level
  // step with no internal timeout of its own" — control/session.js's own
  // DECIDE_TIMEOUT_MS already bounds the common case (a slow model call),
  // so what's left uncaught here is a narrower, rarer risk than the false
  // positive this avoids.
  const heartbeatTimer = setInterval(() => jobStore.touchHeartbeat(jobId), 20_000);

  let result;
  try {
    result = await runControlSession(goal, plan);
  } catch (err) {
    clearInterval(heartbeatTimer);
    jobStore.appendTrace(jobId, {
      phase: 'outcome',
      effect: 'external',
      kind: 'tool',
      summary: 'The control session threw an unexpected error',
      detail: JSON.stringify({ name: 'control_computer', ok: false }),
    });
    setStatus(jobId, { status: 'failed', error: err?.message || 'Something went wrong controlling the computer.', finishedAt: nowIso() });
    notify(job, 'error', `"${job.title}" ran into a problem`, err?.message || 'Something went wrong.');
    return;
  }
  clearInterval(heartbeatTimer);

  jobStore.appendTrace(jobId, {
    phase: 'outcome',
    effect: 'external',
    kind: 'tool',
    summary: `Computer-control session ended: ${result.status}`,
    detail: JSON.stringify({ name: 'control_computer', ok: result.status === 'done' }),
  });

  if (result.status === 'done') {
    setStatus(jobId, { status: 'done', result: result.summary || 'Done.', finishedAt: nowIso() });
    notify(job, 'success', `"${job.title}" is done`, result.summary || 'Done.');
    return;
  }
  if (result.status === 'stopped') {
    // The safety spine itself stopped it (real mouse movement, the global
    // hotkey, a blocklist hit) — a clean cancellation, not a failure.
    setStatus(jobId, { status: 'cancelled', finishedAt: nowIso(), error: result.reason || null });
    return;
  }
  if (result.status === 'stuck' || result.status === 'blocked') {
    setStatus(jobId, { status: 'awaiting_decision' });
    jobStore.addOutboxEntry(jobId, {
      tier: 1,
      reason: 'stuck',
      summary: `I'm stuck controlling the computer for "${job.title}" — ${result.reason}`,
    });
    notify(job, 'warning', `"${job.title}" needs your input`, result.reason || 'Stuck.');
    return;
  }
  // 'error'
  setStatus(jobId, { status: 'failed', error: result.reason || 'Something went wrong.', finishedAt: nowIso() });
  notify(job, 'error', `"${job.title}" ran into a problem`, result.reason || 'Something went wrong.');
}

/**
 * Drives a job from wherever it currently stands (fresh, or resuming from a
 * transcript snapshot after a crash-restart / a live hang recovery) through
 * to done/failed/awaiting_decision. Never throws past the orchestrator's own
 * catch — every exit path here writes a terminal or parked status itself.
 */
export async function driveJob(jobId, { allowedTools, kindByName, resumeText } = {}) {
  const job = jobStore.getJob(jobId);
  if (!job) return;
  if (job.kind === 'computer') {
    return driveComputerJob(jobId, job, resumeText);
  }
  const sessionId = `job:${jobId}`;

  const alreadyLive = conversation.getMessages(sessionId).length > 0;
  const resuming = Boolean(job.transcript?.length);
  if (resuming && !alreadyLive) {
    conversation.loadSnapshot(sessionId, job.transcript);
  }

  setStatus(jobId, { status: 'running', startedAt: job.startedAt || nowIso() });
  jobStore.touchHeartbeat(jobId);

  // `resumeText` (from orchestrator.js's resumeStuckJob) overrides the
  // generic continuation text with the owner's own guidance — the whole
  // point of asking "keep trying, or stop?" is wasted if the answer doesn't
  // actually reach the model.
  let nextText =
    resumeText ||
    (resuming
      ? 'Continue working on the goal from where you left off. Call report_job_done when you have ' +
        'fully finished, or report_job_stuck if you cannot proceed without the owner. If the goal would ' +
        'genuinely go better broken into separate pieces, call request_job_split instead.'
      : `${job.goal}\n\nWhen you have fully completed this, call report_job_done with a clear summary. ` +
        "If you get stuck and need the owner's input, call report_job_stuck instead of guessing. If this " +
        'would genuinely go better broken into separate, independent pieces of work, call request_job_split.');

  let stepsSoFar = 0;

  while (true) {
    stepsSoFar++;
    const turn = await driveOneTurn(jobId, sessionId, nextText, allowedTools, kindByName);

    if (turn.pausedReason) {
      // No model could handle it at all — a genuine failure, not a stall;
      // nothing about "try a different approach" would help this.
      setStatus(jobId, { status: 'failed', error: turn.pausedReason, finishedAt: nowIso() });
      notify(job, 'error', `"${job.title}" ran into a problem`, turn.pausedReason);
      return;
    }

    if (turn.reportedDone) {
      // Verification (root CLAUDE.md's Operational Awareness item 4) — "did
      // it finish" and "is it actually right" are different questions.
      // Reuses the EXACT SAME canAutoRetry/escalate shape diagnoseStall's
      // own retry branch already uses below, at the SAME call site, sharing
      // the SAME job.retries counter — never a second recovery mechanism. A
      // job that already spent its one retry on a stall gets no extra
      // retry for a verification mismatch, and vice versa.
      const verdict = await verifySemanticMatch({ request: job.goal, resultSummary: turn.reportedDone });
      if (verdict.checked && verdict.matches === false) {
        const current = jobStore.getJob(jobId);
        if (canAutoRetry(current)) {
          jobStore.appendTrace(jobId, {
            phase: 'outcome',
            effect: 'read',
            kind: 'decision',
            summary: `Reported done, but its own summary doesn't clearly match the goal (${verdict.reason || 'no reason given'}) — pausing to reconsider. This is the one automatic recovery attempt.`,
          });
          jobStore.updateJob(jobId, { retries: current.retries + 1 });
          nextText =
            `Before finishing: your own summary doesn't clearly match the actual goal ("${job.goal}") — ` +
            `${verdict.reason || 'reconsider whether this is genuinely complete'}. If it really is done, call ` +
            'report_job_done again with a summary that makes the connection to the goal clear. If it genuinely ' +
            "isn't done yet, keep working instead.";
          continue;
        }
        // Already spent the one retry (on this or an earlier stall) — the
        // SAME escalate move the stall branch below makes.
        jobStore.appendTrace(jobId, {
          phase: 'outcome',
          effect: 'read',
          kind: 'decision',
          summary: `Reported done again, but verification still doesn't match the goal (${verdict.reason || 'no reason given'}) after one reconsideration — escalating.`,
        });
        setStatus(jobId, { status: 'awaiting_decision' });
        jobStore.addOutboxEntry(jobId, {
          tier: 1,
          reason: 'stuck',
          summary: `"${job.title}" reported done, but I'm not confident the result actually matches the goal — ${verdict.reason || 'want to double check with you'}. Keep it as done anyway, or should I keep working?`,
        });
        notify(job, 'warning', `"${job.title}" may need a second look`, verdict.reason || 'The result may not fully match the goal.');
        return;
      }
      // checked:false (no model available) never blocks a real completion —
      // silence stays the safe failure direction, same as everywhere else
      // this build applies it.
      setStatus(jobId, { status: 'done', result: turn.reportedDone, finishedAt: nowIso() });
      notify(job, 'success', `"${job.title}" is done`, turn.reportedDone);
      return;
    }

    if (turn.reportedStuck) {
      // The model itself says it's blocked — a stronger signal than a
      // heuristic detecting a loop, so this escalates immediately without
      // spending the one automatic retry (see root CLAUDE.md's Jobs
      // section — nudging a job that just explicitly asked for the owner's
      // input would be presumptuous, not helpful).
      jobStore.appendTrace(jobId, {
        phase: 'outcome',
        effect: 'read',
        kind: 'decision',
        summary: `Reported stuck: ${turn.reportedStuck}`,
      });
      setStatus(jobId, { status: 'awaiting_decision' });
      jobStore.addOutboxEntry(jobId, {
        tier: 1,
        reason: 'stuck',
        summary: `I'm stuck on "${job.title}" — ${turn.reportedStuck}`,
      });
      notify(job, 'warning', `"${job.title}" needs your input`, turn.reportedStuck);
      return;
    }

    // Something OUTSIDE this loop may have changed the job's status between
    // steps — a stop_working_on cancellation, or driveOneTurn's own
    // onEscalate callback parking it on a confirm-gate decision. Either way,
    // respect it rather than overwriting it: this is the only point a stop
    // (or an escalation) CAN take effect, since an in-flight runTurn call
    // has no cancellation token (same limitation as control/session.js's
    // raceAgainstStop).
    const liveJob = jobStore.getJob(jobId);
    if (!liveJob || liveJob.status !== 'running') {
      return;
    }

    // Neither done nor stuck by its own account — check whether it's
    // actually stalled (alive, still emitting events, going nowhere) before
    // just letting it keep going.
    const tail = jobStore.getTraceTail(jobId, DIAGNOSE_TAIL_SIZE);
    const diagnosis = diagnoseStall(tail);
    const overBudget = !diagnosis && stepBudgetExceeded(stepsSoFar, job.kind);

    if (diagnosis || overBudget) {
      const current = jobStore.getJob(jobId);
      const cause = diagnosis?.cause || 'step_budget';
      const detail =
        diagnosis?.detail ||
        `Used up its step budget (${STEP_BUDGET_BY_KIND[job.kind] ?? STEP_BUDGET_BY_KIND.generic} steps) without finishing.`;

      if (canAutoRetry(current)) {
        jobStore.appendTrace(jobId, {
          phase: 'outcome',
          effect: 'read',
          kind: 'decision',
          summary: `Possible stall (${cause}): ${detail} Pausing to try a different approach — this is the one automatic recovery attempt.`,
        });
        jobStore.updateJob(jobId, { retries: current.retries + 1 });
        // Status never leaves 'running' — this is an internal recovery
        // step, not a visible failure (root CLAUDE.md's Jobs section).
        nextText =
          `You seem stuck: ${detail} Don't repeat the same action — try a genuinely different approach, ` +
          'or call report_job_stuck if you are actually blocked and need the owner.';
        continue;
      }

      // Already retried once and it happened again (or a fresh cause fired
      // right after the retry) — escalate rather than nudge indefinitely.
      jobStore.appendTrace(jobId, {
        phase: 'outcome',
        effect: 'read',
        kind: 'decision',
        summary: `Stalled again after one recovery attempt (${cause}: ${detail}) — escalating instead of retrying further.`,
      });
      setStatus(jobId, { status: 'awaiting_decision' });
      jobStore.addOutboxEntry(jobId, {
        tier: 1,
        reason: 'stuck',
        summary: `I got stuck working on "${job.title}" and a retry didn't help (${detail}). Keep trying a different way, or stop here?`,
      });
      notify(job, 'warning', `"${job.title}" needs your input`, detail);
      return;
    }

    // No stall, not done, not stuck — genuinely still working. Keep going.
    nextText =
      'Continue working toward the goal. Call report_job_done when finished, or report_job_stuck if you cannot proceed.';
  }
}

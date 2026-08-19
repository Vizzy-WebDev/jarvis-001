// The first background process in Jarvis. A 30-second tick checks every
// enabled task's due time and runs it — including a one-time "catch-up" run
// for anything that was due while the app was closed (Jarvis only runs
// while its window is open, by design — see CLAUDE.md), flagged `late`
// rather than silently skipped or replayed once per missed occurrence.
//
// Re-exports task-store.js's CRUD so server.js only needs to import one
// module for the whole "Scheduled Tasks" feature.

import { nextRunAt } from './recurrence.js';
import { composeBriefing } from './briefing.js';
import { runTurn, resetConversation } from '../models/runner.js';
import { runSkill } from '../skills/index.js';
import { getModel } from '../models/registry.js';
import { broadcast } from '../events.js';
import { addNotification } from '../notifications.js';
import { checkpointFromText } from '../memory/memory-review.js';
import { friendlyMessageFor } from '../friendly-message.js';
import {
  listTasks,
  getTask,
  createTask,
  updateTask,
  deleteTask,
  listRuns,
  recordRun,
} from './task-store.js';

export { listTasks, getTask, createTask, updateTask, deleteTask, listRuns };

const TICK_MS = 30 * 1000;
const LATE_THRESHOLD_MS = 2 * 60 * 1000; // more than 2 minutes overdue counts as "missed while closed"

let tickHandle = null;

function toIso(date) {
  return date ? date.toISOString() : null;
}

/**
 * One background model turn, collecting the final text plus which model
 * actually did it — every unattended run records this so Recent Activity
 * can say e.g. "ran using Claude Haiku — auto-switched from Gemini, no key
 * configured" instead of just a bare result.
 */
async function runOneTurn(sessionId, text, { modelId = null, noTools = false, allowedTools = null } = {}) {
  let summary = '';
  let ok = true;
  let error = null;
  let usedModelId = null;
  let switchedFrom = null;
  let switchReason = null;

  for await (const ev of runTurn(sessionId, text, {
    background: true,
    source: 'text',
    modelId: modelId || undefined,
    autoConfirm: true,
    noTools,
    allowedTools,
  })) {
    if (ev.type === 'model_switch' && !switchedFrom) {
      switchedFrom = ev.from;
      switchReason = ev.reason;
    }
    if (ev.type === 'done') {
      summary = ev.text;
      usedModelId = ev.modelId;
    }
    if (ev.type === 'paused') {
      ok = false;
      error = ev.reason;
    }
  }
  resetConversation(sessionId);
  return { ok, summary, error, usedModelId, switchedFrom, switchReason };
}

/** The 'skill' action: run the skill directly (the user already chose it — no need for a model to decide to call it), then have a model narrate the raw result into a short sentence for the log. Mirrors briefing.js's "gather facts in code, narrate via model" pattern. */
async function runSkillAction(task) {
  const { skillName, args, modelId } = task.action;
  const result = await runSkill(skillName, args || {}, { autoConfirm: true, source: 'task' });

  // NOT `!result.ok` — get_time returns no `ok` field on success at all, so
  // that check would silently treat every get_time-based task as a failure.
  if (result && result.ok === false) {
    // A skill's own {ok:false, error} is normally a tool result a MODEL
    // phrases into a spoken reply, not a direct UI string — but a
    // scheduler-driven run has no model narrating it (see runSkillAction's
    // own header comment: narration only happens for a *successful*
    // result, below), so this is the one place a skill error really does
    // reach the user raw, unfiltered by anything. Routed through the same
    // clean-sentence helper as the model-failure paths for that reason.
    return {
      ok: false,
      summary: '',
      error: result.error ? friendlyMessageFor(result.error, 'This step', `The "${skillName}" step didn't work.`) : `The "${skillName}" step didn't work.`,
    };
  }

  const prompt = [
    `A scheduled task just ran the "${skillName}" step, which returned the data below.`,
    'Say what it found, in one or two short spoken sentences, in your own words.',
    'Use ONLY this data — never invent or guess anything that is not here.',
    '',
    JSON.stringify(result, null, 2),
  ].join('\n');

  return runOneTurn(`task:${task.id}:${Date.now()}`, prompt, { modelId, noTools: true });
}

async function runAction(task) {
  if (task.action.type === 'briefing') {
    const result = await composeBriefing({ modelId: task.action.modelId });
    return {
      ok: result.ok,
      summary: result.text,
      usedModelId: result.modelId,
      switchedFrom: result.switchedFrom,
      switchReason: result.switchReason,
    };
  }

  if (task.action.type === 'message') {
    return { ok: true, summary: task.action.text || task.title };
  }

  if (task.action.type === 'skill') {
    return runSkillAction(task);
  }

  if (task.action.type === 'prompt') {
    // `task.action.connectors` (an array of allowed skill names, written by
    // the task-creation UI) restricts which tools the model can call for
    // this unattended run — absent/null means "all non-meta tools" (see
    // runner.js's toolsForTurn: background:true already drops meta skills
    // regardless of this).
    return runOneTurn(`task:${task.id}:${Date.now()}`, task.action.text, {
      modelId: task.action.modelId,
      allowedTools: task.action.connectors || null,
    });
  }

  return { ok: false, summary: '', error: `Unknown task action: ${task.action.type}` };
}

/** Runs a task immediately (used by both the tick and the "Run now" button in the UI). */
export async function runTaskNow(id, { late = false } = {}) {
  const task = getTask(id);
  if (!task) throw new Error(`Unknown task: ${id}`);

  // A task pinned to a model that's since been removed isn't broken — the
  // pin is simply ignored and auto-selection takes over (see runner.js's
  // leadWith) — but it's worth surfacing so the user notices and can
  // either re-pin or leave it on auto.
  const pinnedModelMissing = Boolean(task.action.modelId && !getModel(task.action.modelId));

  let result;
  try {
    result = await runAction(task);
  } catch (err) {
    console.error(`[scheduler] task "${task.id}" threw:`, err);
    result = { ok: false, summary: '', error: friendlyMessageFor(err, 'This task', 'Something went wrong running this task.') };
  }

  updateTask(id, { lastRunAt: new Date().toISOString() });

  const usedModel = result.usedModelId ? getModel(result.usedModelId) : null;
  const switchedFromModel = result.switchedFrom ? getModel(result.switchedFrom) : null;

  const run = recordRun({
    taskId: id,
    title: task.title,
    ranAt: new Date().toISOString(),
    late,
    ok: result.ok,
    summary: result.summary,
    error: result.error || null,
    modelId: result.usedModelId || null,
    modelLabel: usedModel?.label || result.usedModelId || null,
    switchedFromLabel: switchedFromModel?.label || result.switchedFrom || null,
    switchReason: result.switchReason || null,
    pinnedModelMissing,
    notify: task.notify || 'always',
  });
  broadcast({ type: 'task_run', ...run });

  // The "scheduled task completion" memory checkpoint (Stage 2) — scoped to
  // `prompt` actions only (free-text work through the model, the one action
  // type that can plausibly surface a new fact about the user) and only on
  // success. `message`/`briefing`/`skill` actions are mechanical or already
  // draw on existing data, not new personal facts, so checkpointing every
  // one of them would spend real model-call quota for little chance of
  // finding anything — see root CLAUDE.md's Memory section on why quota is
  // the binding constraint here. Fire-and-forget, same reasoning as the
  // other checkpoints: a task's own notification must never wait on this.
  if (task.action.type === 'prompt' && run.ok) {
    checkpointFromText(run.summary, { sourceKind: 'task', sourceRef: run.id }).catch((err) =>
      console.error('[scheduler] task-complete checkpoint failed:', err)
    );
  }
  // Same 'never'/'on_error'/'always' semantics the old in-transcript system
  // note used (public/app.js's connectEvents, before this moved to a real
  // notification) — 'never' stays silent always, 'on_error' only speaks up
  // when something went wrong, missing/unrecognized falls back to 'always'
  // so task records created before `notify` existed keep their behavior.
  const notify = run.notify || 'always';
  if (notify !== 'never' && !(notify === 'on_error' && run.ok)) {
    const suffix = run.late ? ' (it was due while Jarvis was closed)' : '';
    addNotification({
      kind: 'task_run',
      level: run.ok ? 'success' : 'error',
      title: run.ok ? `"${run.title}" ran${suffix}` : `"${run.title}" ran into a problem${suffix}`,
      body: run.ok ? '' : run.error || 'Unknown error.',
      action: { label: 'View tasks', section: 'tasks' },
      // Lets the notification's detail view (public/screens/_notification-
      // detail.js) look up the real run record — model used, any
      // auto-switch, the task's actual summary — instead of the row
      // having nothing more to show than what's already in title/body.
      meta: { runId: run.id },
    });
  }
  return result;
}

async function tick() {
  const now = new Date();
  for (const task of listTasks()) {
    if (!task.enabled || !task.nextRunAt) continue;
    const due = new Date(task.nextRunAt);
    if (due.getTime() > now.getTime()) continue;

    const late = now.getTime() - due.getTime() > LATE_THRESHOLD_MS;

    // Advance the schedule BEFORE running — so a task that throws, or a
    // server restart mid-run, can never get stuck re-firing the same due
    // timestamp forever, and a run missed for days only ever catches up once.
    if (task.recurrence.type === 'once') {
      updateTask(task.id, { enabled: false, nextRunAt: null });
    } else {
      updateTask(task.id, { nextRunAt: toIso(nextRunAt(task.recurrence, now)) });
    }

    await runTaskNow(task.id, { late });
  }
}

/** Starts the 30-second tick. Call once from server.js, after the HTTP server is listening. */
export function startScheduler() {
  if (tickHandle) return;
  tick().catch((err) => console.error('[scheduler] initial tick failed:', err));
  tickHandle = setInterval(() => {
    tick().catch((err) => console.error('[scheduler] tick failed:', err));
  }, TICK_MS);
}

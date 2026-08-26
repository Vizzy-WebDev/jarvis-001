// The Orchestrator: admission (capacity/resource gating, generic-kind tool
// set for now — research/files/computer arrive in Phase 4), steady-state
// supervision (starting queued jobs, hang detection), and crash recovery
// (classifying anything found `running` at startup). Reasoning happens only
// at the rare boundaries the plan calls for (none yet in Phase 2 — the
// admission-planning model call arrives in Phase 3, once there's a live
// conversation to announce the decomposition to); everything here is
// deterministic code, per turn.
//
// Two channels, on purpose, same shape as server/monitor/engine.js's
// monitorEvents: worker.js (and job-actions.js) emit on jobEvents
// (in-process only, so neither needs to import events.js) and THIS file
// re-broadcasts to the browser via events.js — only server.js imports this
// module, so nothing under server/tools/ ever reaches it (root CLAUDE.md's
// circular-import invariant: this file imports worker.js, which imports
// models/runner.js, which imports capabilities.js, which imports
// tools/index.js — a tool importing THIS file would deadlock the very
// loader that's trying to load it). Tool-safe job actions live in
// job-actions.js instead, a leaf module this file re-exports
// createJobIfCapacity/cancelJob/resumeStuckJob from unchanged, so
// server.js's routes don't need to know the split exists.

import * as jobStore from './job-store.js';
import { jobEvents } from './job-events.js';
import { driveJob } from './worker.js';
import { classifyRecovery, resourceAvailable, isHung, canAutoRetry, hasCapacity } from './job-policy.js';
import { resetSession, loadSnapshot } from '../conversation.js';
import { listCapabilities } from '../capabilities.js';
import { broadcast } from '../events.js';
import { getPrefs } from '../prefs.js';
import { addNotification } from '../notifications.js';

function notify(job, level, title, body) {
  try {
    addNotification({ kind: 'job', level, title, body, meta: { jobId: job.id } });
  } catch (err) {
    console.error('[jobs] notification failed:', err);
  }
}

export { createJobIfCapacity, cancelJob, resumeStuckJob, hasBackgroundCapacityNow, runningSummaries } from './job-actions.js';

const TICK_MS = 5_000;
// Generous relative to real model latency on a slow free-tier provider —
// background jobs aren't latency-sensitive the way live voice is (compare
// control/session.js's much tighter DECIDE_TIMEOUT_MS = 45s for a live,
// watched desktop-control loop). This only needs to catch a GENUINE hang
// (heartbeat silence, not "thinking hard") — see job-policy.js's isHung().
const HANG_TIMEOUT_MS = 90_000;

let tickHandle = null;
const activeWorkers = new Set(); // job ids this PROCESS is currently driving

function nowIso() {
  return new Date().toISOString();
}

// research/files restrict the generic runTurn loop to a curated tool list —
// the model still reasons iteratively (that's the whole point of a
// tool-calling loop over a single canned call into research.js/run_code.js
// directly: a real background research job often needs several searches,
// cross-referencing, deciding when it's actually answered the question).
// 'computer' is the one genuinely different kind — it bypasses the
// tool-calling loop entirely and drives control/session.js's own
// perceive/decide/act engine instead (see worker.js's driveComputerJob) —
// so it needs no tool list here at all.
const KIND_TOOL_NAMES = {
  research: ['web_search', 'read_web_page', 'look_it_up', 'check_claim', 'get_headlines', 'search_conversations'],
  files: ['run_code', 'analyze_spreadsheet', 'allow_folder'],
};

// Every runTurn-based kind gets these three, regardless of what's otherwise
// restricted — the completion/stuck/split signaling vocabulary is the same
// for every kind, not something 'research'/'files' should have to give up
// in exchange for a narrower tool list.
const INTERNAL_JOB_TOOLS = ['report_job_done', 'report_job_stuck', 'request_job_split'];

/**
 * The tool set + name->kind map for one runTurn-based job kind
 * ('generic'/'research'/'files' — 'computer' never calls this, see
 * startWorker below). Recomputed per job rather than cached: a connector's
 * tools can change between jobs (connected/disconnected mid-use).
 */
function buildToolsetForKind(kind) {
  const caps = listCapabilities({ includeMeta: false }); // already excludes internal tools
  const filtered = kind === 'generic' ? caps : caps.filter((c) => KIND_TOOL_NAMES[kind]?.includes(c.name));
  if (kind !== 'generic' && !KIND_TOOL_NAMES[kind]) {
    throw new Error(`Unknown job kind "${kind}".`);
  }
  const kindByName = new Map(filtered.map((c) => [c.name, c.kind]));
  for (const name of INTERNAL_JOB_TOOLS) kindByName.set(name, 'builtin');
  const allowedTools = [...filtered.map((c) => c.name), ...INTERNAL_JOB_TOOLS];
  return { allowedTools, kindByName };
}

function startWorker(jobId, { resumeText } = {}) {
  if (activeWorkers.has(jobId)) return;
  activeWorkers.add(jobId);
  const job = jobStore.getJob(jobId);
  // 'computer' bypasses the tool-list machinery entirely — driveJob()
  // itself branches on job.kind and never reads allowedTools/kindByName for
  // that kind.
  const toolset = job.kind === 'computer' ? {} : buildToolsetForKind(job.kind);
  driveJob(jobId, { ...toolset, resumeText })
    .catch((err) => {
      console.error(`[jobs] worker for "${jobId}" threw:`, err);
      jobStore.updateJob(jobId, { status: 'failed', error: err?.message || 'Something went wrong.', finishedAt: nowIso() });
      jobEvents.emit('status', { jobId, status: 'failed', title: job?.title });
    })
    .finally(() => activeWorkers.delete(jobId));
}

/**
 * Resuming/restarting a parked job goes through the SAME capacity gate a
 * fresh job does — without this, resuming several parked jobs in a row
 * could silently exceed prefs.maxBackgroundJobs through a back door
 * `work_in_background` itself would never allow.
 */
function assertCapacityForResume() {
  const active = jobStore.listActiveJobs();
  const prefs = getPrefs();
  if (!hasCapacity(active, prefs.maxBackgroundJobs)) {
    throw new Error(
      `Already at capacity (${active.length} of ${prefs.maxBackgroundJobs} background jobs running) — stop one or wait for one to finish first.`
    );
  }
}

/**
 * A job that's gone truly silent (no heartbeat at all — see isHung()) can't
 * be safely awaited forever, but its in-flight runTurn call also can't be
 * cancelled (no cancellation token exists in the adapters — same accepted
 * limitation as control/session.js's raceAgainstStop). Rather than trust
 * whatever partial state the hung call may have left in conversation.js's
 * live session, this discards it and resumes from the last COMPLETED step's
 * transcript snapshot — the abandoned call, if it ever resolves, finds the
 * job already moved on and its result is simply moot.
 */
async function recoverFromHang(job) {
  if (!canAutoRetry(job)) {
    jobStore.appendTrace(job.id, {
      phase: 'outcome',
      effect: 'read',
      kind: 'decision',
      summary: 'Stopped responding for a long time, and a retry already happened once — escalating instead of trying again.',
    });
    jobStore.updateJob(job.id, { status: 'awaiting_decision' });
    jobEvents.emit('status', { jobId: job.id, status: 'awaiting_decision', title: job.title });
    jobStore.addOutboxEntry(job.id, {
      tier: 1,
      reason: 'stuck',
      summary: `"${job.title}" stopped responding and a retry didn't help. Keep trying, or stop here?`,
    });
    notify(job, 'warning', `"${job.title}" needs your input`, 'Stopped responding and a retry did not help.');
    activeWorkers.delete(job.id);
    return;
  }

  jobStore.appendTrace(job.id, {
    phase: 'outcome',
    effect: 'read',
    kind: 'decision',
    summary: 'Stopped responding for a long time — resuming from the last saved point.',
  });
  jobStore.updateJob(job.id, { retries: job.retries + 1 });
  jobStore.touchHeartbeat(job.id); // buys time before the next tick re-triggers this on the same silence

  // resetSession/loadSnapshot are no-ops for a 'computer' job (it has no
  // conversation.js session at all — control/session.js manages its own
  // state) — harmless to call regardless of kind.
  resetSession(`job:${job.id}`);
  loadSnapshot(`job:${job.id}`, job.transcript || []);

  const toolset = job.kind === 'computer' ? {} : buildToolsetForKind(job.kind);
  // Deliberately bypasses the activeWorkers guard's usual add() — it's
  // already held from the original (now-abandoned) call; this just starts a
  // fresh driveJob() under the SAME jobId slot.
  driveJob(job.id, toolset)
    .catch((err) => {
      console.error(`[jobs] worker for "${job.id}" threw after hang recovery:`, err);
      jobStore.updateJob(job.id, { status: 'failed', error: err?.message || 'Something went wrong.', finishedAt: nowIso() });
      jobEvents.emit('status', { jobId: job.id, status: 'failed', title: job.title });
    })
    .finally(() => activeWorkers.delete(job.id));
}

async function tick() {
  // Start anything queued and not already resource-blocked. Capacity itself
  // was already checked at CREATION time (see createJobIfCapacity below) —
  // "never silently queue" means the caller is told up front, not that the
  // tick quietly holds a row back once it exists. A job can still sit
  // briefly `queued` here only for genuine resource contention (e.g. two
  // jobs both wanting the exclusive 'computer' claim).
  for (const job of jobStore.listJobs({ status: 'queued' })) {
    if (activeWorkers.has(job.id)) continue;
    if (!resourceAvailable(jobStore.listResourceHoldingJobs(), job.resource)) continue;
    // `resumeNote` (job-actions.js's resumeStuckJob, itself leaf-safe for
    // server/tools/ to call) is how a tool's "keep trying" response reaches
    // the worker without that tool importing this file — cleared the
    // moment it's read so a LATER re-queue of the same job never reuses
    // stale guidance.
    const resumeText = job.resumeNote || undefined;
    if (job.resumeNote) jobStore.updateJob(job.id, { resumeNote: null });
    startWorker(job.id, { resumeText });
  }

  const now = Date.now();
  for (const job of jobStore.listJobs({ status: 'running' })) {
    if (!activeWorkers.has(job.id)) continue; // not ours to supervise this tick (shouldn't happen outside a crash, which recoverOrphans() handles separately at startup)
    if (isHung(job, { now, timeoutMs: HANG_TIMEOUT_MS })) {
      recoverFromHang(job).catch((err) => console.error(`[jobs] hang recovery for "${job.id}" failed:`, err));
    }
  }
}

function describeOrphan(job, recovery) {
  const labels = {
    resumable: "It hadn't done anything I can't safely pick back up — I can resume it right where it left off.",
    restartable: "It had started some local work I can't fully trust after the restart, but nothing outside its own workspace — safe to start over.",
    needs_input: 'It was already waiting on a decision from you when this happened.',
    unrecoverable: "It had already taken an action I can't safely repeat or undo — I won't restart or resume this one automatically.",
  };
  return `While I was restarting, "${job.title}" was still marked as running. ${labels[recovery] || ''} Resume, restart, or discard it?`;
}

/**
 * Any job still `status: 'running'` at startup crashed — no process is
 * running it, so no heuristic is needed to know that (see job-policy.js's
 * classifyRecovery doc comment). Classified from its write-ahead trace,
 * marked `orphaned` (never auto-resumed), and reported via a Tier 1 outbox
 * row — the owner chooses resume/restart/discard, exactly per the build
 * spec ("never tell me it can resume if it can't"). Run once, at startup,
 * before the first tick.
 */
export function recoverOrphans() {
  for (const job of jobStore.listJobs({ status: 'running' })) {
    const trace = jobStore.getTrace(job.id);
    const recovery = classifyRecovery(job, trace);
    jobStore.updateJob(job.id, { status: 'orphaned', recovery });
    jobStore.appendTrace(job.id, {
      phase: 'outcome',
      effect: 'read',
      kind: 'decision',
      summary: `Found running with no active worker after a restart — classified "${recovery}".`,
    });
    jobStore.addOutboxEntry(job.id, { tier: 1, reason: 'permission', summary: describeOrphan(job, recovery) });
    notify(job, 'warning', `"${job.title}" was interrupted`, describeOrphan(job, recovery));
  }
}

/** Resumes an orphaned job classified `resumable` — rehydrates its transcript snapshot and continues, per job-store.js's transcript column and worker.js's own resume check. */
export function resumeOrphan(jobId) {
  const job = jobStore.getJob(jobId);
  if (!job || job.status !== 'orphaned') throw new Error('That job is not waiting on a resume/restart/discard decision.');
  if (job.recovery !== 'resumable') throw new Error(`This job is classified "${job.recovery}", not resumable — restart or discard it instead.`);
  assertCapacityForResume();
  jobStore.updateJob(jobId, { recovery: null });
  startWorker(jobId);
}

/** Starts an orphaned job over from scratch — disallowed for `unrecoverable` (restarting risks repeating an action that can't be safely repeated). */
export function restartOrphan(jobId) {
  const job = jobStore.getJob(jobId);
  if (!job || job.status !== 'orphaned') throw new Error('That job is not waiting on a resume/restart/discard decision.');
  if (job.recovery === 'unrecoverable') {
    throw new Error("This job already did something that can't be safely repeated — it can only be discarded, not restarted.");
  }
  assertCapacityForResume();
  jobStore.updateJob(jobId, { transcript: null, retries: 0, recovery: null });
  resetSession(`job:${jobId}`);
  startWorker(jobId);
}

/** Starts the 5-second tick and runs the one-time startup orphan sweep. Call once from server.js, after the HTTP server is listening — same convention as scheduler.js's startScheduler(). */
export function startOrchestrator() {
  if (tickHandle) return;
  jobEvents.on('status', ({ jobId, status, title }) => {
    broadcast({ type: 'job_progress', jobId, status, title });
  });
  recoverOrphans();
  tick().catch((err) => console.error('[jobs] initial tick failed:', err));
  tickHandle = setInterval(() => {
    tick().catch((err) => console.error('[jobs] tick failed:', err));
  }, TICK_MS);
}

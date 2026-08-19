// The watcher behind "tell me when X happens" — separate from the desktop
// control loop (control/session.js) on purpose: a monitor never moves the
// mouse or keyboard, it only looks, so it needs none of session.js's
// confirm/guard machinery and can run alongside an active control session
// without conflict (see the amber "Watching for" indicator vs. the red
// "controlling" one in public/app.js — both can be lit at once).
//
// Imports ps-bridge.js (the same low-level screen/window reader
// control/session.js uses), ai.js (for the one check kind that needs a
// model), events.js (to broadcast a trigger), and monitor-store.js — never
// models/runner.js, matching CLAUDE.md's circular-import invariant (nothing
// under server/skills/ may reach runner.js, and watch_for.js only imports
// this file plus the leaf store). server.js is the one place that reacts to
// a trigger by actually calling into runner.js for an 'act' monitor — kept
// out of this file specifically so it never becomes an edge back to the
// skill loader.
//
// Checks are deliberately ordered cheapest-first when a monitor's `check`
// could be answered more than one way: a window/process/file check is one
// cheap local call; `screen_looks_like` (a screenshot + a real model call)
// is the LAST resort, used only when the user's own request genuinely needs
// visual judgment ("tell me when the progress bar finishes") that nothing
// deterministic can answer. Burning a vision call every 2 seconds would
// exhaust a free-tier quota in minutes for something a free `windows` call
// could usually answer instead.

import { send as sendCommand } from '../control/ps-bridge.js';
import { askModel } from '../ai.js';
import { broadcast } from '../events.js';
import { saveScreenshot } from '../control/screenshot-store.js';
import { showIndicator, hideIndicator } from '../control/observation-bridge.js';
import { getMonitor, updateMonitor, listActiveMonitors, pruneFinished } from './monitor-store.js';
import { addNotification } from '../notifications.js';
import fs from 'node:fs';
import { EventEmitter } from 'node:events';

// A SEPARATE channel from the SSE broadcast() above — that one is for the
// browser UI (the amber "Watching for" bar, a toast notice); this one is
// in-process, for server.js to react to a trigger by actually dispatching
// the follow-up action through models/runner.js. Kept as two channels on
// purpose: this file must never import runner.js itself (see this file's
// header comment on the circular-import invariant), and server.js needs a
// way to know a monitor fired regardless of whether any browser tab happens
// to be open to receive the SSE event.
export const monitorEvents = new EventEmitter();

const MIN_POLL_MS = 2_000;
const MID_POLL_MS = 5_000;
const SLOW_POLL_MS = 15_000;
const MAX_POLL_MS = 30_000;

const VISION_MIN_INTERVAL_MS = 20_000;
const VISION_MAX_INTERVAL_MS = 2 * 60_000;

const MAX_LIFETIME_MS = 2 * 60 * 60_000; // 2 hours — see createMonitor's own default, kept in sync here as the hard ceiling this engine enforces regardless of what's stored

const timers = new Map(); // monitorId -> Node timer

// Which currently-watching monitors are ones that actually look at the
// screen (screen_looks_like) — backs the blue "Jarvis can see your screen"
// badge (observation-bridge.js). Deliberately lit for the WHOLE lifetime of
// such a watch, not just flashed during each individual checkScreenLooksLike()
// call: a screen_looks_like watch backs off to checking only every 20s-2min
// (see VISION_MIN/MAX_INTERVAL_MS below), and a badge that only flashed for
// the literal capture instant would be effectively invisible on that cadence
// — "Jarvis is watching your screen" is the honest ongoing state the user
// actually asked about, not "Jarvis glanced at your screen just now".
const visionWatchers = new Set();
// The single observation-bridge token representing "at least one
// screen_looks_like watch is running" — engine.js does its own internal
// ref-counting via visionWatchers (multiple such monitors can run at once)
// and only touches observation-bridge.js's own token pool once, at the 0->1
// and 1->0 transitions, so it behaves as ONE caller from that module's
// perspective (see observation-bridge.js's token comment for why that
// matters: a second, unrelated caller like look_at_screen.js must never be
// able to turn this off, and vice versa).
let indicatorToken = null;

function markVisionWatchStarted(monitorId) {
  const wasEmpty = visionWatchers.size === 0;
  visionWatchers.add(monitorId);
  if (wasEmpty) indicatorToken = showIndicator('Jarvis is watching your screen');
}

function markVisionWatchStopped(monitorId) {
  visionWatchers.delete(monitorId);
  if (visionWatchers.size === 0 && indicatorToken !== null) {
    hideIndicator(indicatorToken);
    indicatorToken = null;
  }
}

function basePollDelayMs(elapsedMs) {
  if (elapsedMs < 30_000) return MIN_POLL_MS;
  if (elapsedMs < 120_000) return MID_POLL_MS;
  if (elapsedMs < 600_000) return SLOW_POLL_MS;
  return MAX_POLL_MS;
}

function windowMatches(win, { processName, titleContains }) {
  if (processName && win.processName.toLowerCase() !== processName.toLowerCase()) return false;
  if (titleContains && !win.title.toLowerCase().includes(titleContains.toLowerCase())) return false;
  return true;
}

async function currentWindows() {
  const { windows } = await sendCommand('windows');
  return windows;
}

// ---------- individual check kinds, cheapest first ----------

async function checkWindowAppears(params) {
  const windows = await currentWindows();
  return windows.some((w) => windowMatches(w, params));
}

async function checkWindowGone(params) {
  const windows = await currentWindows();
  return !windows.some((w) => windowMatches(w, params));
}

async function checkWindowTitleMatches({ processName, titlePattern }) {
  const windows = await currentWindows();
  return windows.some(
    (w) => (!processName || w.processName.toLowerCase() === processName.toLowerCase()) && w.title.toLowerCase().includes(String(titlePattern || '').toLowerCase())
  );
}

async function windowedProcessNames() {
  const { processes } = await sendCommand('processes');
  return new Set(processes.map((p) => p.name.toLowerCase()));
}

async function checkProcessRunning({ processName }) {
  const names = await windowedProcessNames();
  return names.has(String(processName || '').toLowerCase());
}

async function checkProcessGone({ processName }) {
  const names = await windowedProcessNames();
  return !names.has(String(processName || '').toLowerCase());
}

function checkFileExists({ path: filePath }) {
  return fs.existsSync(filePath);
}

/**
 * The real "download finished" signal — a file that exists AND hasn't
 * changed size since the last check. `monitor.checkState` (persisted between
 * ticks) carries the previously-seen size so this works across polls without
 * needing its own separate timer.
 */
function checkFileSizeStable(monitor, { path: filePath }) {
  if (!fs.existsSync(filePath)) return { matched: false, nextCheckState: monitor.checkState };
  const size = fs.statSync(filePath).size;
  const prev = monitor.checkState?.lastSize;
  if (prev !== undefined && prev === size && size > 0) {
    return { matched: true, nextCheckState: { lastSize: size } };
  }
  return { matched: false, nextCheckState: { lastSize: size } };
}

async function checkElementTextMatches({ processName, titleContains, textContains }) {
  const windows = await currentWindows();
  const target = windows.find((w) => windowMatches(w, { processName, titleContains }));
  if (!target) return false;
  try {
    const { elements } = await sendCommand('read_window', { handle: target.handle });
    const needle = String(textContains || '').toLowerCase();
    return (elements || []).some((e) => `${e.name || ''} ${e.value || ''}`.toLowerCase().includes(needle));
  } catch {
    return false; // an unreadable window just means "not matched yet", not an error worth surfacing
  }
}

/** Last resort — a screenshot plus one real model call, asking a plain yes/no question. Callers are responsible for the vision-specific rate limit (see tick() below); this function just does the one check. */
async function checkScreenLooksLike({ processName, titleContains, description }) {
  const windows = await currentWindows();
  const target = processName || titleContains ? windows.find((w) => windowMatches(w, { processName, titleContains })) : windows.find((w) => w.foreground);

  const shot = await sendCommand('screenshot', target ? { handle: target.handle } : {});
  try {
    saveScreenshot(shot.base64, { label: target ? target.processName : 'monitor' });
  } catch {
    // Not saving it is a lesser failure than not answering the check.
  }

  const result = await askModel({
    prompt: `Looking at the current screen, is this true right now: "${description}"?`,
    system: 'Reply with JSON only: {"matches": true|false}.',
    media: [{ kind: 'image', mimeType: shot.mimeType, dataBase64: shot.base64 }],
    json: true,
    need: { vision: true },
  });
  return Boolean(result.ok && result.data?.matches === true);
}

/** Runs one monitor's check. Returns `{matched, nextCheckState?}` — most kinds don't need to carry state between ticks, only file_size_stable does. Exported (alongside the public API below) purely so each check kind is directly testable without a full monitor lifecycle, per CLAUDE.md's pure-logic-module pattern. */
export async function runCheck(monitor) {
  const { kind, ...params } = monitor.check;
  switch (kind) {
    case 'window_appears':
      return { matched: await checkWindowAppears(params) };
    case 'window_gone':
      return { matched: await checkWindowGone(params) };
    case 'window_title_matches':
      return { matched: await checkWindowTitleMatches(params) };
    case 'process_running':
      return { matched: await checkProcessRunning(params) };
    case 'process_gone':
      return { matched: await checkProcessGone(params) };
    case 'file_exists':
      return { matched: checkFileExists(params) };
    case 'file_size_stable':
      return checkFileSizeStable(monitor, params);
    case 'element_text_matches':
      return { matched: await checkElementTextMatches(params) };
    case 'screen_looks_like':
      return { matched: await checkScreenLooksLike(params) };
    default:
      throw new Error(`Unknown monitor check kind: "${kind}"`);
  }
}

function clearTimer(monitorId) {
  const timer = timers.get(monitorId);
  if (timer) clearTimeout(timer);
  timers.delete(monitorId);
}

function scheduleNext(monitorId, delayMs) {
  clearTimer(monitorId);
  const timer = setTimeout(() => tick(monitorId), delayMs);
  timer.unref?.(); // never keeps the process alive on its own
  timers.set(monitorId, timer);
}

async function tick(monitorId) {
  const monitor = getMonitor(monitorId);
  if (!monitor || monitor.status !== 'watching') {
    clearTimer(monitorId);
    return;
  }

  const elapsedMs = Date.now() - new Date(monitor.createdAt).getTime();
  if (elapsedMs > MAX_LIFETIME_MS || Date.now() > new Date(monitor.expiresAt).getTime()) {
    updateMonitor(monitorId, { status: 'expired' });
    broadcast({ type: 'monitor_expired', monitorId, description: monitor.description });
    addNotification({
      kind: 'monitor',
      level: 'info',
      title: 'Stopped watching',
      body: `"${monitor.description}" — nothing happened in time.`,
      // No standalone monitor-lookup route exists today (monitors are
      // ephemeral — expired/stopped ones aren't listed anywhere), so the
      // detail view currently shows nothing extra for this; kept for
      // forward-compatibility rather than left null, since it costs
      // nothing (meta already passes through end to end unchanged).
      meta: { monitorId },
    });
    markVisionWatchStopped(monitorId);
    clearTimer(monitorId);
    return;
  }

  let outcome;
  try {
    outcome = await runCheck(monitor);
  } catch {
    outcome = { matched: false }; // a transient failure just means "not yet" — keep polling rather than give up
  }

  const patch = { lastCheckedAt: new Date().toISOString() };
  if (outcome.nextCheckState !== undefined) patch.checkState = outcome.nextCheckState;
  updateMonitor(monitorId, patch);

  if (outcome.matched) {
    const triggered = updateMonitor(monitorId, { status: 'triggered', triggeredAt: new Date().toISOString() });
    broadcast({ type: 'monitor_triggered', monitorId, description: monitor.description, onTrigger: monitor.onTrigger });
    monitorEvents.emit('triggered', triggered);
    markVisionWatchStopped(monitorId);
    clearTimer(monitorId);
    return;
  }

  // screen_looks_like gets its own, much slower, backing-off cadence — the
  // rate limit is what actually protects a free-tier quota, the "last
  // resort" ordering above is what limits how OFTEN this expensive path is
  // even reached in the first place.
  let delay;
  if (monitor.check.kind === 'screen_looks_like') {
    const visionChecks = (monitor.checkState?.visionChecks || 0) + 1;
    delay = Math.min(VISION_MIN_INTERVAL_MS * 2 ** (visionChecks - 1), VISION_MAX_INTERVAL_MS);
    updateMonitor(monitorId, { checkState: { ...monitor.checkState, visionChecks } });
  } else {
    delay = basePollDelayMs(elapsedMs);
  }
  scheduleNext(monitorId, delay);
}

/** Starts (or resumes, e.g. after a server restart) watching one monitor. */
export function startWatching(monitorId) {
  const monitor = getMonitor(monitorId);
  if (monitor?.check?.kind === 'screen_looks_like') markVisionWatchStarted(monitorId);
  scheduleNext(monitorId, MIN_POLL_MS);
}

/** Stops a monitor immediately — the user's own "stop watching" action. */
export function stopWatching(monitorId) {
  clearTimer(monitorId);
  markVisionWatchStopped(monitorId);
  try {
    updateMonitor(monitorId, { status: 'stopped' });
  } catch {
    // Already gone — fine either way.
  }
}

/**
 * Stops every currently-active screen_looks_like watch — the one action
 * behind a click on the blue observation badge (server.js's
 * POST /api/observation/stop). The badge represents a single yes/no state
 * ("can Jarvis currently see my screen"), not a per-watch UI, so a click
 * stops all of them rather than asking which one — same simplification the
 * amber "Watching for" bar already lives with when two ordinary monitors
 * are running at once. Returns the stopped monitors so the caller can
 * broadcast monitor_stopped for each (keeping the amber bar in sync too).
 */
export function stopAllVisionWatches() {
  const ids = [...visionWatchers];
  const stopped = [];
  for (const id of ids) {
    const monitor = getMonitor(id);
    stopWatching(id);
    if (monitor) stopped.push(monitor);
  }
  return stopped;
}

/** Resumes every monitor that was still 'watching' when the server last stopped — called once at startup (server.js). Without this, a server restart would silently orphan any in-progress watch with no timer ever ticking again. */
export function resumeActiveMonitors() {
  for (const monitor of listActiveMonitors()) startWatching(monitor.id);
  pruneFinished(24 * 60 * 60_000); // drop anything that finished more than a day ago
}

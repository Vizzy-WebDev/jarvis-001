// CRUD over active/recent monitors (data/monitors.json) — leaf module, same
// dependency-free reasoning as scheduler/task-store.js (see CLAUDE.md's
// circular-import invariant): this file must never import skills/index.js,
// models/runner.js, or engine.js, and nothing here does. engine.js and the
// watch_for skill both depend on this; it depends on neither.
//
// A monitor is one "watch for X, then do Y" request: `check` describes the
// condition (one of the cheapest-first kinds engine.js knows how to
// evaluate), `onTrigger` describes what happens once it's met — either a
// plain notification or a follow-up instruction to actually carry out. Kept
// deliberately small; a monitor's own history isn't tracked the way task
// runs are, since a monitor is a one-shot watch, not a recurring job.

import { readJson, writeJson } from '../store.js';

const FILE = 'monitors';

function load() {
  return readJson(FILE, { monitors: [] });
}
function save(data) {
  writeJson(FILE, data);
}

function makeId() {
  return `m${Date.now().toString(36)}${Math.random().toString(36).slice(2, 8)}`;
}

export function listMonitors() {
  return load().monitors;
}

export function listActiveMonitors() {
  return load().monitors.filter((m) => m.status === 'watching');
}

export function getMonitor(id) {
  return load().monitors.find((m) => m.id === id) || null;
}

/**
 * `check` is `{kind, ...params}` — one of engine.js's known kinds.
 * `onTrigger` is `{mode: 'notify'}` or `{mode: 'act', instruction}`.
 * `expiresAt` defaults to 2 hours out if not given (engine.js's own
 * auto-expire ceiling — see its header comment for why an unbounded watch
 * isn't offered).
 */
export function createMonitor({ description, check, onTrigger, expiresAt }) {
  if (!check?.kind) throw new Error('A monitor needs a check.');
  const data = load();
  const now = new Date();
  const monitor = {
    id: makeId(),
    description: description || 'something',
    check,
    onTrigger: onTrigger || { mode: 'notify' },
    status: 'watching',
    createdAt: now.toISOString(),
    expiresAt: expiresAt || new Date(now.getTime() + 2 * 60 * 60 * 1000).toISOString(),
    lastCheckedAt: null,
    triggeredAt: null,
  };
  data.monitors.push(monitor);
  save(data);
  return monitor;
}

export function updateMonitor(id, patch = {}) {
  const data = load();
  const idx = data.monitors.findIndex((m) => m.id === id);
  if (idx === -1) throw new Error(`Unknown monitor: ${id}`);
  data.monitors[idx] = { ...data.monitors[idx], ...patch };
  save(data);
  return data.monitors[idx];
}

/** Drops monitors that finished a while ago — called occasionally by engine.js so data/monitors.json doesn't grow forever. Active ('watching') monitors are never touched. */
export function pruneFinished(olderThanMs) {
  const data = load();
  const cutoff = Date.now() - olderThanMs;
  data.monitors = data.monitors.filter((m) => m.status === 'watching' || new Date(m.createdAt).getTime() > cutoff);
  save(data);
}

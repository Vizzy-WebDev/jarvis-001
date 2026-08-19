// CRUD over scheduled tasks (data/tasks.json) and their run history
// (data/task-runs.json). Split out from scheduler.js so both the tick loop
// and the briefing composer (which lists today's tasks as one of its
// sections) can read tasks without importing each other.

import { readJson, writeJson } from '../store.js';
import { nextRunAt, describe } from './recurrence.js';

const TASKS_FILE = 'tasks';
const RUNS_FILE = 'task-runs';
const MAX_RUNS_KEPT = 200;

function loadTasks() {
  return readJson(TASKS_FILE, { tasks: [] });
}
function saveTasks(data) {
  writeJson(TASKS_FILE, data);
}
function loadRuns() {
  return readJson(RUNS_FILE, { runs: [] });
}
function saveRuns(data) {
  writeJson(RUNS_FILE, data);
}

function toIso(date) {
  return date ? date.toISOString() : null;
}

function makeId() {
  return `t${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`;
}

export function listTasks() {
  return loadTasks().tasks;
}

export function getTask(id) {
  return loadTasks().tasks.find((t) => t.id === id) || null;
}

export function createTask({ title, recurrence, action, enabled = true, notify = 'on_error' }) {
  if (!recurrence?.type) throw new Error('A task needs a recurrence.');
  if (!action?.type) throw new Error('A task needs an action.');
  const data = loadTasks();
  const task = {
    id: makeId(),
    title: title || describe(recurrence),
    recurrence,
    action,
    enabled,
    // 'always' | 'on_error' | 'never' — read directly off the task record by
    // scheduler.js's runTaskNow when it records a run; defaults to
    // 'on_error' so a task created without an explicit choice (e.g. the
    // schedule_task voice skill) doesn't spam a notice on every routine run.
    notify,
    nextRunAt: enabled ? toIso(nextRunAt(recurrence, new Date())) : null,
    createdAt: new Date().toISOString(),
    lastRunAt: null,
  };
  data.tasks.push(task);
  saveTasks(data);
  return task;
}

export function updateTask(id, patch = {}) {
  const data = loadTasks();
  const idx = data.tasks.findIndex((t) => t.id === id);
  if (idx === -1) throw new Error(`Unknown task: ${id}`);
  const task = { ...data.tasks[idx], ...patch };
  if (patch.recurrence || patch.enabled !== undefined) {
    task.nextRunAt = task.enabled ? toIso(nextRunAt(task.recurrence, new Date())) : null;
  }
  data.tasks[idx] = task;
  saveTasks(data);
  return task;
}

export function deleteTask(id) {
  const data = loadTasks();
  data.tasks = data.tasks.filter((t) => t.id !== id);
  saveTasks(data);
}

export function listRuns(taskId) {
  const runs = loadRuns().runs;
  return taskId ? runs.filter((r) => r.taskId === taskId) : runs;
}

export function recordRun(run) {
  const data = loadRuns();
  const full = { id: `r${Date.now().toString(36)}${Math.random().toString(36).slice(2, 8)}`, ...run };
  data.runs.unshift(full);
  data.runs = data.runs.slice(0, MAX_RUNS_KEPT);
  saveRuns(data);
  return full;
}

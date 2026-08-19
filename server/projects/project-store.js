// CRUD over projects being planned (data/projects.json) — one record per
// idea the user is talking through with Jarvis.
//
// Deliberately thin compared to the old plan-store.js. There is NO
// `questions[]`/`answers{}` queue — that queue is what made the old build a
// conveyor belt: once it existed, every reply the user gave got filed
// against the next unanswered slot, including a challenge or a change of
// mind that was never meant as an answer at all. Removing the queue is the
// single change that fixes that. Jarvis asks what's unclear in its own
// words, in the conversation itself, and just... talks.
//
// `decisions[]` is what's on disk instead: a plain, growing list of things
// that got settled during discussion, added one at a time
// (see server/skills/note_project_decision.js). This is also the
// deliberate mitigation for conversation memory being explicitly out of
// scope for this rebuild — the chat itself doesn't survive a restart, but
// what was actually decided does, and that's what the plan and the prompts
// are built from.
//
// A dependency-free leaf module — same circular-import reasoning as the
// content store: server/skills/ files import this, and skills/index.js
// dynamically imports every file in server/skills/ at load time, so nothing
// reachable from a skill may lead back to that loader.

import { readJson, writeJson } from '../store.js';

const FILE = 'projects';

function load() {
  return readJson(FILE, { projects: [] });
}

function save(data) {
  writeJson(FILE, data);
}

function makeId() {
  return `pj${Date.now().toString(36)}${Math.random().toString(36).slice(2, 8)}`;
}

/** Newest first. */
export function listProjects() {
  return [...load().projects].sort((a, b) => String(b.updatedAt).localeCompare(String(a.updatedAt)));
}

export function getProject(id) {
  return load().projects.find((p) => p.id === id) || null;
}

export function createProject({ idea, title, sessionId }) {
  const text = String(idea || '').trim();
  if (!text) throw new Error('A project needs an idea to start from.');

  const now = new Date().toISOString();
  const project = {
    id: makeId(),
    // Which conversation this belongs to — read for context when writing
    // the plan and the prompts, and used to resolve "the project we're
    // talking about" for a follow-up that doesn't name one explicitly.
    sessionId: sessionId || 'main',
    title: String(title || '').trim() || text.split(/\s+/).slice(0, 8).join(' '),
    idea: text,
    decisions: [],
    research: null,
    plan: null,
    prompts: null,
    promptOrder: null,
    target: null,
    // Which model did which step, so a project built by a weaker fallback
    // model isn't silently indistinguishable from one built by the good one.
    history: [],
    error: null,
    createdAt: now,
    updatedAt: now,
  };

  const data = load();
  data.projects.push(project);
  save(data);
  return project;
}

/** Merges a patch into a project. Returns null if it's been deleted mid-job — the user is allowed to bin a project while it's still working. */
export function updateProject(id, patch = {}) {
  const data = load();
  const idx = data.projects.findIndex((p) => p.id === id);
  if (idx === -1) return null;

  const next = { ...data.projects[idx], ...patch, id, updatedAt: new Date().toISOString() };
  data.projects[idx] = next;
  save(data);
  return next;
}

/** Appends one settled decision. Separate from updateProject so two decisions noted close together can't overwrite each other. */
export function addDecision(id, text) {
  const project = getProject(id);
  if (!project) return null;
  const clean = String(text || '').trim();
  if (!clean) return project;
  const decisions = [...(project.decisions || []), { text: clean, at: new Date().toISOString() }];
  return updateProject(id, { decisions });
}

/** Appends one "step X ran on model Y" entry. */
export function recordStep(id, step, modelLabel) {
  const project = getProject(id);
  if (!project) return null;
  const history = [...(project.history || []), { step, modelLabel: modelLabel || null, at: new Date().toISOString() }];
  return updateProject(id, { history });
}

export function deleteProject(id) {
  const data = load();
  data.projects = data.projects.filter((p) => p.id !== id);
  save(data);
}

/**
 * The project a follow-up most likely belongs to, within ONE conversation:
 * the most recently touched one in that session. Session-scoped so a
 * follow-up in one conversation can never resolve to a project discussed in
 * a completely different one.
 */
export function latestInSession(sessionId) {
  return (
    [...load().projects]
      .filter((p) => p.sessionId === (sessionId || 'main'))
      .sort((a, b) => String(b.updatedAt).localeCompare(String(a.updatedAt)))[0] || null
  );
}

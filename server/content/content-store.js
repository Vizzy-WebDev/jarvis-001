// CRUD over shared content (data/content.json) — one record per video,
// article, image, file or piece of text the user has handed to Jarvis.
//
// Deliberately thin compared to the old analysis-store.js. There is no
// `digest`, no permanent `claims` list, and no status pipeline — those
// existed because the old build assumed every piece of content would be
// summarized and picked over for claims. This one assumes nothing about
// what will be asked of it:
//
//   identity  — what the thing IS, from a free glance (no model call).
//               Title, rough kind, a length/size if known. This is what
//               lets Jarvis say "that's a 40-minute video on X — what do
//               you want from it?" before ever reading a byte of it.
//   material  — a cache, populated only once something has actually been
//               examined. Text sources (articles, pasted text) cache their
//               full text, which is cheap and always safe to re-read.
//               Expensive media (video/audio/image) additionally cache
//               `observations` — a neutral record of what a model actually
//               perceived — so a second question doesn't re-watch the thing.
//   findings  — one entry per question asked about this content and its
//               answer. Nothing here is "the" verdict; there can be many.
//
// A dependency-free leaf module — same circular-import reasoning as the
// project store: server/skills/ files import this, and skills/index.js
// dynamically imports every file in server/skills/ at load time, so nothing
// reachable from a skill may lead back to that loader.

import { readJson, writeJson } from '../store.js';

const FILE = 'content';

function load() {
  return readJson(FILE, { content: [] });
}

function save(data) {
  writeJson(FILE, data);
}

function makeId() {
  return `ct${Date.now().toString(36)}${Math.random().toString(36).slice(2, 8)}`;
}

/** Newest first. */
export function listContent() {
  return [...load().content].sort((a, b) => String(b.createdAt).localeCompare(String(a.createdAt)));
}

export function getContent(id) {
  return load().content.find((c) => c.id === id) || null;
}

export function createContent({ source, identity, sessionId }) {
  const now = new Date().toISOString();
  const record = {
    id: makeId(),
    // Which conversation this belongs to — findings are looked up by
    // session first, so two different pieces of content shared in two
    // different conversations never get crossed.
    sessionId: sessionId || 'main',
    source: source || { kind: 'text' },
    identity: identity || null,
    material: null,
    findings: [],
    createdAt: now,
    updatedAt: now,
  };

  const data = load();
  data.content.push(record);
  save(data);
  return record;
}

/** Returns null if the record was deleted mid-job — a normal thing for the user to do, not an error. */
export function updateContent(id, patch = {}) {
  const data = load();
  const idx = data.content.findIndex((c) => c.id === id);
  if (idx === -1) return null;

  const next = { ...data.content[idx], ...patch, id, updatedAt: new Date().toISOString() };
  data.content[idx] = next;
  save(data);
  return next;
}

/** Appends one question-and-answer. Separate from updateContent so concurrent findings can't overwrite each other's history. */
export function addFinding(id, { request, answer, intake, sources }) {
  const record = getContent(id);
  if (!record) return null;

  const finding = {
    id: `fd${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`,
    request: String(request || '').trim(),
    answer: answer || '',
    intake: intake || record.material?.intake || null,
    sources: Array.isArray(sources) ? sources : [],
    at: new Date().toISOString(),
  };

  return { record: updateContent(id, { findings: [...(record.findings || []), finding] }), finding };
}

export function deleteContent(id) {
  const data = load();
  data.content = data.content.filter((c) => c.id !== id);
  save(data);
}

/**
 * The content a follow-up most likely refers to, within ONE conversation.
 * Session-scoped on purpose — the old build's global "most recent" could
 * resolve a follow-up in one conversation to content shared in a completely
 * different one.
 */
export function latestInSession(sessionId) {
  return (
    [...load().content]
      .filter((c) => c.sessionId === (sessionId || 'main'))
      .sort((a, b) => String(b.updatedAt).localeCompare(String(a.updatedAt)))[0] || null
  );
}

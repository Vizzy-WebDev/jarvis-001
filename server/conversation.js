// Neutral, model-agnostic conversation store. One canonical transcript per
// session; server/adapters/*.js translate it to/from each model's own wire
// format on the way in and out. This is what lets a mid-conversation model
// switch (server/models/runner.js) preserve context, instead of the old
// design where each provider hoarded its own private history format and
// switching meant starting over.
//
// A neutral message:
//   { id, role: 'user'|'assistant'|'tool', text?,
//     toolCalls?: [{id,name,args}], toolResults?: [{id,name,result}],
//     modelId?, raw?: { adapter, content },
//     media?: [{ kind: 'image'|'video', mimeType, dataBase64?, uri? }] }
// `raw` carries a model's own reply object verbatim when its adapter needs
// exact round-tripping (Gemini's thought_signature — see adapters/gemini.js
// for the full explanation of why this matters).
// `media` (user messages only) carries images/video alongside text — e.g. a
// screenshot from a computer-control session (control/session.js), or a
// video/link the user shares directly. `dataBase64` for inline bytes, `uri`
// for a reference a model can fetch itself (Gemini accepts a YouTube URL
// directly as a `fileData` part — see adapters/gemini.js). Every adapter
// degrades gracefully if it doesn't support a given kind (see each adapter's
// toContents()/toMessages()-equivalent).
//
// Sessions are separate namespaces, not separate storage: live chat uses
// whichever conversation id is currently active (see brain.js/chat-store.js)
// each scheduled task run gets its own session id so a 7am briefing never
// mixes into what you were chatting about the night before.
//
// This in-memory Map is still the ONLY thing runner.js reads from mid-turn —
// it's the trimmed 60-message working window a model actually sees. A
// "bound" session additionally persists every message to chat-store.js's
// SQLite store, which keeps the FULL transcript forever (see hydrate() /
// bindSession() below) — the 60-entry cap here is a context-window /
// memory-pressure limit, not a data-retention one.

import * as chatStore from './chat-store.js';

const sessions = new Map(); // sessionId -> neutral message[]
const boundSessions = new Set(); // sessionId -> persisted to chat-store.js
const MAX_HISTORY_ENTRIES = 60;
let nextId = 1;

function newId() {
  return `m${nextId++}`;
}

function trim(sessionId) {
  const list = sessions.get(sessionId);
  if (list && list.length > MAX_HISTORY_ENTRIES) {
    sessions.set(sessionId, list.slice(list.length - MAX_HISTORY_ENTRIES));
  }
}

function push(sessionId, message) {
  const list = sessions.get(sessionId) || [];
  const full = { id: newId(), ...message };
  list.push(full);
  sessions.set(sessionId, list);
  trim(sessionId);
  if (boundSessions.has(sessionId)) {
    // Persist the neutral message as-is (minus the in-memory-only id, which
    // chat-store.js assigns its own row-based version of on read — nothing
    // downstream keys off this id across a restart, see chat-store.js).
    const { id, ...rest } = full;
    chatStore.appendMessage(sessionId, rest);
  }
  return full;
}

/**
 * Marks a session as persistent — every message pushed to it from now on is
 * also written to chat-store.js's SQLite store. Scheduled task runs and
 * briefings deliberately never call this, so they stay ephemeral and don't
 * show up as browsable conversations (see chat-store.js's CLAUDE.md note).
 */
export function bindSession(sessionId) {
  boundSessions.add(sessionId);
}

/**
 * Loads a persisted conversation's tail back into the in-memory working set
 * — called on startup (to restore the active conversation after a restart)
 * and whenever the user opens an older conversation from Chat History. Also
 * binds the session, since anything worth hydrating is worth persisting.
 */
export function hydrate(sessionId) {
  bindSession(sessionId);
  const stored = chatStore.getMessages(sessionId).map(({ id, ...rest }) => ({ id: newId(), ...rest }));
  sessions.set(sessionId, stored);
  trim(sessionId);
}

/** All neutral messages in a session, oldest first. */
export function getMessages(sessionId = 'main') {
  return sessions.get(sessionId) || [];
}

export function pushUserText(sessionId, text, { media } = {}) {
  return push(sessionId, { role: 'user', text, media });
}

export function pushAssistantText(sessionId, text, { modelId, raw } = {}) {
  return push(sessionId, { role: 'assistant', text, modelId, raw });
}

export function pushAssistantToolCalls(sessionId, toolCalls, { modelId, raw, text } = {}) {
  return push(sessionId, { role: 'assistant', toolCalls, modelId, raw, text });
}

export function pushToolResults(sessionId, toolResults) {
  return push(sessionId, { role: 'tool', toolResults });
}

export function resetSession(sessionId = 'main') {
  sessions.delete(sessionId);
}

export function resetAllSessions() {
  sessions.clear();
}

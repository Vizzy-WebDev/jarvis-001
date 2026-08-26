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
  // createdAt (real wall-clock time, not the model's business) is what lets
  // prompt.js's situationSection() tell the model how long it's actually
  // been since the previous turn — see runner.js's runTurn(), which reads
  // the PREVIOUS last message's createdAt before this push() call ever
  // fires. Added here (not left to chat-store.js's own created_at) so it's
  // available on every session, bound or not — a scheduled task's ephemeral
  // session has no chat-store row at all.
  const full = { id: newId(), createdAt: new Date().toISOString(), ...message };
  list.push(full);
  sessions.set(sessionId, list);
  trim(sessionId);
  if (boundSessions.has(sessionId)) {
    // Persist the neutral message as-is (minus the in-memory-only id, which
    // chat-store.js assigns its own row-based version of on read — nothing
    // downstream keys off this id across a restart, see chat-store.js).
    // createdAt is also excluded from the persisted payload — chat-store.js
    // already stamps its own created_at column on every row; rowToMessage()
    // reads it back from there on hydrate() rather than double-storing the
    // same timestamp in both the column and the JSON payload.
    const { id, createdAt, ...rest } = full;
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

/**
 * Restores a session's message list wholesale from a plain snapshot (e.g.
 * server/jobs/job-store.js's `jobs.transcript` column) — fresh ids assigned,
 * original `createdAt`/everything else preserved verbatim. Deliberately
 * NOT hydrate(): never binds to chat-store.js, so a rehydrated job session
 * still never appears in Chat History. Used by jobs/worker.js to continue a
 * job with its real prior context after a crash-restart or a live hang,
 * instead of restarting from just the job's goal text.
 */
export function loadSnapshot(sessionId, messages = []) {
  sessions.set(sessionId, messages.map((m) => ({ ...m, id: newId() })));
  trim(sessionId);
}

/**
 * The text an assistant message should be treated as having actually said —
 * `spokenText` if it was interrupted (see markLastAssistantInterrupted()
 * below), its full `text` otherwise. Every adapter's message-mapping
 * (toMessages()/toContents()) uses this instead of reading `.text` directly
 * for an assistant turn, so a barge-in never leaves the model believing,
 * on the NEXT turn, that it finished saying something it was actually cut
 * off partway through.
 */
export function assistantTextOf(message) {
  return message.interrupted ? message.spokenText || '' : message.text || '';
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

/**
 * Marks the most recent assistant message in a session as interrupted —
 * called when the duplex voice engine's barge-in cuts Jarvis off mid-reply.
 * `spokenText` is what the user actually heard (see public/voice/playback.js's
 * getSpokenText()), reconstructed from chunks that truly started playing —
 * NOT a slice of the full text by character count, which would need exact
 * whitespace agreement between the client's chunking and the model's own
 * spacing that isn't guaranteed to hold.
 *
 * The full generated `text` field is left untouched (nothing is deleted —
 * chat history can still show what the model would have finished saying);
 * only `interrupted`/`spokenText` are added. Every adapter's own message
 * mapping (toMessages()/toContents()) checks `interrupted` and sends
 * `spokenText` instead of the full `text` for that turn when building the
 * next request — the model should believe it said only what was truly
 * heard, not everything it happened to finish generating after being cut
 * off, or a follow-up like "why did you stop?" makes no sense to it.
 *
 * Returns false (a no-op) if there's no assistant message to mark yet — a
 * genuine race is possible (the interrupt arrives before pushAssistantText
 * has run, if the model was still mid-generation server-side when the user
 * barged in) — see runner.js's runOnEntry, which finishes generating and
 * pushes regardless of whether anyone is still listening client-side. That
 * race is a known, disclosed gap: rare (barge-in after the model has
 * already finished but before the client processed 'done' is a narrow
 * window), and the failure mode is only "the interruption wasn't recorded",
 * never wrong/corrupted data.
 */
export function markLastAssistantInterrupted(sessionId, spokenText) {
  const list = sessions.get(sessionId);
  if (!list) return false;
  for (let i = list.length - 1; i >= 0; i--) {
    if (list[i].role === 'assistant' && list[i].text) {
      list[i].interrupted = true;
      list[i].spokenText = spokenText;
      if (boundSessions.has(sessionId)) {
        chatStore.updateLastAssistantMessage(sessionId, { interrupted: true, spokenText });
      }
      return true;
    }
  }
  return false;
}

export function resetSession(sessionId = 'main') {
  sessions.delete(sessionId);
}

export function resetAllSessions() {
  sessions.clear();
}

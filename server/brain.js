// Thin dispatcher into the model runner — kept as a stable import point so
// server.js (and anything else) doesn't need to know the runner, registry,
// or adapters exist underneath. Used to forward to whichever single
// provider (Gemini/Claude/OpenAI) was active; now forwards to the runner,
// which picks from — and fails over across — the user's whole model list.
//
// The live chat's session id is no longer the hardcoded string 'main' — it's
// whichever conversation is currently active in chat-store.js's SQLite
// store (see server/chat-store.js, server/conversation.js's bindSession/
// hydrate). getActiveSessionId() is the one place that decision is made, so
// every caller here (and server.js's chat/chat-stream routes) stays in sync
// with whatever the user has open, including across a restart.

import { runTurn, resetConversation as resetRunnerConversation } from './models/runner.js';
import { bindSession, hydrate } from './conversation.js';
import * as chatStore from './chat-store.js';
import { checkpointConversation } from './memory/memory-review.js';

let activeId = null; // cached; chat-store.js's app_state table is the source of truth
let reopenCheckpointDone = false; // the "next open" memory checkpoint only ever fires once per process

/**
 * The session id every chat turn should use right now. Lazily creates the
 * very first conversation (fresh install) or resumes whatever was active
 * when Jarvis last closed — this is what makes "close and reopen, the
 * conversation is just there" work with no user action.
 */
export function getActiveSessionId() {
  if (activeId) return activeId;

  let id = chatStore.getActiveId();
  const isFreshInstall = !id || !chatStore.isConversation(id);
  if (isFreshInstall) {
    id = chatStore.createConversation().id;
    chatStore.setActiveId(id);
  }
  hydrate(id); // loads its persisted tail back into conversation.js's working set
  activeId = id;

  // The "next open" memory checkpoint (Stage 2) — covers "I closed Jarvis"
  // without depending on catching an unload event, which isn't reliable
  // (see root CLAUDE.md's Memory section). Fires once, the first time the
  // active conversation is actually resumed; a fresh install has nothing to
  // check yet. Fire-and-forget — reads a whole session's worth of
  // conversation, so it must never delay the very first request Jarvis
  // serves after starting up.
  if (!isFreshInstall && !reopenCheckpointDone) {
    reopenCheckpointDone = true;
    checkpointConversation(id, 'reopen').catch((err) => console.error('[brain] reopen checkpoint failed:', err));
  }

  return activeId;
}

/** Streams one turn in the currently active chat session. See models/runner.js for the full event shape. */
export async function* chatStream(userText, opts = {}) {
  yield* runTurn(getActiveSessionId(), userText, opts);
}

/** Non-streaming convenience wrapper — collects the streamed turn into a single reply string. */
export async function chat(userText, opts = {}) {
  let finalText = '';
  for await (const ev of chatStream(userText, opts)) {
    if (ev.type === 'done') finalText = ev.text;
    if (ev.type === 'paused') {
      throw Object.assign(new Error(ev.reason), { code: 'PAUSED' });
    }
  }
  return finalText;
}

/**
 * Starts a new chat: creates a fresh conversation, makes it active, and
 * returns it. Old behavior (wiping the session into nowhere) is gone —
 * the previous conversation stays exactly as it was, fully intact and
 * browsable from Chat History; this only changes what's *active* now.
 */
export function resetConversation() {
  const previousId = activeId || getActiveSessionId();
  // The "new chat" memory checkpoint (Stage 2) — the most reliable of the
  // checkpoints, since the user just clicked something. Fire-and-forget:
  // starting a new chat must feel instant, not wait on a background model
  // call over the conversation being left behind.
  checkpointConversation(previousId, 'new_chat').catch((err) => console.error('[brain] new-chat checkpoint failed:', err));

  resetRunnerConversation(previousId);
  const conv = chatStore.createConversation();
  chatStore.setActiveId(conv.id);
  bindSession(conv.id);
  activeId = conv.id;
  return conv;
}

/**
 * Switches the active conversation to an existing one (opening it from Chat
 * History) and hydrates its persisted transcript back into the working set
 * so the model has its context again. Throws if the id doesn't exist.
 */
export function activateConversation(id) {
  if (!chatStore.isConversation(id)) {
    throw new Error('That conversation no longer exists.');
  }
  hydrate(id);
  chatStore.setActiveId(id);
  activeId = id;
  return chatStore.getConversation(id);
}

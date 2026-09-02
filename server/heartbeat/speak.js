// Real proactive speech — the one genuinely new channel this build adds.
// Jarvis starting a turn nobody asked for: a real assistant message in the
// active conversation (persists, since brain.js's session is bound) plus a
// live SSE push so any open tab can actually play it out loud.
//
// Deliberately reuses brain.js's getActiveSessionId() rather than
// reimplementing session resolution here — that function is already the
// one authoritative place "which conversation is the user actually looking
// at, including across a restart" is decided (see root CLAUDE.md's Chat
// Persistence section); duplicating it would risk the two drifting apart.
// This does make this file (and engine.js/triggers.js/index.js above it)
// NOT leaf-safe — fine, since nothing under server/tools/ ever imports
// this file directly (only outbox-store.js, a true leaf, for
// acknowledge_notice.js's own needs).

import { getActiveSessionId } from '../brain.js';
import { pushAssistantText } from '../conversation.js';
import { broadcast } from '../events.js';
import { markDelivered } from './outbox-store.js';

/**
 * `outboxId`, when given, is marked delivered immediately — Jarvis
 * genuinely having said this in real time IS the resolving action for the
 * live-speech path, unlike the next-turn-injection fallback (which needs
 * acknowledge_notice.js once the model actually mentions it).
 */
export function speakNow(text, { outboxId = null, reason = null } = {}) {
  const sessionId = getActiveSessionId();
  pushAssistantText(sessionId, text, { modelId: null });
  broadcast({ type: 'proactive_message', text, reason });
  if (outboxId != null) markDelivered(outboxId);
}

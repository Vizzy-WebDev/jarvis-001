// The checkpoint engine — turns "something happened worth checking" (a new
// chat started, Jarvis reopened with unreviewed material, a scheduled task
// finished, the model itself sensed a natural stopping point) into a single
// batched extraction call, never one per turn. See root CLAUDE.md's Memory
// section for why: free-tier quota is the binding constraint on this
// project, and per-turn background extraction would roughly double model
// usage for a background feature the user never explicitly asked for that
// turn.
//
// An explicit "remember that X" never goes through this file at all — it's
// filed directly by remember_about_me.js the moment the user confirms it,
// at zero extra model-call cost. This file only handles the QUIET,
// self-noticing half of memory: candidates nobody asked for in the moment.
//
// Not a leaf module (imports ai.js, which is itself leaf-safe, plus
// chat-store.js/memory-store.js/events.js — none of which reach the
// loader/runner) — safe to import from scheduler.js and brain.js, but see
// each call site for why it's never imported from anywhere under
// server/skills/ directly (checkpoint_memories.js calls it, but that file
// itself lives under server/skills/ and this module doesn't import back
// into the loader, so the circular-import invariant still holds).

import { askModel } from '../ai.js';
import { broadcast } from '../events.js';
import * as chatStore from '../chat-store.js';
import * as memoryStore from './memory-store.js';
import { decide } from './memory-policy.js';

/**
 * The shared extraction step: one askModel call over a chunk of new
 * content, filing whatever it finds as pending candidates. `conversationId`
 * is set for a chat checkpoint (so a deleted conversation cascades its
 * still-unreviewed drafts — see db.js's migration-2 comment) and left null
 * for a non-chat source (a scheduled task has no conversation to cascade
 * from).
 */
async function extractAndFile({ transcript, conversationId = null, sourceKind, sourceRef }) {
  const policy = decide({ sourceKind });
  if (policy !== 'require-approval') {
    // Reserved for a future configurable trust policy — this build's
    // memory-policy.js only ever returns 'require-approval', so this branch
    // should be unreachable; if it isn't, fail safe rather than silently
    // auto-writing something nobody approved.
    console.warn(`[memory-review] unexpected policy "${policy}" from memory-policy.decide() — treating as require-approval.`);
  }

  const existingMemories = memoryStore.listMemories({});
  const categories = memoryStore.listCategories({ includePending: false }).map((c) => c.name);

  const memoriesBlock = existingMemories.length
    ? existingMemories.map((m) => `- [${m.id}] (${m.category}) ${m.text}`).join('\n')
    : '(none yet)';

  const prompt = [
    'Read the excerpt below and pull out any durable facts, preferences, or goals about the user that are worth remembering long-term.',
    'Skip anything trivial, one-off, or already obvious from context — only propose something a person would genuinely want recalled weeks from now.',
    '',
    `Existing categories: ${categories.join(', ')}. Use one of these if it fits; otherwise propose a short, clear new one.`,
    '',
    'Existing memories, for checking conflicts — does anything in the excerpt CONTRADICT one of these (a changed preference, an outdated fact)?',
    memoriesBlock,
    '',
    'Excerpt:',
    transcript,
    '',
    'Reply with JSON: {"candidates": [{"text": "...", "category": "...", "conflictsWithId": "mem_id or null"}]}.',
    'An empty candidates array is a completely normal answer — most excerpts have nothing worth remembering. Never invent a fact that is not actually stated or clearly implied.',
  ].join('\n');

  const result = await askModel({
    prompt,
    system:
      'You extract durable, worth-remembering facts about a user from a piece of conversation or activity. ' +
      'You are careful and conservative, and you never invent anything that was not actually said.',
    json: true,
    background: true, // not latency-critical — this never blocks a spoken reply
  });

  if (!result.ok || !result.data || !Array.isArray(result.data.candidates)) {
    return { candidates: [] };
  }

  const created = [];
  for (const raw of result.data.candidates) {
    const text = String(raw?.text || '').trim();
    if (!text) continue;
    let category = String(raw?.category || 'Uncategorized').trim() || 'Uncategorized';
    if (!categories.includes(category)) {
      // A new category is never automatically permanent — it rides in the
      // same approval batch as its own candidates (memory-store.js's
      // approveCandidate() approves it the moment anything in it is
      // approved; it otherwise just sits as 'pending').
      memoryStore.proposeCategory(category);
    }
    const conflictWith = raw?.conflictsWithId && existingMemories.some((m) => m.id === raw.conflictsWithId) ? raw.conflictsWithId : null;
    const candidate = memoryStore.createCandidate({ conversationId, sourceKind, sourceRef, category, text, conflictWith });
    if (candidate) created.push(candidate);
  }

  if (created.length) {
    // The proactive half of "present them at natural checkpoints" — the
    // review card appears on its own; the user never has to ask for it
    // (review_memories.js exists for the explicit "what's pending?" case).
    // hydrateCandidate() adds the conflicting memory's current text, if
    // any, so the card can show an old-vs-new comparison with no extra
    // round trip.
    broadcast({ type: 'memory_candidates_ready', count: created.length, candidates: created.map(memoryStore.hydrateCandidate) });
  }
  return { candidates: created };
}

/**
 * The chat checkpoint — reads only what's NEW since this conversation's
 * last checkpoint (memory-store.js's memory_checkpoints table), so a
 * repeated "new chat" / "reopen" checkpoint on a quiet conversation costs
 * nothing extra. Safe to call on a non-persisted/unknown session id (a
 * scheduled task's ephemeral session, an id that's since been deleted) —
 * it just no-ops.
 */
export async function checkpointConversation(conversationId, reason = 'unspecified') {
  if (!conversationId || !chatStore.isConversation(conversationId)) return { candidates: [] };

  const { lastSeq } = memoryStore.getCheckpoint(conversationId);
  const since = chatStore.getMessagesSince(conversationId, lastSeq);
  const relevant = since.filter((m) => (m.role === 'user' || m.role === 'assistant') && m.text);

  if (!relevant.length) {
    // Nothing new — still advance the pointer so a later checkpoint doesn't
    // re-scan messages (tool calls, empty turns) that were already looked
    // at and correctly judged to have nothing worth remembering.
    if (since.length) memoryStore.setCheckpoint(conversationId, since[since.length - 1].seq);
    return { candidates: [] };
  }

  const transcript = relevant.map((m) => `${m.role === 'user' ? 'User' : 'Jarvis'}: ${m.text}`).join('\n');
  const result = await extractAndFile({ transcript, conversationId, sourceKind: 'chat', sourceRef: conversationId });
  memoryStore.setCheckpoint(conversationId, since[since.length - 1].seq);
  return result;
}

/**
 * A checkpoint over plain text rather than a stored conversation — for a
 * scheduled task's own result (which runs in an ephemeral, never-persisted
 * session, see chat-store.js/CLAUDE.md) and, later, any other non-chat
 * source (a document, a connected app) without needing a schema change:
 * every candidate already carries a generic {sourceKind, sourceRef}.
 */
export async function checkpointFromText(text, { sourceKind, sourceRef } = {}) {
  const trimmed = String(text || '').trim();
  if (!trimmed) return { candidates: [] };
  return extractAndFile({ transcript: trimmed, conversationId: null, sourceKind, sourceRef });
}

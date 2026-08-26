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
import { addNotification } from '../notifications.js';
import * as chatStore from '../chat-store.js';
import * as memoryStore from './memory-store.js';
import { decide } from './memory-policy.js';

/**
 * Same normalization spirit as remember_about_me.js's findSimilar() (see
 * that file for why it's duplicated rather than shared): lowercase, strip
 * punctuation, collapse whitespace, so "Uses WSL for development work." and
 * "uses wsl for development work" compare equal. Used below to tell a real
 * conflict (a genuinely changed fact) apart from a DUPLICATE (the same fact,
 * reworded) — see the loop in extractAndFile().
 */
function normalizeText(s) {
  return String(s || '')
    .trim()
    .toLowerCase()
    .replace(/[^\w\s]/g, '')
    .replace(/\s+/g, ' ');
}

/**
 * The shared extraction step: one askModel call over a chunk of new
 * content, filing whatever it finds as pending candidates. `conversationId`
 * is set for a chat checkpoint (so a deleted conversation cascades its
 * still-unreviewed drafts — see db.js's migration-2 comment) and left null
 * for a non-chat source (a scheduled task has no conversation to cascade
 * from).
 */
async function extractAndFile({ transcript, conversationId = null, sourceKind, sourceRef }) {
  const existingMemories = memoryStore.listMemories({});
  const categories = memoryStore.listCategories({ includePending: false }).map((c) => c.name);
  // Confirmed live against a real conversation: without this, a fact the
  // user already has (once auto-saved, once Jarvis later recalls it out
  // loud — see checkpointConversation()'s labeling below) gets re-extracted
  // as a "conflict" against ITSELF, and the conflict floor then forces an
  // approval card for something the user already has. The model needs to
  // see what's already pending too, or the same gap reopens for anything
  // still sitting in the review queue when a related topic comes up again.
  const pendingCandidates = memoryStore.listPendingCandidates();

  const memoriesBlock = existingMemories.length
    ? existingMemories.map((m) => `- [${m.id}] (${m.category}) ${m.text}`).join('\n')
    : '(none yet)';
  const pendingBlock = pendingCandidates.length
    ? pendingCandidates.map((c) => `- (${c.category}) ${c.text}`).join('\n')
    : null;

  const prompt = [
    'Read the excerpt below and pull out any durable facts, preferences, or goals about the user that are worth remembering long-term.',
    'Skip anything trivial, one-off, or already obvious from context — only propose something a person would genuinely want recalled weeks from now.',
    '',
    `Existing categories: ${categories.join(', ')}. Use one of these if it fits; otherwise propose a short, clear new one.`,
    '',
    'Existing memories. Do NOT propose anything already covered by one of these — not reworded, not re-confirmed. Only flag one via conflictsWithId if the excerpt genuinely CONTRADICTS it (a changed preference, an outdated fact) — restating or recalling the same fact again is not a contradiction.',
    memoriesBlock,
    ...(pendingBlock ? ['', 'Already proposed and awaiting the user\'s own decision — don\'t propose these again either, even reworded:', pendingBlock] : []),
    '',
    'Excerpt:',
    transcript,
    '',
    'Reply with JSON: {"candidates": [{"text": "...", "category": "...", "conflictsWithId": "mem_id or null", "confidence": 0.0-1.0}]}.',
    '"confidence": how sure you are this is a real, durable fact the user would want kept. 0.9+ only when they stated it plainly and directly about themselves; 0.6-0.8 when it is clearly implied but not said outright; below 0.5 when you are inferring or unsure. Be honest and conservative here — most things are not 0.9. This number controls whether the fact is saved automatically or held for the user to review, so an inflated score is not a harmless guess.',
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

  const pending = [];
  const autoSaved = [];
  for (const raw of result.data.candidates) {
    const text = String(raw?.text || '').trim();
    if (!text) continue;
    let category = String(raw?.category || 'Uncategorized').trim() || 'Uncategorized';
    if (!categories.includes(category)) {
      // A new category is never automatically permanent — it rides in the
      // same approval batch as its own candidates (memory-store.js's
      // approveCandidate()/autoApproveCandidate() both approve it the
      // moment anything in it is saved; it otherwise just sits as
      // 'pending'). This holds whether the candidate itself ends up
      // approved by the user or auto-saved — either way a category the
      // model just invented only becomes real once something actually
      // lands in it.
      memoryStore.proposeCategory(category);
    }
    const conflictWith = raw?.conflictsWithId && existingMemories.some((m) => m.id === raw.conflictsWithId) ? raw.conflictsWithId : null;

    // A deterministic backstop, independent of the model actually following
    // the "don't propose what's already covered" instruction above — and
    // deliberately checked against EVERY existing memory/pending candidate,
    // not just whichever one (if any) the model itself flagged via
    // conflictsWithId, since a model that silently re-proposes an existing
    // fact WITHOUT flagging it as a conflict at all would otherwise slip
    // straight through untouched. Bidirectional CONTAINMENT, not exact
    // equality — confirmed live that strict equality misses realistic
    // paraphrasing (the extraction model wrote "User's sister is getting
    // married in March." against a saved memory reading "Sister is getting
    // married in March" — those never normalize to the same string, but one
    // clearly contains the other).
    const normalizedText = normalizeText(text);
    const isDuplicate = (otherText) => {
      const other = normalizeText(otherText);
      return Boolean(normalizedText && other && (normalizedText.includes(other) || other.includes(normalizedText)));
    };
    if (existingMemories.some((m) => isDuplicate(m.text)) || pendingCandidates.some((c) => isDuplicate(c.text))) {
      // Confirmed live: this is exactly what happened when Jarvis auto-saved
      // a fact, later recalled it out loud, and the next checkpoint
      // re-extracted its own recall as "new" evidence, misfiled as a
      // conflict against the very memory it duplicated. A REAL conflict
      // (genuinely changed text) never matches here and still reaches
      // decide()'s hard floor exactly as before.
      continue;
    }

    const confidenceRaw = Number(raw?.confidence);
    const confidence = Number.isFinite(confidenceRaw) ? Math.max(0, Math.min(1, confidenceRaw)) : null;
    const candidate = memoryStore.createCandidate({ conversationId, sourceKind, sourceRef, category, text, conflictWith, confidence });
    if (!candidate) continue;

    // decide() is asked per-candidate, after the row exists — its hard
    // floor (a conflict, or no usable confidence score) always wins
    // regardless of the user's trust setting; see memory-policy.js.
    const policy = decide({ confidence: candidate.confidence, conflictsWithId: candidate.conflictWith });
    if (policy === 'auto-approve') {
      autoSaved.push(memoryStore.autoApproveCandidate(candidate.id));
    } else {
      pending.push(candidate);
    }
  }

  if (pending.length) {
    // The proactive half of "present them at natural checkpoints" — the
    // review card appears on its own; the user never has to ask for it
    // (review_memories.js exists for the explicit "what's pending?" case).
    // hydrateCandidate() adds the conflicting memory's current text, if
    // any, so the card can show an old-vs-new comparison with no extra
    // round trip.
    broadcast({ type: 'memory_candidates_ready', count: pending.length, candidates: pending.map(memoryStore.hydrateCandidate) });
  }
  if (autoSaved.length) {
    // A saved fact the user never approved in the moment still needs to be
    // visible and undoable — the notification bell (persisted, so it's
    // there even if no tab was open) plus a live event so an open Memory
    // screen refreshes without polling.
    addNotification({
      kind: 'memory',
      level: 'info',
      title: autoSaved.length === 1 ? 'Jarvis remembered something on its own' : `Jarvis remembered ${autoSaved.length} things on its own`,
      body: autoSaved.map((m) => m.text).join(' · '),
      action: { label: 'Review Memory', section: 'memory' },
    });
    broadcast({ type: 'memory_auto_saved', count: autoSaved.length, memories: autoSaved });
  }
  return { candidates: pending, autoSaved };
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

  // Assistant turns are labeled as a RECALL, not a fresh statement — the
  // actual cause (confirmed live) of the duplicate-conflict loop above:
  // when Jarvis says a saved fact back out loud ("you mentioned your
  // sister's wedding is in March"), the plain "Jarvis: ..." label gave the
  // extraction model no way to tell that apart from new information. The
  // memories/pending-candidates blocks above are the belt; this is the
  // suspenders — even if the model can't cross-reference perfectly, the
  // label itself discourages treating a recall as new evidence.
  const transcript = relevant
    .map((m) =>
      m.role === 'user'
        ? `User: ${m.text}`
        : `Jarvis (recalling something already remembered — not the user newly stating it): ${m.text}`
    )
    .join('\n');
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

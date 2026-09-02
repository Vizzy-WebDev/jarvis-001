// Utterance provenance — the actual verifier, per root CLAUDE.md's
// "Self-Model" section. The audit's central finding: check_myself retrieves
// real data, then a model builds a sentence on top of it, and nothing
// checks the sentence used the data faithfully. This does not attempt to
// verify free-form prose at all — that's not solvable, and this file never
// tries. It verifies exactly one narrow, checkable thing: did a specific
// NUMBER from a specific self-model snapshot actually reappear in the real
// reply that followed it.
//
// Leaf-adjacent: imports self-store.js and chat-store.js, both leaves —
// self-store.js deliberately never imports chat-store.js itself (see that
// file's own header comment), so this is the one place the two meet.
// Neither import path reaches capabilities.js/tools/index.js/runner.js, so
// this stays safe for server/tools/ to import if a future tool ever wants
// to expose verifyCitation() directly (none does yet — today this is a
// forensic function, called from a one-off script the way earlier real
// bugs in this project were diagnosed, per root CLAUDE.md's "No automated
// test suite" section).

import { getSelfModelSnapshot } from './self-store.js';
import { getByPath } from './self-model.js';
import { getMessages } from '../chat-store.js';

/** Every text form a model might plausibly have written for a number — the raw value, and for something that reads as a 0..1 rate, its rounded percentage too ("1" -> also "100%"; "0.85" -> also "85%"). Deliberately small and literal, never a fuzzy/semantic match — see this file's header comment on why. */
function candidateStrings(value) {
  const forms = new Set([String(value)]);
  if (value >= 0 && value <= 1) {
    forms.add(`${Math.round(value * 100)}%`);
    forms.add(String(Math.round(value * 100)));
  }
  return [...forms];
}

/**
 * Whether `numStr` (a plain digit string, e.g. "0" or "100") appears in
 * `text` as its own number, never as a coincidental substring of a LONGER
 * one — a real bug caught during this fix's own verification: a bare
 * `.includes()` check let `0` "match" inside `100%`, since "100%" contains
 * the character "0". Digit-only forms are checked with lookaround so a
 * neighbouring digit fails the match; a form ending in `%` only needs its
 * own left boundary checked, since `%` itself can never be mistaken for
 * part of a longer number.
 */
function containsNumberForm(text, form) {
  const escaped = form.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const pattern = form.endsWith('%') ? `(?<!\\d)${escaped}` : `(?<!\\d)${escaped}(?!\\d)`;
  return new RegExp(pattern).test(text);
}

/**
 * Finds the message row (from chat-store.js's own getMessages(), the real
 * persisted transcript) whose toolResults array contains `toolCallId`, then
 * the first LATER message that's a genuine final reply — role:'assistant',
 * real text, no toolCalls of its own (a tool-calling step's own assistant
 * message carries toolCalls; this deliberately skips those, since a
 * mid-turn tool call is not the sentence actually shown to the user).
 * Returns null if either half can't be found — an open/still-in-progress
 * turn, or a toolCallId that's stale/wrong.
 */
function findReplyAfterToolCall(conversationId, toolCallId) {
  const messages = getMessages(conversationId); // already ordered oldest-first
  const callIndex = messages.findIndex((m) => m.role === 'tool' && Array.isArray(m.toolResults) && m.toolResults.some((r) => r.id === toolCallId));
  if (callIndex === -1) return null;
  for (let i = callIndex + 1; i < messages.length; i++) {
    const m = messages[i];
    if (m.role === 'assistant' && m.text && !Array.isArray(m.toolCalls)) return m;
  }
  return null;
}

/**
 * The real check. `snapshotId`/`toolCallId` identify which check_myself
 * call to check; `fieldName` is one of the dot/bracket paths
 * self-model.js's extractCitableFields() produces (also resolvable
 * straight off a fresh snapshot read via getByPath() — never trusts
 * self-store.js's own stored `field_value`, always re-derives it here).
 *
 * Returns one of three verdicts, never a fourth silently-inferred one:
 *   'used'         — the field's value (or its natural percentage form)
 *                     appears verbatim in the real reply that followed.
 *   'ignored'      — the snapshot genuinely had this field, a real reply
 *                     exists, but the value never appears in it.
 *   'unverifiable' — the honest outcome whenever a real yes/no can't be
 *                     produced: the snapshot/field doesn't exist, the
 *                     field isn't a plain number (free text/enum — the
 *                     class of claim this file was told not to attempt),
 *                     or no final reply exists yet to check against.
 */
export function verifyCitation(snapshotId, toolCallId, fieldName) {
  const snapshot = getSelfModelSnapshot(snapshotId);
  if (!snapshot) return { verdict: 'unverifiable', reason: 'No snapshot on record with this id.' };

  const value = getByPath(snapshot.snapshot, fieldName);
  if (typeof value !== 'number' || !Number.isFinite(value)) {
    return { verdict: 'unverifiable', reason: 'This field is not a plain checkable number — free text and enum values are not machine-verifiable by design.' };
  }

  const reply = findReplyAfterToolCall(snapshot.conversationId, toolCallId);
  if (!reply) {
    return { verdict: 'unverifiable', reason: 'No real reply exists yet after this call to check against.' };
  }

  const forms = candidateStrings(value);
  const matched = forms.some((f) => containsNumberForm(reply.text, f));
  return {
    verdict: matched ? 'used' : 'ignored',
    snapshotId,
    toolCallId,
    fieldName,
    fieldValue: value,
    replyText: reply.text,
  };
}

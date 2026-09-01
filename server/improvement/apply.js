// Applies and undoes self-improvement changes — rules and settings ONLY.
//
// THE INVARIANT THIS FILE EXISTS TO PROTECT: this module may never write to
// any file under the repo source tree, and never will — "Jarvis never
// edits its own code" is enforced here structurally, not just documented.
// Everything this file touches is a SQLite row (improvement-store.js) or a
// prefs.js key. A proposal whose kind is 'skill' or 'code' is refused
// outright by applyProposal() below, and improvement-policy.js's decide()
// already never routes either kind to auto-apply in the first place — this
// file is the second, independent gate, not the only one.

import * as store from './improvement-store.js';
import { getPrefs, setPrefs } from '../prefs.js';

/**
 * Prefs keys a self-improvement 'setting' proposal may ever touch — a
 * deliberate allowlist, not "anything in prefs.js." These are genuine
 * behaviour dials (how cautious, how fast, how much background work);
 * anything else in prefs.js (a pinned model id, the TTS provider, whether
 * auto-select itself is on) is structural configuration, not a behaviour
 * rule, and stays out of reach even at improvementTrust:'auto'.
 */
const ALLOWED_SETTING_KEYS = new Set(['balance', 'clarifySensitivity', 'memoryTrust', 'maxBackgroundJobs', 'improvementTrust', 'improvementResearch']);

// Every whitelisted key above is string-valued except this one — a caller
// this far from the actual pref shape (a model-called tool, in particular)
// may reasonably send "3" instead of 3. Coercing here, once, is cheaper and
// safer than trusting every caller to know prefs.js's exact type per key.
const NUMERIC_SETTING_KEYS = new Set(['maxBackgroundJobs']);

function coerceSettingValue(key, value) {
  if (NUMERIC_SETTING_KEYS.has(key)) {
    const n = Number(value);
    if (!Number.isFinite(n)) throw new Error(`"${value}" isn't a valid number for "${key}".`);
    return n;
  }
  return value;
}

function valuesEqual(a, b) {
  return JSON.stringify(a) === JSON.stringify(b);
}

/**
 * Applies a pending 'rule' or 'setting' proposal — called both by the
 * auto-apply path (once improvement-policy.js's decide() says
 * 'auto-apply') and by the screen's manual Approve button for anything a
 * human approves by hand, including a 'skill'/'code' idea the user
 * approves the CONCEPT of (which still never writes code here — see
 * implementation-prompt.js for what actually happens to those).
 *
 * Returns the created rule/updated pref value. Throws for any kind other
 * than 'rule'/'setting' — this function is never the path a Skill or code
 * change takes, regardless of who calls it.
 */
export function applyProposal(proposalId) {
  const proposal = store.getProposal(proposalId);
  if (!proposal) throw new Error('That suggestion no longer exists.');
  if (proposal.status !== 'pending') throw new Error(`That suggestion is already ${proposal.status}.`);

  if (proposal.kind === 'rule') {
    const text = String(proposal.payload?.text || '').trim();
    const scope = proposal.payload?.scope || 'general';
    if (!text) throw new Error('This proposal has no rule text to apply.');
    const rule = store.createRule({ text, scope, sourceProposalId: proposal.id });
    store.recordChange({
      kind: 'rule',
      target: rule.id,
      before: null,
      after: { text: rule.text, scope: rule.scope, active: rule.active },
      reason: proposal.rationale || 'Applied from a self-improvement suggestion.',
      proposalId: proposal.id,
    });
    store.setProposalStatus(proposal.id, 'applied');
    return rule;
  }

  if (proposal.kind === 'setting') {
    const key = proposal.payload?.key;
    if (!ALLOWED_SETTING_KEYS.has(key)) {
      throw new Error(`"${key}" isn't a setting self-improvement is allowed to change.`);
    }
    const value = coerceSettingValue(key, proposal.payload?.value);
    // Snapshot `before` and write `after` with NO await between them — the
    // one real hazard for a synchronous store like this: an interleaved
    // POST /api/prefs from the browser landing between a read and a write
    // would otherwise get silently clobbered. getPrefs()/setPrefs() are
    // both fully synchronous (store.js's readJson/writeJson), so this
    // block genuinely cannot yield to anything else mid-way.
    const before = getPrefs()[key];
    setPrefs({ [key]: value });
    store.recordChange({
      kind: 'setting',
      target: key,
      before,
      after: value,
      reason: proposal.rationale || 'Applied from a self-improvement suggestion.',
      proposalId: proposal.id,
    });
    store.setProposalStatus(proposal.id, 'applied');
    return { key, value };
  }

  throw new Error(`Self-improvement never applies a "${proposal.kind}" change directly — that always goes through the user first.`);
}

function currentValueFor(change) {
  if (change.kind === 'rule') {
    const rule = store.getRule(change.target);
    return rule ? { text: rule.text, scope: rule.scope, active: rule.active } : null;
  }
  if (change.kind === 'setting') {
    return getPrefs()[change.target];
  }
  return undefined; // 'undo' rows are never undoable — see undoChange() below
}

/** True only for a 'rule' change whose target no longer exists at all (hard-deleted from the archived view) — a genuinely different situation from "the user edited it since," which undoChange() must not conflate with the ordinary changed_since refusal (that one at least offers a real force option; this one has nothing left to restore). */
function targetWasDeleted(change) {
  return change.kind === 'rule' && !store.getRule(change.target);
}

/**
 * Undoes ONE applied change. Refuses instead of clobbering when the live
 * value no longer matches what this change last set (`after`) — the user
 * changed it themselves since, on the Settings screen or the
 * Self-Improvement screen's own rule text, and a blind restore would
 * silently overwrite that later, deliberate decision. Pass `force: true`
 * only after the caller has explicitly shown the user the mismatch and
 * they confirmed anyway.
 *
 * An 'undo' row (a change produced by a PREVIOUS call to this function) is
 * never itself undoable — re-applying something previously undone means
 * approving a fresh proposal, not "undoing an undo." This keeps the log
 * unambiguous: every row's `before`/`after` describes exactly what
 * happened at that moment, with no chain of reversals to reason through.
 *
 * Returns `{ok:true, restoredTo}` on success, `{ok:false, reason:
 * 'changed_since', expected, current}` when refusing because the live
 * value has moved on, or `{ok:false, reason:'target_deleted'}` when the
 * rule this change was about has since been hard-deleted entirely — a
 * genuinely different situation (nothing is left to restore, `force`
 * would silently no-op) that the screen must show differently.
 */
export function undoChange(changeId, { force = false } = {}) {
  const change = store.getChange(changeId);
  if (!change) throw new Error('That change no longer exists.');
  if (change.undoneAt) throw new Error('That change was already undone.');
  if (change.kind === 'undo') throw new Error("An undo can't itself be undone — approve a fresh suggestion to bring something back instead.");

  if (targetWasDeleted(change)) {
    return { ok: false, reason: 'target_deleted' };
  }

  const current = currentValueFor(change);
  if (!force && !valuesEqual(current, change.after)) {
    return { ok: false, reason: 'changed_since', expected: change.after, current };
  }

  let restoredTo;
  if (change.kind === 'rule') {
    // The only kind of rule change apply.js writes with before:null is a
    // CREATION — undoing one means deactivating it, never a hard delete,
    // so its own history stays intact and answerable (same choice
    // memory-store.js's mergeMemories() makes: archive, never delete).
    // A non-null `before` means an EDIT (server.js's rule-edit route
    // records one via improvement-store.js's updateRuleText()) — undoing
    // THAT means putting the wording back, not just flipping active.
    if (change.before == null) {
      store.setRuleActive(change.target, false);
      restoredTo = { ...currentValueFor(change) };
    } else {
      const before = change.before;
      store.updateRuleText(change.target, before.text);
      if (typeof before.active === 'boolean') store.setRuleActive(change.target, before.active);
      restoredTo = { ...currentValueFor(change) };
    }
  } else if (change.kind === 'setting') {
    setPrefs({ [change.target]: change.before });
    restoredTo = change.before;
  } else {
    throw new Error(`Don't know how to undo a "${change.kind}" change.`);
  }

  store.markChangeUndone(changeId);
  store.recordChange({
    kind: 'undo',
    target: change.target,
    before: change.after,
    after: restoredTo,
    reason: `Undo of an earlier ${change.kind} change.`,
    proposalId: change.proposalId,
  });

  return { ok: true, restoredTo };
}

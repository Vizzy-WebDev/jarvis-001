// The single decision point for "does this proposal apply itself, or does
// it wait for the user" — the same seam design as memory/memory-policy.js's
// decide(), and for the identical reason: every caller (apply.js, the
// screen's approve button, a conversational suggest_improvement call) asks
// THIS function, none of them hardcode "always ask" or "just do it"
// themselves.
//
// Deliberately a PURE function — no db.js import, no side effects. Testable
// with a one-off `node -e` truth table, no server or database needed (see
// root CLAUDE.md's "No automated test suite" section).

import { getPrefs } from '../prefs.js';

/**
 * `improvementTrust` levels — mirrors memory-policy.js's THRESHOLDS shape,
 * but self-improvement has no confidence-score dial the way Memory does:
 * the hard floors below already do the real gating, so trust level here
 * only ever widens or narrows what counts as "enough evidence," never
 * bypasses a floor. 'ask' (default) requires every proposal, regardless of
 * evidence, to wait — reproduces "nothing auto-applies" byte for byte for
 * anyone who never opens the Self-Improvement screen.
 */
export const MIN_EVIDENCE_BY_TRUST = { ask: Infinity, balanced: 2, auto: 1 };

/**
 * `proposal` needs at least `kind`, `sourceTier`, and `evidence` (an array
 * — length is what's checked, not its contents). `trust` overrides the
 * user's saved preference — passed by tests; every real caller omits it.
 *
 * Returns 'auto-apply' or 'require-approval'. Hard floors that NO trust
 * level overrides, per the user's own settled requirements:
 *   - kind must be 'rule' or 'setting' — a Skill or a code change always
 *     asks first, full stop, regardless of evidence or trust.
 *   - sourceTier must be 1 (the proposal's own evidence is Jarvis's own
 *     task history) — anything sourced from docs/communities/web always
 *     asks, since it's something Jarvis READ, not something it directly
 *     observed about its own performance.
 *   - conflictWith must be unset — a proposal that contradicts an existing
 *     rule always needs a human decision, never resolved silently (same
 *     floor memory-policy.js already enforces for a conflicting candidate).
 * Evidence count is the damping floor beneath trust level: fewer than
 * MIN_EVIDENCE_BY_TRUST[level] distinct outcomes behind a proposal always
 * requires approval too — this is what stops a single job failure from
 * minting a permanent rule, and what stops the intended learn -> apply ->
 * learn loop from oscillating.
 */
export function decide(proposal = {}, { trust } = {}) {
  if (proposal.kind !== 'rule' && proposal.kind !== 'setting') return 'require-approval';
  if (proposal.conflictWith) return 'require-approval';
  if (Number(proposal.sourceTier) !== 1) return 'require-approval';

  const level = MIN_EVIDENCE_BY_TRUST[trust] !== undefined ? trust : getPrefs().improvementTrust;
  const minEvidence = MIN_EVIDENCE_BY_TRUST[level] !== undefined ? MIN_EVIDENCE_BY_TRUST[level] : MIN_EVIDENCE_BY_TRUST.ask;

  const evidenceCount = Array.isArray(proposal.evidence) ? proposal.evidence.length : 0;
  if (evidenceCount < minEvidence) return 'require-approval';

  return 'auto-apply';
}

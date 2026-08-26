// The single decision point for "what happens to a candidate memory" — the
// one seam the trust-level system plugs into without touching
// memory-review.js, the skills, or the review-card UI. Every one of those
// callers asks THIS function what to do; none of them hardcode "always ask
// the user" (or "always auto-save") themselves.
//
// Deliberately a PURE function — no db.js import, no side effects. That's
// what lets the ten-case truth table in this file's own test (see root
// CLAUDE.md's "No automated test suite" section — this is the kind of
// pure-logic module meant to be exercised with a one-off `node -e` script,
// no server or database needed) be exhaustive and instant, and what lets
// memory-review.js resolve a whole batch of candidates without a prefs.js
// read per candidate.

import { getPrefs } from '../prefs.js';

/**
 * The confidence a candidate's score must meet or exceed to auto-save, per
 * trust level. `ask` is `Infinity` on purpose, not just a high number —
 * that's what guarantees the default level reproduces the original
 * approval-first behavior byte for byte, for every score a model could ever
 * return (including a buggy one above 1.0).
 */
export const THRESHOLDS = { ask: Infinity, balanced: 0.85, auto: 0 };

/**
 * `candidate` is whatever a caller has on hand — needs at least
 * `confidence` (0..1, from the extraction model) and, if applicable,
 * `conflictsWithId`. `trust` overrides the user's saved preference — passed
 * by tests so they never have to mutate prefs.json; every real caller in
 * this codebase omits it and gets the user's actual setting.
 *
 * Returns 'require-approval' or 'auto-approve'. Two things ALWAYS require
 * approval, at every trust level, with no override: a candidate that
 * conflicts with an existing memory (resolving a conflict changes or
 * duplicates something that already exists — never done silently), and a
 * candidate with no usable confidence score at all (a caller that can't say
 * how sure it is doesn't get the benefit of the doubt).
 */
export function decide(candidate = {}, { trust } = {}) {
  const level = THRESHOLDS[trust] !== undefined ? trust : getPrefs().memoryTrust;
  const threshold = THRESHOLDS[level] !== undefined ? THRESHOLDS[level] : THRESHOLDS.ask;

  if (candidate.conflictsWithId) return 'require-approval';

  const confidence = Number(candidate.confidence);
  if (!Number.isFinite(confidence)) return 'require-approval';

  return confidence >= threshold ? 'auto-approve' : 'require-approval';
}

// The single decision point for "what happens to a candidate memory" — the
// one seam a future trust-level/approval-policy system plugs into without
// touching memory-review.js, the skills, or the review-card UI. Every one
// of those callers asks THIS function what to do; none of them hardcode
// "always ask the user" themselves.
//
// This build has exactly one policy: always require approval. That's the
// whole point of it living here instead of being inlined — the day a
// configurable trust level is wanted, it's a change to this one function's
// body (and wherever its setting is stored), not a rewrite of everything
// that currently assumes "approval-first" by scattering that assumption
// through their own code.

/**
 * `candidate` is whatever shape a caller has on hand when it needs to
 * decide (a proposed {category, text, source...}, or nothing at all for a
 * blanket policy check) — deliberately loose, since a future policy might
 * key off category, source, or confidence in ways this build never needs
 * to. Returns one of: 'require-approval' (the only value this build ever
 * returns), and — reserved for later, not implemented — 'auto-approve'.
 */
export function decide(_candidate) {
  return 'require-approval';
}

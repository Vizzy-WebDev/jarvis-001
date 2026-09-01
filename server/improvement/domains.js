// The structural guard behind the user's own explicit rule: the life-pattern
// engine (life-patterns.js) may notice patterns in work, projects, study,
// finances, and travel — never the user's emotional state or relationships.
// Same hybrid design as personality.js's detectFloors(): pure regex, zero
// imports, directly testable with a one-off `node -e` script, no server
// needed. This is the MECHANISM, not a prompt instruction asking the model
// to please not mention feelings — life-patterns.js applies this on BOTH
// sides: matching memories/messages are dropped BEFORE they ever reach a
// model call, and any produced insight that matches is dropped AFTER, so a
// prompt-level instruction is only ever the backstop, never the guarantee.
//
// Deliberately biased broad, not narrow — the opposite asymmetry from
// personality.js's distress/serious-topic floors. There, a false positive
// costs only tone (harmless) and a false negative could miss real distress,
// so those patterns stay narrow. Here, a false NEGATIVE means relationship
// or emotional content leaks into analysis the user explicitly said never
// to run — the actual harm this module exists to prevent. A false positive
// just drops one memory/message from consideration; the life-pattern engine
// has plenty of other material to work with, so over-excluding costs
// nothing structurally. When genuinely unsure whether something is
// relationship/emotional content, this errs toward excluding it.
//
// The one thing broad matching must still not do: fire on a plainly
// unrelated use of a word like "relationship" (e.g. "my relationship with
// this codebase") — every relationship pattern below is anchored to an
// actual personal-relationship noun, never a bare "relationship" match.

const RELATIONSHIP_NOUN = '(girlfriend|boyfriend|wife|husband|spouse|partner|fianc[eé]e?|ex(?:-(?:girlfriend|boyfriend|wife|husband))?|mom|dad|mother|father|parents?|stepmom|stepdad|sister|brother|sibling|siblings|grandma|grandpa|grandmother|grandfather|in-laws?|mother-in-law|father-in-law|kids?|children|son|daughter|best friend|friendship)';

const RELATIONSHIP_PATTERNS = [
  new RegExp(`\\b(my|our)\\s+${RELATIONSHIP_NOUN}\\b`, 'i'),
  new RegExp(`\\brelationship\\s+with\\s+(my|our)\\s+${RELATIONSHIP_NOUN}\\b`, 'i'),
  new RegExp(`\\b${RELATIONSHIP_NOUN}\\s+and\\s+i\\b`, 'i'),
  /\b(break[\s-]?up|broke up|breaking up)\b/i,
  /\bdivorc(e|ing|ed)\b/i,
  /\b(dating|engaged|engagement|propos(e|al|ed)|married|marriage|wedding)\b/i,
  /\bcustody\b/i,
  /\bin a relationship\b/i,
];

// (?:\w+\s+){0,2}? tolerates a short intervening adverb — "feel REALLY
// anxious", "feel so overwhelmed" — the same tolerance personality.js's own
// distress regex needed after a live miss on "feel like SUCH a failure".
const EMOTIONAL_STATE_PATTERNS = [
  /\bi\s+feel(?:ing)?\s+(?:\w+\s+){0,2}?(happy|sad|anxious|depressed|lonely|angry|frustrated|overwhelmed|excited|scared|afraid|worried|stressed|heartbroken|grief|grieving|hurt|hopeless|numb)\b/i,
  /\bi('m| am)\s+(?:\w+\s+){0,2}?(feeling\s+)?(happy|sad|anxious|depressed|lonely|angry|frustrated|overwhelmed|excited|scared|afraid|worried|stressed|heartbroken|grieving|hopeless|numb)\b/i,
  /\b(anxiety|depression|grief|heartbreak|loneliness|panic attack)\b/i,
  /\bi('m| am) in love\b/i,
  /\bmy mental health\b/i,
];

function matchesAny(patterns, text) {
  return patterns.some((re) => re.test(text));
}

/** Pure function: text in, boolean out. True if the text touches emotional state or relationships — the two areas the life-pattern engine must never see or analyse. */
export function isExcludedDomain(text) {
  const t = String(text || '');
  if (!t.trim()) return false;
  return matchesAny(RELATIONSHIP_PATTERNS, t) || matchesAny(EMOTIONAL_STATE_PATTERNS, t);
}

/** Filters an array of {text, ...} items (memories, messages, insights — anything with a `.text`), dropping every one that touches the excluded domains. Used on BOTH the input side (before a life-patterns model call) and the output side (after it, on the model's own produced insights). */
export function filterExcludedDomains(items, textKey = 'text') {
  if (!Array.isArray(items)) return items;
  return items.filter((item) => !isExcludedDomain(item?.[textKey]));
}

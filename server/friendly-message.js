// The single place a raw provider/SDK error becomes ONE clean, user-facing
// sentence — built because none of it existed before: every adapter's
// friendlyError() only de-nests the SDK's JSON envelope to find the
// provider's own error string (see each adapter's own friendlyError()), it
// never rewrites that string into plain language. The raw provider text —
// quota errors with an appended JSON blob, connection internals, provider
// jargon — was reaching the user verbatim in ~20 places: the main
// conversation/voice error path, scheduled-task notifications, and several
// settings screens (Model Settings, Skills, App Control), each doing its own
// `data.error || 'default'` passthrough with no shared cleanup step anywhere.
//
// Reuses models/error-kind.js's classifyError() for the actual
// categorization (quota/auth/no_access/network/other) rather than
// re-inventing it — that function is already exercised by health.js's
// cooldown logic and runner.js's availability tracking, so improving its
// accuracy (see the auth-regex word-order fix made alongside this file)
// benefits those too, not just this new consumer.

import { classifyError } from './models/error-kind.js';

const CATEGORY_MESSAGES = {
  quota: 'This model has hit its usage limit for now — try again later, or switch models in Model Settings.',
  auth: "That API key isn't valid — check it in Model Settings.",
  no_access: "This model isn't available on that account or key — check its permissions, or try a different model in Model Settings.",
  network: "Couldn't reach that model's server — check your connection (or, for a local model, that it's actually running).",
};

/**
 * Turns a raw error into one clean sentence — NEVER the raw provider
 * string, even as a last resort (that's what `fallback` is for). Accepts
 * either a real Error/response object (what classifyError() expects) or a
 * plain string (already-extracted text, e.g. from a caught `err.message` or
 * a JSON API's `data.error`) — a bare string is wrapped as `{message: ...}`
 * first, since classifyError()'s own text-matching only ever looks at
 * `err.message` and friends, not at a string argument directly.
 */
export function friendlyMessage(err, fallback = 'I ran into a problem talking to that model.') {
  const normalized = typeof err === 'string' ? { message: err } : err;
  const kind = classifyError(normalized);
  return CATEGORY_MESSAGES[kind] || fallback;
}

/**
 * Same idea, generalized past "a model" for connectors/skills/other
 * non-model failures — `classifyError()`'s categories (quota/auth/
 * no_access/network) are common enough across API-shaped services that
 * they're worth reusing rather than writing a second classifier, but the
 * sentence itself shouldn't say "that model" for, say, a Notion connector.
 * `subject` is a short noun phrase that reads naturally as the START of a
 * sentence ("That connection", "This skill install", ...) — callers supply
 * it already capitalized/phrased for that position, same discipline as a
 * template string.
 */
export function friendlyMessageFor(err, subject, fallback) {
  const normalized = typeof err === 'string' ? { message: err } : err;
  const kind = classifyError(normalized);
  switch (kind) {
    case 'quota':
      return `${subject} has hit a usage limit for now — try again later.`;
    case 'auth':
      return `${subject} couldn't be verified — check the key or credentials.`;
    case 'no_access':
      return `${subject} doesn't have permission to do that — check its access settings.`;
    case 'network':
      return `${subject} couldn't reach its server — check your connection.`;
    default:
      return fallback || `${subject} ran into a problem.`;
  }
}

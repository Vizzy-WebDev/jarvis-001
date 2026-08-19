// Best-effort classification of an error thrown by an adapter's stream()/
// testConnection() into a coarse "kind" — used by health.js to pick a
// cooldown length (a quota error should recover much sooner than a
// permanently-revoked key) and by runner.js to persist a model's
// `availability` state so the UI can show *why* a model is benched, not just
// that it is. Never throws itself — worst case it returns 'other'.
//
// Error shapes seen in the wild, per adapter (see each adapter's own
// friendlyError() for the human-readable-message half of this same
// problem):
//   - Gemini: err.message is often raw JSON — JSON.parse(err.message).error
//     carries {code, message}. Some SDK errors also set err.status/err.code
//     directly.
//   - Anthropic: err.error.error.message is the nested human message;
//     err.status is a numeric HTTP-status-like field the SDK sets on API
//     errors.
//   - openai-compatible: err.error.message is the nested message; err.status
//     is numeric. A refused local connection wraps as `TypeError: fetch
//     failed` with the real code buried at err.cause.cause.code — walk the
//     .cause chain to find it (mirrors openai-compatible.js's
//     isConnectionRefused()).

const NETWORK_CODES = new Set(['ECONNREFUSED', 'ETIMEDOUT', 'ENOTFOUND']);

function findNetworkCode(err) {
  let current = err;
  for (let i = 0; i < 5 && current; i++) {
    if (NETWORK_CODES.has(current.code)) return true;
    current = current.cause;
  }
  return false;
}

/** Tries to parse err.message as JSON (Gemini's shape) and return the parsed body, or null. */
function parsedBody(err) {
  try {
    const raw = err?.message;
    if (typeof raw !== 'string') return null;
    return JSON.parse(raw);
  } catch {
    return null;
  }
}

function findStatus(err) {
  try {
    if (typeof err?.status === 'number') return err.status;
    if (typeof err?.code === 'number') return err.code;
    const body = parsedBody(err);
    const bodyCode = body?.error?.code;
    if (typeof bodyCode === 'number') return bodyCode;
  } catch {
    // fall through
  }
  return null;
}

function findMessageText(err) {
  try {
    const parts = [];
    if (typeof err?.message === 'string') parts.push(err.message);
    const body = parsedBody(err);
    if (body?.error?.message) parts.push(body.error.message);
    if (err?.error?.error?.message) parts.push(err.error.error.message);
    if (err?.error?.message) parts.push(err.error.message);
    return parts.join(' ');
  } catch {
    return '';
  }
}

// The one place this failure-kind -> availability-badge-state mapping is
// declared. runner.js, ai.js, and server.js all record a model's
// `availability.state` off a classifyError() result and previously each
// carried their own copy of this literal — harmless while byte-identical,
// but nothing enforced that, and a future edit to one copy silently
// drifting from the others would make a model's badge depend on which code
// path happened to classify its error. error-kind.js has zero imports of
// its own (all three call sites already depend on it for classifyError), so
// importing this from here adds no new edges and can't affect the skills
// circular-import invariant (see CLAUDE.md) — this file isn't under
// server/skills/ and has nothing that could create a cycle through it.
export const AVAILABILITY_STATE_FOR_KIND = {
  quota: 'quota',
  auth: 'auth',
  no_access: 'no_access',
  network: 'unreachable',
  other: 'unreachable',
};

/** Classifies an error thrown by an adapter into 'quota' | 'no_access' | 'auth' | 'network' | 'other'. Never throws. */
export function classifyError(err) {
  try {
    if (findNetworkCode(err)) return 'network';

    const status = findStatus(err);
    if (status === 429) return 'quota';
    if (status === 401) return 'auth';
    if (status === 403) return 'no_access';

    const text = findMessageText(err).toLowerCase();
    if (/quota|rate.?limit/.test(text)) return 'quota';
    // Both word orders needed — a real, live gap found while building
    // server/friendly-message.js: Gemini's actual invalid-key text is "API
    // key not valid. Please pass a valid API key.", which the single
    // `invalid.*key` pattern never matches (the words are in the opposite
    // order from what it expects).
    if (/invalid.*key|key.*invalid|key.*not valid|unauthorized|authentication/.test(text)) return 'auth';
    if (/permission|access denied|not enabled|forbidden/.test(text)) return 'no_access';
    // Catches the friendly (already-stringified) messages the manual "Test"
    // route works with, where the raw error/cause chain isn't available —
    // e.g. openai-compatible's friendlyError() text "Couldn't reach that
    // address — is the local server running?".
    if (/couldn.?t reach|connection refused|timed? ?out|not reachable/.test(text)) return 'network';

    return 'other';
  } catch {
    return 'other';
  }
}

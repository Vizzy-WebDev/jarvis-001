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
  // 'other' used to alias straight to 'unreachable' — the same 6h cooldown
  // as a genuinely dead connection, for an error nobody has actually
  // classified. Given its own 'error' state (see router.js's
  // AVAILABILITY_COOLDOWNS_MS) so an unrecognized failure gets a moderate
  // cooldown, not the harshest one, purely because nothing else matched.
  other: 'error',
  // A provider-side overload (503 "high demand", "please try again later")
  // is not the model's fault and typically clears in seconds — confirmed
  // live: a real 503 from gemini-3.7-flash cleared on the very next
  // individual call, seconds later. Before this classification existed it
  // fell through to 'other' -> 'unreachable', a 6-hour ban for a transient
  // condition (see router.js's AVAILABILITY_COOLDOWNS_MS for the short
  // cooldown this state actually gets).
  transient: 'busy',
  // A model that genuinely cannot serve chat at all (wrong/retired model
  // name, a TTS/image/embedding-only model handed a chat request, no
  // endpoints support tool-calling) will fail identically forever — no
  // cooldown will ever fix it. router.js excludes this state from routing
  // outright rather than giving it a cooldown; only a manual Test/Check-all
  // clears it, once the user has actually changed something.
  unsupported: 'unsupported',
};

// Text patterns for a provider overload/transient-unavailability response —
// distinct from a real outage (network) or a permanent rejection (auth/
// no_access/unsupported). Deliberately narrow: only wording that describes
// the PROVIDER'S OWN current state, never generic wording that could also
// describe a permanent problem.
const TRANSIENT_TEXT = /overloaded|high demand|temporarily unavailable|service unavailable|currently unavailable|try again later/;

// Text patterns for a model that can never serve a chat/tool-calling
// request, regardless of retrying — a wrong/retired model name, a
// non-chat model (TTS/image/embedding) handed a chat request, or a
// provider explicitly saying it has no route for this model at all.
const UNSUPPORTED_TEXT = /no endpoints found|no allowed providers|not a valid model|model not found|unknown model|does not support tool use|does not support tools/;

// A 400 is ambiguous — it can mean "this model can never handle any
// request shaped like this" (unsupported) or "this specific request was
// too big/malformed this one time" (context length, too many tokens — a
// per-turn problem, not a per-model one). Only the former should ever get
// classified as 'unsupported'; the context-length case must keep falling
// through to 'other' so it isn't excluded from routing for every future
// turn just because one long turn tripped it.
const INVALID_ARGUMENT_TEXT = /invalid argument|invalid request/;
const CONTEXT_LENGTH_TEXT = /context length|context window|too (many|long) tokens|maximum context|token limit/;

/** Classifies an error thrown by an adapter into 'quota' | 'no_access' | 'auth' | 'network' | 'transient' | 'unsupported' | 'other'. Never throws. */
export function classifyError(err) {
  try {
    if (findNetworkCode(err)) return 'network';

    const status = findStatus(err);
    if (status === 429) return 'quota';
    if (status === 401) return 'auth';
    if (status === 403) return 'no_access';
    if (status === 404) return 'unsupported';
    if (status === 500 || status === 502 || status === 503 || status === 504 || status === 529) return 'transient';

    const text = findMessageText(err).toLowerCase();
    // "usage limit" is friendly-message.js's own rewritten wording for a
    // quota error (see its 'quota' case) — needed because server.js's
    // manual Test path can end up classifying that already-friendlied text
    // when no raw detail is available (see testAndRecord()'s comment).
    if (/quota|rate.?limit|usage limit/.test(text)) return 'quota';
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
    if (TRANSIENT_TEXT.test(text)) return 'transient';
    if (UNSUPPORTED_TEXT.test(text)) return 'unsupported';
    // Live-confirmed shape: gemini-3.1-flash-tts-preview (a TTS-only model
    // handed a chat request) throws exactly {"code":400,"message":"Request
    // contains an invalid argument.","status":"INVALID_ARGUMENT"} from a
    // live conversation turn — but a MANUAL Test/Check-all never sees that
    // raw shape at all: it classifies from the adapter's own already-
    // de-nested friendlyError() text (gemini.js's friendlyError() returns
    // only `parsed.error.message`, discarding `parsed.error.code`), so
    // `status` is always null on that path. Originally gated on
    // `status === 400`, confirmed LIVE (via a real Check-all run against
    // this exact model) to never fire there as a result — the model kept
    // classifying as 'other' and being silently re-tried forever, spending
    // real quota each time on something that will never work. The text
    // pattern alone is specific enough (Gemini only uses this generic
    // "invalid argument" wording for a structurally wrong request, never
    // for an ordinary content problem) — the real safety valve is the
    // context-length exclusion right below, not the status code.
    if (INVALID_ARGUMENT_TEXT.test(text) && !CONTEXT_LENGTH_TEXT.test(text)) return 'unsupported';

    return 'other';
  } catch {
    return 'other';
  }
}

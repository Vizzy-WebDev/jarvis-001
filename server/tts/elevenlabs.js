// ElevenLabs text-to-speech provider — a real, working second provider
// behind tts/index.js's seam (see that file's header comment for the
// contract every provider implements). Its key comes from
// external-services.js — the same generic store Round 2 built for Deepgram
// — never a new hardcoded secret ref.
//
// VERIFIED LIVE against the real endpoint (2026-08-24, the user's own real
// key) before writing any of this — per CLAUDE.md's explicit warning that a
// live API can differ from what's remembered/assumed. Confirmed live:
//   - Auth: `xi-api-key: <key>` header (not Bearer/Authorization).
//   - Key check: GET /v1/user -> 200 + account JSON on a good key, 401 +
//     {detail:{type,code,message,status,request_id}} on a bad one.
//   - Voices: GET /v1/voices -> {voices:[{voice_id, name, ...}, ...]}.
//   - Synthesis: POST /v1/text-to-speech/{voice_id} with
//     {text, model_id} JSON body and `Accept: audio/mpeg` -> 200 + a real
//     playable MP3 body (confirmed via its ID3 header) on success; 400 +
//     the same {detail:{...}} shape on e.g. an invalid voice_id.
//
// Ref recognition — the one genuinely generic-vs-specific tension in this
// whole design: external-services.js lets the user type ANY label
// ("ElevenLabs", "Elevenlab", "11labs", ...), slugified into whatever ref
// that produces — there is no guaranteed exact match between that ref and
// whatever an adapter expects to look itself up under. Rather than smear
// provider-specific name-guessing into the generic seam (tts/index.js) or
// require the user to type one exact spelling, this ADAPTER (where
// provider-specific knowledge already necessarily lives — it's calling
// ElevenLabs' real API either way) exports matchesRef(), a small, bounded,
// self-contained recognizer. tts/index.js just asks every registered
// adapter "is this configured service yours?" — fully generic dispatch,
// zero provider names hardcoded in the shared seam itself.

import * as externalServices from '../external-services.js';

const API_BASE = 'https://api.elevenlabs.io';

// Live-verified during implementation (real free-tier account): a
// hardcoded "standard" voice_id (tried "Rachel", ElevenLabs' own
// long-documented default) is NOT usable — free-tier accounts get 400
// "Free users cannot use library voices via the API. Please upgrade your
// subscription to use this voice." There is no voice_id that's guaranteed
// synthesizable across every account tier. So when no extra field (Voice
// ID) is set, the default is resolved dynamically instead of assumed: the
// FIRST voice in the account's own GET /v1/voices list — a voice this
// specific account already has API access to, by construction, regardless
// of plan. Cached briefly per key so a multi-sentence reply (several
// stream() calls in a row, no explicit voice each time) doesn't refetch
// the list for every sentence.
const DEFAULT_VOICE_CACHE_MS = 5 * 60 * 1000;
const defaultVoiceCache = new Map(); // apiKey -> {voiceId, at}

async function resolveDefaultVoiceId(apiKey) {
  const cached = defaultVoiceCache.get(apiKey);
  if (cached && Date.now() - cached.at < DEFAULT_VOICE_CACHE_MS) return cached.voiceId;

  const res = await fetch(`${API_BASE}/v1/voices`, { headers: { 'xi-api-key': apiKey } });
  if (!res.ok) {
    const err = new Error(parseErrorMessage(await res.text(), res.status));
    err.code = res.status === 401 ? 'NO_API_KEY' : 'ELEVENLABS_ERROR';
    throw err;
  }
  const data = await res.json();
  const voiceId = data?.voices?.[0]?.voice_id;
  if (!voiceId) {
    const err = new Error('This ElevenLabs account has no voices available — add one in your ElevenLabs account, or set a Voice ID on the saved service.');
    err.code = 'ELEVENLABS_ERROR';
    throw err;
  }
  defaultVoiceCache.set(apiKey, { voiceId, at: Date.now() });
  return voiceId;
}

// A plain substring check ("does the ref contain 'elevenlab'?") was tried
// first and genuinely failed live, twice, on two different real typos —
// "elevenlab" (missing the trailing 's') needed the substring check
// widened once already, and "elevenlap" (a 'b'->'p' slip) doesn't contain
// "elevenlab" as a substring AT ALL, so it silently matched nothing —
// the service vanished from both the voice picker and the Test button
// with no error, because nothing was wrong from either's own point of
// view; they just never found each other. Same lesson this project
// learned from the self-echo bug: a heuristic that has already failed
// live more than once needs to close the whole FAILURE CLASS, not get one
// more specific case bolted on — so this is real typo-tolerant matching
// (Levenshtein edit distance), not another literal string added to a list.
const CANONICAL_NAMES = ['elevenlabs', '11labs'];
const MAX_TYPO_DISTANCE = 2; // tolerates ~1-2 character slips/omissions on a 10-character name

function levenshtein(a, b) {
  const dp = Array.from({ length: a.length + 1 }, () => new Array(b.length + 1).fill(0));
  for (let i = 0; i <= a.length; i++) dp[i][0] = i;
  for (let j = 0; j <= b.length; j++) dp[0][j] = j;
  for (let i = 1; i <= a.length; i++) {
    for (let j = 1; j <= b.length; j++) {
      dp[i][j] =
        a[i - 1] === b[j - 1] ? dp[i - 1][j - 1] : 1 + Math.min(dp[i - 1][j], dp[i][j - 1], dp[i - 1][j - 1]);
    }
  }
  return dp[a.length][b.length];
}

function matchesName(normalized, canonical) {
  // Too short to fuzzy-match without real risk of matching something
  // else's ref by coincidence (e.g. a 2-3 character name).
  if (normalized.length < 4) return false;
  if (normalized.includes(canonical) || canonical.includes(normalized)) return true;
  // Lengths too far apart for an edit-distance-of-2 comparison to mean
  // anything (it would either always fail or risk matching unrelated
  // names of a similar length) — skip straight to "not a match".
  if (Math.abs(normalized.length - canonical.length) > MAX_TYPO_DISTANCE + 1) return false;
  return levenshtein(normalized, canonical) <= MAX_TYPO_DISTANCE;
}

/** Recognizes a configured external-service ref as "this is ElevenLabs", tolerating small typos in what the user actually typed — see this file's header comment. */
export function matchesRef(ref) {
  const normalized = String(ref || '').toLowerCase().replace(/[^a-z0-9]/g, '');
  if (!normalized) return false;
  return CANONICAL_NAMES.some((name) => matchesName(normalized, name));
}

/** Finds which currently-configured service (if any) this adapter should read its key/voice from. */
function findRef() {
  const match = externalServices.listServices().find((s) => matchesRef(s.ref));
  return match?.ref || null;
}

export function isConfigured() {
  const ref = findRef();
  return ref ? Boolean(externalServices.getKey(ref)) : false;
}

function parseErrorMessage(body, status) {
  try {
    const parsed = JSON.parse(body);
    if (parsed?.detail?.message) return parsed.detail.message;
    if (typeof parsed?.detail === 'string') return parsed.detail;
  } catch {
    // non-JSON body — fall through to the generic message below
  }
  return status === 401 ? 'ElevenLabs rejected the API key.' : `ElevenLabs rejected the request (${status}).`;
}

/**
 * Yields exactly one `{ buffer, mimeType }` chunk — a full playable MP3.
 * Matches every other provider's async-generator contract (see
 * tts/index.js) even though this call itself isn't streamed; ElevenLabs
 * does have a real streaming endpoint, but server.js's /api/tts route
 * already concatenates every provider's chunks into one response before
 * replying, so a streaming fetch would add complexity with no client-visible
 * benefit today — see tts/index.js's header comment on this being a future
 * enhancement to the ROUTE, not the seam.
 */
export async function* stream(text, { voice } = {}) {
  const ref = findRef();
  const apiKey = ref ? externalServices.getKey(ref) : null;
  if (!apiKey) {
    const err = new Error('No ElevenLabs API key configured.');
    err.code = 'NO_API_KEY';
    throw err;
  }
  const voiceId = voice || (ref && externalServices.getExtraField(ref)) || (await resolveDefaultVoiceId(apiKey));

  const res = await fetch(`${API_BASE}/v1/text-to-speech/${encodeURIComponent(voiceId)}`, {
    method: 'POST',
    headers: { 'xi-api-key': apiKey, 'Content-Type': 'application/json', Accept: 'audio/mpeg' },
    body: JSON.stringify({ text, model_id: 'eleven_multilingual_v2' }),
  });
  if (!res.ok) {
    const message = parseErrorMessage(await res.text(), res.status);
    const err = new Error(message);
    err.code = res.status === 401 ? 'NO_API_KEY' : 'ELEVENLABS_ERROR';
    throw err;
  }

  const buffer = Buffer.from(await res.arrayBuffer());
  yield { buffer, mimeType: 'audio/mpeg' };
}

/**
 * Tests a key LIVE against the real ElevenLabs endpoint — independent of
 * whatever (if anything) is currently saved, same shape as every other
 * provider's testConnection/testKey. GET /v1/user is a real, free
 * (no-synthesis-cost) auth check, same "handshake only, no expensive
 * operation" philosophy as stt/deepgram.js's testKey().
 */
export async function testKey(key) {
  const trimmed = String(key || '').trim();
  if (!trimmed) return { ok: false, error: 'No key provided.' };
  try {
    const res = await fetch(`${API_BASE}/v1/user`, { headers: { 'xi-api-key': trimmed } });
    if (res.ok) return { ok: true };
    return { ok: false, error: parseErrorMessage(await res.text(), res.status) };
  } catch {
    return { ok: false, error: 'Could not reach ElevenLabs.' };
  }
}

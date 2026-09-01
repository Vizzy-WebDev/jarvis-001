// A small, growing registry of KNOWN TTS providers' real connection details
// — endpoint, auth shape, and how to build the request body — baked
// directly into code, the same way elevenlabs.js already knows ElevenLabs'
// own shape. This is what makes "any provider" actually mean "type the
// name, paste the key, Connect, Test" with ZERO extra steps for the user:
// different companies' real APIs genuinely use different endpoints AND
// different body field names (confirmed live: Fish Audio's real API wants
// `reference_id` for the voice, not `voice` — verified against Fish Audio's
// own docs, https://docs.fish.audio/api-reference/endpoint/openapi-v1/text-to-speech),
// so a single one-size-fits-all request shape genuinely cannot work across
// real companies. The fix is to put that per-company knowledge here, once,
// in code — never pushed onto the user as a field to fill in.
//
// Recognized the same typo-tolerant way elevenlabs.js recognizes itself
// (Levenshtein edit distance against a list of real names for that
// provider) — matchesRef() checks every entry below.
//
// Adding a new known provider: one entry in KNOWN_PROVIDERS. No UI change,
// no new route, no new file — this is the whole point.

import * as externalServices from '../external-services.js';

function levenshtein(a, b) {
  const dp = Array.from({ length: a.length + 1 }, () => new Array(b.length + 1).fill(0));
  for (let i = 0; i <= a.length; i++) dp[i][0] = i;
  for (let j = 0; j <= b.length; j++) dp[0][j] = j;
  for (let i = 1; i <= a.length; i++) {
    for (let j = 1; j <= b.length; j++) {
      dp[i][j] = a[i - 1] === b[j - 1] ? dp[i - 1][j - 1] : 1 + Math.min(dp[i - 1][j], dp[i][j - 1], dp[i - 1][j - 1]);
    }
  }
  return dp[a.length][b.length];
}

const MAX_TYPO_DISTANCE = 2;

function matchesName(normalized, canonical) {
  if (normalized.length < 4) return false;
  if (normalized.includes(canonical) || canonical.includes(normalized)) return true;
  if (Math.abs(normalized.length - canonical.length) > MAX_TYPO_DISTANCE + 1) return false;
  return levenshtein(normalized, canonical) <= MAX_TYPO_DISTANCE;
}

function normalize(ref) {
  return String(ref || '').toLowerCase().replace(/[^a-z0-9]/g, '');
}

// One entry per real, known provider. `names` are what a saved service ref
// can typo-tolerantly match against (same idea as elevenlabs.js's
// CANONICAL_NAMES). `buildBody(text, voice)` is the ONE thing that
// genuinely differs company to company — everything else (fetch, auth
// header, error handling) is shared below.
const KNOWN_PROVIDERS = [
  {
    id: 'fish-audio',
    names: ['fishaudio', 'fish'],
    endpoint: 'https://api.fish.audio/v1/tts',
    authHeader: 'Authorization',
    authPrefix: 'Bearer ',
    // Verified against Fish Audio's own API reference: `text` is required;
    // voice selection is `reference_id` (a voice model id), optional — the
    // account's own default voice is used when omitted, mirroring
    // elevenlabs.js's own "don't require a voice up front" behavior.
    buildBody(text, voice) {
      const body = { text };
      if (voice) body.reference_id = voice;
      return body;
    },
  },
];

function findProvider(ref) {
  const normalized = normalize(ref);
  if (!normalized) return null;
  return KNOWN_PROVIDERS.find((p) => p.names.some((name) => matchesName(normalized, name))) || null;
}

export function matchesRef(ref) {
  return Boolean(findProvider(ref));
}

/** True if ANY known provider has a real key saved under some configured service — mirrors elevenlabs.js's isConfigured() shape for the voice-output picker. */
export function isConfigured() {
  return externalServices.listServices().some((s) => findProvider(s.ref) && Boolean(externalServices.getKey(s.ref)));
}

async function callProvider(provider, apiKey, text, voice) {
  return fetch(provider.endpoint, {
    method: 'POST',
    headers: {
      [provider.authHeader]: `${provider.authPrefix}${apiKey}`,
      'Content-Type': 'application/json',
      Accept: 'audio/mpeg, audio/wav, audio/*;q=0.9, */*;q=0.5',
    },
    body: JSON.stringify(provider.buildBody(text, voice)),
  });
}

/** `ref` is required in practice — tts/index.js always passes the resolved ref through. */
export async function* stream(text, { voice, ref } = {}) {
  const provider = findProvider(ref);
  const apiKey = provider ? externalServices.getKey(ref) : null;
  if (!provider || !apiKey) {
    const err = new Error('No API key is set up for that voice yet.');
    err.code = 'NO_API_KEY';
    throw err;
  }
  const resolvedVoice = voice || externalServices.getExtraField(ref) || undefined;
  const res = await callProvider(provider, apiKey, text, resolvedVoice);
  if (!res.ok) {
    let detail = '';
    try {
      detail = (await res.text()).slice(0, 300);
    } catch {
      // no body to read — fall through with an empty detail
    }
    const err = new Error(detail ? `${provider.id} rejected the request (${res.status}): ${detail}` : `${provider.id} rejected the request (${res.status}).`);
    err.code = res.status === 401 || res.status === 403 ? 'NO_API_KEY' : 'GENERIC_TTS_ERROR';
    throw err;
  }
  const buffer = Buffer.from(await res.arrayBuffer());
  if (!buffer.length) throw new Error(`${provider.id} returned no audio.`);
  yield { buffer, mimeType: res.headers.get('content-type') || 'audio/mpeg' };
}

/** A real, live "does this key work" check — same request shape as stream(), a short fixed phrase, since none of these known providers has a cheaper standalone "check the key" call the way ElevenLabs' own /v1/user does. */
export async function testKey(key, ref) {
  const provider = findProvider(ref);
  if (!provider) return { ok: false, error: 'This isn\'t a recognized voice provider yet.' };
  const trimmedKey = String(key || '').trim();
  if (!trimmedKey) return { ok: false, error: 'No key provided.' };
  try {
    const res = await callProvider(provider, trimmedKey, 'Testing.', externalServices.getExtraField(ref) || undefined);
    if (res.ok) return { ok: true };
    const detail = (await res.text().catch(() => '')).slice(0, 200);
    return { ok: false, error: detail ? `Rejected (${res.status}): ${detail}` : `Rejected (${res.status}).` };
  } catch {
    return { ok: false, error: `Could not reach ${provider.id}.` };
  }
}

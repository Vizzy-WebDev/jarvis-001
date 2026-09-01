// Strips secrets out of raw provider error text before it's allowed to leave
// the server. A raw adapter error (server/models/registry.js's new `detail`
// field, surfaced to the user as "Technical details" on a failed test) can
// echo back whatever was actually sent — including a key the user just
// typed into the Add-a-model form. Leaf module, no imports, so it can be
// called from anywhere without risking the tools/ circular-import invariant
// (see CLAUDE.md).

// A bearer-style token embedded in header/body text — `Bearer sk-...`,
// `Authorization: sk-...`, or a bare `sk-`/`sk-ant-`/`AIza`-prefixed run of
// token characters wherever it appears. Provider key prefixes differ
// (OpenAI's `sk-`, Anthropic's `sk-ant-`, Google's `AIza...`) but a
// self-hosted gateway's key has no fixed shape at all — that's what the
// explicit `secrets` list below is for; this regex only catches the
// well-known first-party shapes as a second line of defense.
const KNOWN_KEY_SHAPE = /\b(sk-[a-zA-Z0-9_-]{10,}|AIza[a-zA-Z0-9_-]{10,})\b/g;

/**
 * Replaces every occurrence of each string in `secrets` (typically the one
 * key the user just submitted, whether or not the connection is saved yet)
 * plus anything matching a known provider key shape, with `••••`. Safe to
 * call on `undefined`/`''` — returns it unchanged. Never throws.
 */
export function redactSecrets(text, secrets = []) {
  if (!text || typeof text !== 'string') return text;
  let out = text;
  for (const secret of secrets) {
    if (!secret || typeof secret !== 'string' || secret.length < 4) continue;
    out = out.split(secret).join('••••');
  }
  out = out.replace(KNOWN_KEY_SHAPE, '••••');
  return out;
}

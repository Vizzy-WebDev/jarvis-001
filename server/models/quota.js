// Best-effort "how much free quota is left on this connection" — used only
// to show a real cost estimate before "Check all models" spends it (see
// server.js's /api/models/recheck/preview and CLAUDE.md's Model system
// section). Never throws, never blocks a recheck if it's wrong or the
// provider can't answer — this is advisory information for the user, not a
// gate on anything.
//
// A leaf module on purpose: config.js only, no import of registry.js/
// connections.js. It exists specifically to be called from server.js
// alongside those, not to become part of the routing/registry graph.

import { getSecret } from '../config.js';

// OpenRouter's own key-status endpoint — confirmed live against both of the
// user's real keys during this investigation: returns `is_free_tier`,
// `usage`, and (when set) `limit_remaining`. There is no per-day counter in
// this response; OpenRouter's free tier is a flat "50 requests/day" (20 if
// you've never added credit) that isn't exposed as a number anywhere in
// this API, so `remaining` is left null here — `isFreeTier: true` alone is
// enough for the UI to show a real warning ("free-tier account, has a daily
// limit") without inventing a precise count this endpoint doesn't give.
async function openRouterKeyStatus(secret) {
  const res = await fetch('https://openrouter.ai/api/v1/key', {
    headers: { Authorization: `Bearer ${secret}` },
  });
  if (!res.ok) return null;
  const body = await res.json().catch(() => null);
  const data = body?.data;
  if (!data) return null;
  return {
    isFreeTier: Boolean(data.is_free_tier),
    remaining: typeof data.limit_remaining === 'number' ? data.limit_remaining : null,
  };
}

/**
 * Best-effort quota status for one connection, or null when this provider
 * has no known way to report it (e.g. Gemini/Anthropic have no equivalent
 * lightweight "how much is left" endpoint) or the lookup itself failed.
 * Never throws.
 */
export async function quotaStatusFor(connection) {
  try {
    if (connection?.adapter === 'openai-compatible' && /openrouter\.ai/i.test(connection.baseUrl || '')) {
      const secret = connection.secretRef ? getSecret(connection.secretRef) : null;
      if (!secret) return null;
      return await openRouterKeyStatus(secret);
    }
  } catch (err) {
    console.error(`[models] quota lookup failed for connection "${connection?.id}":`, err);
  }
  return null;
}

// Finds a usable Gemini API key, wherever the user happens to have put it.
//
// Why this exists: three parts of Jarvis always talk to Gemini directly,
// independent of which model is driving the conversation — text-to-speech
// (tts.js), turn detection (turn-check.js), and the media/research paths
// added for Content Analysis. All of them used to read GEMINI_API_KEY
// straight out of .env.
//
// That silently broke once "connections" arrived. A Gemini key added through
// the Model Settings screen is saved as a *connection secret*
// (JARVIS_SECRET_CONN_GEMINI), not as GEMINI_API_KEY — so a user with a
// perfectly good, working Gemini model configured still had no voice output
// and no turn detection, with no error anywhere explaining why.
//
// So: prefer the legacy env var if it's there (nothing changes for anyone
// who set it up the old way), otherwise fall back to the secret behind any
// Gemini connection they've added since.
//
// A leaf module — imports only config.js and the connections store, never
// the registry, the skills loader, or the runner (see CLAUDE.md's
// circular-import invariant), so any of those layers can safely import it.

import { getProviderKey, getSecret } from './config.js';
import { listConnections } from './models/connections.js';

/** A Gemini API key from the legacy env var, or from the first Gemini connection that has one. Null if there's no Gemini key anywhere. */
export function getGeminiKey() {
  const legacy = getProviderKey('gemini');
  if (legacy) return legacy;

  for (const conn of listConnections()) {
    if (conn.adapter !== 'gemini' || !conn.secretRef) continue;
    const key = getSecret(conn.secretRef);
    if (key) return key;
  }
  return null;
}

/** True if anything Gemini-backed (voice, turn detection, video understanding) can run at all. */
export function hasGeminiKey() {
  return Boolean(getGeminiKey());
}

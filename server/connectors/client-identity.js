// Jarvis's own OAuth client identity — the ONE place that decides what Jarvis
// calls itself (client_name), what redirect URIs it uses, and what it hands an
// authorization server as `client_id` when neither pre-registered nor manually
// -supplied credentials exist. Shared by every registration mechanism (the
// Client ID Metadata Document served below, the Dynamic Client Registration
// body oauth.js posts, and the redirect URI both flows and the manual-entry
// form use) so the document an authorization server fetches and the body DCR
// posts can never drift apart — see CLAUDE.md's connector-catalog rebuild for
// why keeping mechanisms as genuine peers, not copies of each other's
// constants, mattered enough to be called out as its own lesson.
//
// CIMD (Client ID Metadata Document, the current MCP spec's PRIMARY client
// registration mechanism — DCR is spec-deprecated, kept only for servers that
// don't support this yet) means `client_id` IS an https URL that the
// AUTHORIZATION SERVER fetches over the real internet. Jarvis binds to
// 127.0.0.1 only (see CLAUDE.md's "Run it") — a remote server can never reach
// a document Jarvis serves locally. So CIMD is only usable when the user has
// configured a real, publicly-reachable https address for this Jarvis (a
// tunnel, a hosted proxy, etc.) via `setPublicBaseUrl()`. Absent that, `cimdUrl()`
// returns null and oauth.js's obtainClientCredentials() falls through to DCR
// exactly as it always has — this is a pure addition, never a regression for
// an install with no public address.

import { readJson, writeJson } from '../store.js';

const FILE = 'oauth-client'; // data/oauth-client.json
export const CLIENT_METADATA_PATH = '/api/connectors/oauth/client-metadata.json';

// One-line switch: if this project ever ships a hosted metadata document
// (e.g. published alongside a future installer), setting this turns CIMD on
// for every install with zero user setup. Left null deliberately — no such
// document exists yet, and claiming one does without hosting it would be the
// same kind of unverified claim CLAUDE.md's "catalog honesty rule" forbids.
const BUNDLED_CIMD_URL = null;

/** The redirect URI every OAuth flow (CIMD, DCR, pre-registered/manual alike) uses. Moved here from oauth.js so every mechanism reads the exact same value; oauth.js re-exports this so nothing importing it needs to change. */
export function redirectUri() {
  const port = process.env.PORT ? Number(process.env.PORT) : 3000;
  return `http://127.0.0.1:${port}/api/connectors/oauth/callback`;
}

export function getPublicBaseUrl() {
  return readJson(FILE, {}).publicBaseUrl || process.env.JARVIS_PUBLIC_URL || null;
}

export function setPublicBaseUrl(url) {
  writeJson(FILE, { ...readJson(FILE, {}), publicBaseUrl: url || null });
}

/** The URL to advertise as client_id, or null when Jarvis has no publicly reachable document to point to. Must be https with a real path component per the spec — a bare origin isn't a valid CIMD client_id. */
export function cimdUrl() {
  const base = getPublicBaseUrl() || BUNDLED_CIMD_URL;
  if (!base) return null;
  try {
    const u = new URL(CLIENT_METADATA_PATH, base);
    return u.protocol === 'https:' ? u.toString() : null;
  } catch {
    return null;
  }
}

/**
 * The document itself — required members per the MCP spec's Client ID
 * Metadata Document section: client_id, client_name, redirect_uris. Both the
 * loopback and localhost forms are listed, matching the spec's own example
 * (`http://127.0.0.1:3000/callback`, `http://localhost:3000/callback`) —
 * loopback redirect URIs are explicitly fine for a CIMD client; only the
 * document itself must be https-hosted.
 */
export function clientMetadataDocument(clientIdUrl) {
  const loopback = redirectUri();
  return {
    client_id: clientIdUrl,
    client_name: 'Jarvis',
    redirect_uris: [loopback, loopback.replace('//127.0.0.1:', '//localhost:')],
    grant_types: ['authorization_code', 'refresh_token'],
    response_types: ['code'],
    token_endpoint_auth_method: 'none',
    application_type: 'native',
  };
}

/** The RFC 7591 Dynamic Client Registration body — the same identity, minus the self-referencing client_id a DCR POST doesn't send. */
export function clientRegistrationBody() {
  const { client_id, ...rest } = clientMetadataDocument(null);
  return rest;
}

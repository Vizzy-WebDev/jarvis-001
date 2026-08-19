// One Client ID/Secret per catalog entry, registered ONCE by the user and
// then shared by every connector that catalog entry ever creates for this
// Jarvis install — the closest a single-user, no-backend install can get to
// what a real product does when it registers one OAuth client centrally for
// every user (Claude's own Gmail/Drive connectors work this way; Anthropic
// did that registration once, invisibly, and no individual Claude user ever
// sees a Client ID field for them). Google's/GitHub's/Slack's real OAuth
// servers don't support automatic registration (verified live —
// no `registration_endpoint`), so SOME registration has to happen somewhere;
// this is what makes it happen exactly once per catalog entry instead of
// once per connector.
//
// Same minimal leaf-module pattern as client-identity.js: readJson/writeJson
// from store.js, dependency-free, nothing here imports skills/index.js or
// any of the forbidden circular-import targets (see root CLAUDE.md's
// Gotchas). The Client ID (not secret) lives in data/catalog-credentials.json
// — git-ignored, same as everything else under data/ — never in the bundled,
// hand-verified catalog.json. The Client SECRET never touches this file at
// all; it goes through the same saveSecret()/getSecret() every other
// credential in this codebase already uses, under a ref namespaced to the
// catalog entry (`catalogclient_<catalogId>`), so it lands in .env, not JSON.

import { readJson, writeJson } from '../store.js';
import { saveSecret, getSecret, deleteSecret } from '../config.js';

const FILE = 'catalog-credentials'; // data/catalog-credentials.json

function secretRef(catalogId) {
  return `catalogclient_${catalogId}`;
}

/** The registered Client ID for a catalog entry, or null if none has been saved yet. Never returns the secret — callers needing it call getCatalogClientSecret() separately. */
export function getCatalogClient(catalogId) {
  const clientId = readJson(FILE, {})[catalogId]?.clientId;
  return clientId ? { clientId } : null;
}

/** The registered Client Secret for a catalog entry, or null. */
export function getCatalogClientSecret(catalogId) {
  return getSecret(secretRef(catalogId));
}

/** Registers (or replaces) the Client ID/Secret for one catalog entry — called once by the user, through the same guided-setup/manual-Client-ID modal that already exists, never a new screen. */
export function saveCatalogClient(catalogId, clientId, clientSecret) {
  const all = readJson(FILE, {});
  writeJson(FILE, { ...all, [catalogId]: { clientId } });
  if (clientSecret) saveSecret(secretRef(catalogId), clientSecret);
}

/** Clears a catalog entry's registered client — for the day a Google Cloud project (or similar) gets deleted or rotated and the user wants to register a fresh one. */
export function clearCatalogClient(catalogId) {
  const all = readJson(FILE, {});
  delete all[catalogId];
  writeJson(FILE, all);
  deleteSecret(secretRef(catalogId));
}

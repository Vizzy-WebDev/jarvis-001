// Generic, user-named external service key storage — replaces the earlier
// hardcoded-to-Deepgram version (server.js's old EXTERNAL_SERVICE_REFS Set +
// a single `deepgram-*` DOM row). A user can add ANY service by typing its
// own name; there is no fixed list here to extend for a new one.
//
// Still deliberately separate from model-provider keys: models/registry.js's
// own connection secrets (secretRef values like 'conn_...') are managed
// entirely through /api/models's own routes, never through here, so a bug
// here can't corrupt an existing model connection. This module is for
// standalone, non-model services only (Deepgram today; a paid TTS provider
// if one is ever added).
//
// Storage split, matching the project's existing convention: non-sensitive
// STRUCTURE (a service's ref/label/optional-second-field label) lives in
// plain JSON (data/external-services.json, via store.js — git-ignored but
// not the dedicated secrets store); every actual credential value — the
// primary key AND an optional second field's value — goes through
// config.js's existing getSecret/saveSecret/deleteSecret exactly as before,
// completely unchanged. "One active key per service" falls out of that for
// free: saveSecret(ref, value) already unconditionally overwrites a single
// env var; there was never anywhere multiple keys per ref could stack up.

import { readJson, writeJson } from './store.js';
import { getSecret, saveSecret, deleteSecret } from './config.js';

const FILE = 'external-services';

function slugify(label) {
  return String(label || '')
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '');
}

function extraRef(ref) {
  return `${ref}_extra`;
}

function readRegistry() {
  const registry = readJson(FILE, {});
  // One-time, idempotent migration: the earlier hardcoded system already
  // saved a real Deepgram key via saveSecret('deepgram', ...) with no
  // registry entry to go with it (the registry itself is new). Without
  // this, an already-configured Deepgram key would keep working (the
  // secret itself is untouched) but silently vanish from the list until
  // manually re-added under the exact same name. Runs on every read but is
  // a no-op the instant a real registry entry exists.
  if (!registry.deepgram && getSecret('deepgram')) {
    registry.deepgram = { label: 'Deepgram', extraFieldLabel: null };
    writeJson(FILE, registry);
  }
  return registry;
}

/** Every known service, with its live connected status — what the UI renders each row from. */
export function listServices() {
  const registry = readRegistry();
  return Object.entries(registry).map(([ref, entry]) => ({
    ref,
    label: entry.label,
    configured: Boolean(getSecret(ref)),
    extraFieldLabel: entry.extraFieldLabel || null,
    extraFieldConfigured: entry.extraFieldLabel ? Boolean(getSecret(extraRef(ref))) : false,
  }));
}

export function getService(ref) {
  return listServices().find((s) => s.ref === ref) || null;
}

/**
 * Adds a brand-new service (no existing ref) or updates an existing one's
 * key/extra-field (same ref, re-submitted). `extraFieldLabel`/
 * `extraFieldValue` are taken as given on every call — the caller (the UI)
 * is responsible for re-sending what should stay, there is no hidden
 * merge-with-the-previous-value logic here.
 *
 * `allowUpdate` (default true) distinguishes the two real call sites:
 * server.js's "update an existing row" route always passes `label:
 * existing.label`, so it can never collide with itself — that route wants
 * the default (silently rotate the key, the whole point of "one active key
 * per service": no error, no stockpiling, just overwrite). server.js's
 * "add a brand-new service" route passes `allowUpdate: false` — found live
 * that WITHOUT this, typing the exact same name as an already-connected
 * service through the ADD-NEW form silently overwrote that service's key
 * with no error and no indication anything happened, a real, confirmed gap
 * (a second key for an "already-connected" service should be a clear
 * error, not a silent, unannounced overwrite through the wrong form — the
 * row's OWN Save button is what a real update is for). With
 * `allowUpdate: false`, ANY existing ref match — exact label or not — is
 * rejected with a clear, actionable error, never silently treated as an
 * update.
 */
export function addOrUpdateService({ label, key, extraFieldLabel, extraFieldValue, allowUpdate = true }) {
  const trimmedLabel = String(label || '').trim();
  if (!trimmedLabel) throw new Error('A service name is required.');
  const trimmedKey = String(key || '').trim();
  if (!trimmedKey) throw new Error('A key is required.');

  const ref = slugify(trimmedLabel);
  if (!ref) throw new Error('That name doesn’t produce a usable service id — try including a letter or number.');

  const registry = readRegistry();
  const existing = registry[ref];
  if (existing && !allowUpdate) {
    throw new Error(
      `"${existing.label}" is already connected. Use its own Test/Remove controls to change its key, or choose a different name for a genuinely new service.`
    );
  }
  if (existing && existing.label.toLowerCase() !== trimmedLabel.toLowerCase()) {
    throw new Error(
      `A service named "${existing.label}" already exists with a very similar name. Choose a different name, or update that one instead.`
    );
  }

  const trimmedExtraLabel = String(extraFieldLabel || '').trim();
  registry[ref] = { label: trimmedLabel, extraFieldLabel: trimmedExtraLabel || null };
  writeJson(FILE, registry);

  saveSecret(ref, trimmedKey);
  if (trimmedExtraLabel) {
    const trimmedExtraValue = String(extraFieldValue || '').trim();
    if (trimmedExtraValue) saveSecret(extraRef(ref), trimmedExtraValue);
    else deleteSecret(extraRef(ref));
  } else {
    // The row no longer declares a second field at all — nothing orphaned
    // left behind under it.
    deleteSecret(extraRef(ref));
  }

  return getService(ref);
}

/** Clears a service's key(s) but keeps the row — it reverts to "not connected" under the same name, ready for a new key. */
export function removeServiceKey(ref) {
  if (!ref) return;
  deleteSecret(ref);
  deleteSecret(extraRef(ref));
}

/** Deletes the row entirely — name and all — not just its key. */
export function deleteService(ref) {
  if (!ref) return;
  deleteSecret(ref);
  deleteSecret(extraRef(ref));
  const registry = readRegistry();
  if (registry[ref]) {
    delete registry[ref];
    writeJson(FILE, registry);
  }
}

/** For whatever future provider code needs to actually use a saved key. */
export function getKey(ref) {
  return getSecret(ref);
}

export function getExtraField(ref) {
  return getSecret(extraRef(ref));
}

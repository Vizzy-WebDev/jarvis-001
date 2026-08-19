// A "connection" is one saved address+key (or key-only, for a cloud host)
// that can host multiple models — created together via the registry's
// combined test/discover/add flow (registry.js's createConnectionWithModels)
// so a shared key isn't duplicated once per model. Split from registry.js
// the same way task-store.js is split from scheduler.js: this file is a
// leaf, plain CRUD + secret lifecycle, no app logic layered on top.

import { readJson, writeJson } from '../store.js';
import { saveSecret, deleteSecret } from '../config.js';

const FILE = 'connections';

// Deleting a connection whose secretRef is one of these must NOT delete the
// underlying secret — GEMINI_API_KEY etc. are also what server/tts.js uses
// for voice output regardless of which model is chatting, so removing a
// migrated "Gemini" connection must never silently break Jarvis's voice.
export const LEGACY_SECRET_REFS = new Set(['gemini', 'anthropic', 'openai']);

function load() {
  return readJson(FILE, { connections: [] });
}

function save(data) {
  writeJson(FILE, data);
}

export function listConnections() {
  return load().connections;
}

export function getConnection(id) {
  return load().connections.find((c) => c.id === id) || null;
}

function makeId(seed) {
  const base =
    String(seed || 'connection')
      .toLowerCase()
      .trim()
      .replace(/[^a-z0-9]+/g, '-')
      .replace(/(^-|-$)/g, '') || 'connection';
  const data = load();
  let id = base;
  let n = 2;
  while (data.connections.some((c) => c.id === id)) {
    id = `${base}-${n++}`;
  }
  return id;
}

function makeSecretRef(id) {
  return `conn_${id}`;
}

/** A sensible default label when the caller doesn't give one: the host for a custom address, else the adapter's own name. */
export function defaultConnectionLabel({ adapter, baseUrl }) {
  if (baseUrl) {
    try {
      return new URL(baseUrl).host;
    } catch {
      return baseUrl;
    }
  }
  return adapter;
}

/**
 * Adds a connection. `secret`, if given, is saved under a fresh ref unless
 * `secretRef` (a legacy alias, e.g. 'gemini') is passed instead. `id`, if
 * given, is used exactly as-is instead of being derived from the label —
 * the legacy-.env migration needs this so a connection's id can match its
 * secretRef alias exactly ('anthropic', not a label-derived 'claude').
 */
export function addConnection({ adapter, baseUrl, label, secret, secretRef, id: forcedId }) {
  if (!adapter) throw new Error('A connection needs an adapter.');
  const data = load();
  const finalLabel = label || defaultConnectionLabel({ adapter, baseUrl });
  if (forcedId && data.connections.some((c) => c.id === forcedId)) {
    throw new Error(`Connection id already in use: ${forcedId}`);
  }
  const id = forcedId || makeId(finalLabel);
  const ref = secretRef || (secret ? makeSecretRef(id) : null);

  const connection = {
    id,
    label: finalLabel,
    adapter,
    baseUrl: baseUrl || null,
    secretRef: ref,
    createdAt: new Date().toISOString(),
  };

  // Only save a fresh secret when we minted the ref ourselves — a passed-in
  // secretRef (the legacy migration path) already has its value saved.
  if (secret && ref && !secretRef) saveSecret(ref, secret);

  data.connections.push(connection);
  save(data);
  return connection;
}

export function updateConnection(id, patch = {}) {
  const data = load();
  const idx = data.connections.findIndex((c) => c.id === id);
  if (idx === -1) throw new Error(`Unknown connection: ${id}`);
  const connection = { ...data.connections[idx] };

  if (patch.secret !== undefined && patch.secret !== '') {
    const ref = connection.secretRef || makeSecretRef(connection.id);
    saveSecret(ref, patch.secret);
    connection.secretRef = ref;
  }

  const { secret, ...rest } = patch;
  Object.assign(connection, rest);
  data.connections[idx] = connection;
  save(data);
  return connection;
}

/**
 * Removes a connection row and (usually) its secret — never for the three
 * legacy refs, and never when `keepSecret` is set (registry.js uses that
 * when re-pointing surviving models at a different secret isn't the goal —
 * today it never does, but this keeps the option open without a footgun).
 * Does NOT touch any models still pointing at this connection — see
 * registry.js's deleteConnection() for the cascading version callers want.
 */
export function removeConnection(id, { keepSecret = false } = {}) {
  const data = load();
  const connection = data.connections.find((c) => c.id === id);
  if (!connection) return;
  if (connection.secretRef && !keepSecret && !LEGACY_SECRET_REFS.has(connection.secretRef)) {
    deleteSecret(connection.secretRef);
  }
  data.connections = data.connections.filter((c) => c.id !== id);
  save(data);
}

// CRUD over saved connectors (data/connectors.json) — one record per saved
// way to reach something: a browser, a folder allowlist, or a user-added
// integration reached over one of the three supported mechanisms. Dependency-
// free leaf module (same reasoning as skill-store.js/task-store.js — see
// CLAUDE.md's circular-import invariant): this file must never import
// skills/index.js, and nothing here does.
//
// `browser` and `files` are singletons — there's one "your browser control
// settings" and one "your folder allowlist", not a list of them the user
// adds from a catalog. These are Jarvis's own built-in abilities, not
// user-facing connectors, and have no App Control card of their own — see
// CLAUDE.md's "Desktop control / Browser / Files have no settings screen".
//
// `mcp`, `api`, and `cli` are the three add-as-many-as-you-want mechanisms —
// every entry the user connects, whether from the Official Connectors
// directory or as a Custom Connector, is one of these three `type`s. They
// are peers: none is "the real one" with the others bolted on. An Official
// Connector's catalog entry already knows which mechanism it needs; a
// Custom Connector asks the user to pick one (see app-control.js).

import { readJson, writeJson } from '../store.js';

const FILE = 'connectors';

function load() {
  return readJson(FILE, { connectors: [] });
}
function save(data) {
  writeJson(FILE, data);
}

function makeId() {
  return `c${Date.now().toString(36)}${Math.random().toString(36).slice(2, 8)}`;
}

export function listConnectors() {
  return load().connectors;
}

export function getConnector(id) {
  return load().connectors.find((c) => c.id === id) || null;
}

/**
 * Adds a connector. `type` is 'browser'|'files'|'mcp'|'api'|'cli'.
 * `config` is type-specific:
 *
 *   browser: { enabled }
 *   files:   { allowedFolders: [absolute paths] }
 *
 *   mcp: { connectFlow, secretRef, toolPermissions }, where connectFlow is one of:
 *          { kind: 'stdio', command, args, env }                — a locally-spawned server (hidden internal plumbing; never a user-facing choice)
 *          { kind: 'oauth_dcr', url }                            — a remote server that registers Jarvis as a client on the spot (Notion-style)
 *          { kind: 'oauth_guided', url, guide: {steps, note?} }   — a remote server needing a manually-provided OAuth client id/secret first (GitHub/Slack/Google-style); `guide` is the connector detail page's step-by-step instructions
 *          { kind: 'none', url }                                 — verified (oauth.js's probeAuthorization, a real unauthenticated request against the server itself, not a guess) to need no authorization at all; getAccessToken() returns null for these, and mcp-remote-client.js sends no Authorization header
 *
 *   api: { baseUrl, auth: {kind: 'bearer'|'header'|'query', name?, prefix?}, secretRef, specUrl?, operations, toolPermissions }
 *          `specUrl` (optional) is the OpenAPI/Swagger document api-client.js parsed `operations` from — kept so
 *          "refresh" can re-fetch it. `operations` is `[{name, description, method, path, parameters}]`, whether
 *          they came from a spec or were added by hand one at a time.
 *
 *   cli: { command, cwd?, commands, toolPermissions }
 *          `commands` is `[{name, description, argv: [literal or "{placeholder}" entries], args: [{name, description}]}]`
 *          — reviewed and saved by the user after cli-client.js's discovery step. Never a raw shell string; see
 *          cli-client.js's header comment for why.
 *
 * `secretRef` (mcp/api) is set once a connect flow completes or a key is saved — it names the saved value in
 * config.js's secret store; the secret itself never lives in this record.
 * `toolPermissions` (mcp/api/cli) is `{[toolName]: 'blocked' | 'ask'}`, set from the connector detail page's
 * three-state per-tool control (Always allow / Ask every time / Never allow — a later, user-requested redesign
 * matching Claude Desktop's own connector permission screen). A key's absence, or any value other than 'blocked'/
 * legacy 'never', means allowed (the default for every newly-discovered tool) with no extra confirmation beyond
 * risk alone. 'ask' is a standing preference that forces confirmation same as a `risky` tool does automatically —
 * see connectors/index.js's getToolDeclarations() for exactly how the two combine, and its file-header comment
 * for why 'ask' living here again is a deliberate, user-confirmed reversal of an earlier design, not a relapse.
 * `description` is optional, shown on the connector detail page (official catalog entries get theirs from
 * catalog.json instead; this is mainly for a Custom Connector).
 * `status` starts 'untested' — set by a real test/dispatch call.
 * `iconDataUri`/`iconFetchedAt` (mcp/api only, top-level — not inside `config`, same as `mcpTools`) are cached by
 * connectors/icon-resolver.js whenever a connector — catalog or custom alike — is confirmed working: for mcp,
 * the server's own declared icon if it provided one, else a real favicon fetched for its domain (a curated
 * marketing domain for a catalog entry, the connector's own configured address for a custom one). Re-checked
 * (not just fetched once) whenever `iconFetchedAt` ages past icon-resolver.js's staleness window, so a real
 * logo change eventually gets picked up automatically. Resolved server-side specifically so the browser itself
 * never talks to the third-party icon service directly. Absent until first successfully resolved; the client
 * falls back to a catalog connector's hand-authored mark, or a generic glyph for a custom one, until then.
 */
export function addConnector({ type, label, description, config, source }) {
  if (!type) throw new Error('A connector needs a type.');
  const data = load();
  const now = new Date().toISOString();
  const connector = {
    id: makeId(),
    type,
    label: label || type,
    description: description || null,
    enabled: true,
    config: config || {},
    status: { state: 'untested', checkedAt: null, detail: null },
    source: source || { type: 'user' },
    createdAt: now,
    updatedAt: now,
  };
  data.connectors.push(connector);
  save(data);
  return connector;
}

export function updateConnector(id, patch = {}) {
  const data = load();
  const idx = data.connectors.findIndex((c) => c.id === id);
  if (idx === -1) throw new Error(`Unknown connector: ${id}`);
  // config merges shallowly rather than replacing outright — e.g. patching
  // just `toolPermissions` shouldn't require resending the rest of config,
  // regardless of which of the three mechanisms this connector uses.
  const next = {
    ...data.connectors[idx],
    ...patch,
    config: patch.config ? { ...data.connectors[idx].config, ...patch.config } : data.connectors[idx].config,
    updatedAt: new Date().toISOString(),
  };
  data.connectors[idx] = next;
  save(data);
  return next;
}

export function deleteConnector(id) {
  const data = load();
  data.connectors = data.connectors.filter((c) => c.id !== id);
  save(data);
}

/** Returns the one connector of `type` (creating it with `defaults` if it doesn't exist yet) — for the singleton browser/files connectors. */
export function getOrCreateSingleton(type, defaults = {}) {
  const existing = load().connectors.find((c) => c.type === type);
  if (existing) return existing;
  return addConnector({ type, label: defaults.label || type, config: defaults.config || {} });
}

export function recordStatus(id, state, detail) {
  return updateConnector(id, { status: { state, checkedAt: new Date().toISOString(), detail: detail || null } });
}

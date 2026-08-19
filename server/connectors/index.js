// Aggregates every enabled connector's tools into one flat list, and
// dispatches a call by name to whichever connector/client actually handles
// it. This is the third source `skills/index.js` merges in (built-in code
// skills, playbook skills, now connector tools) — a connector tool is
// indistinguishable from any other skill to the model.
//
// Imports no skill loader (`skills/index.js`) — see CLAUDE.md's circular-
// import invariant; this file is imported BY `skills/index.js`, never the
// other way around.
//
// Five connector types: `files`, `browser` (singletons, Jarvis's own
// built-in abilities — no App Control card, no per-tool permissions, see
// CLAUDE.md's "Desktop control / Browser / Files have no settings screen"),
// and `mcp` / `api` / `cli` — the three peer, user-facing integration
// mechanisms. None of the three is privileged as "the real one" with the
// others bolted on: `mcpRawTools()`/`apiClient.toolDeclarations()`/
// `cliClient.toolDeclarations()` below feed the exact same permission-
// filtering, risk-classification, and prefixing logic.
//
// Two genuinely separate things, per the user's own original requirements
// (kept apart deliberately, not merged): a **standing permission** (is
// Jarvis allowed to use this tool at ALL — the user's call, per tool,
// `config.toolPermissions`, the same shape for all three mechanisms) and
// **runtime confirmation** (does using an allowed tool pause to ask right
// now). Runtime confirmation has two independent triggers, neither able to
// suppress the other: `guard.js`'s automatic risk classifier (a `risky` tool
// always confirms, full stop, no per-tool override — this is the original,
// unchanged floor) and, since a later user-requested redesign matching
// Claude Desktop's own three-state per-tool control (Always allow / Ask /
// Deny), a genuine per-tool "ask" preference living in the same
// `toolPermissions` map. See getToolDeclarations() below for exactly how
// the two combine.

import { listConnectors, getConnector } from './store.js';
import { classifyActionRisk } from '../control/guard.js';
import * as filesConnector from './files.js';
import * as browserConnector from './browser.js';
import * as mcpClient from './mcp-client.js';
import * as mcpRemoteClient from './mcp-remote-client.js';
import * as apiClient from './api-client.js';
import * as cliClient from './cli-client.js';

const NAME_SEP = '__';

function sanitizeIdentifier(text) {
  return String(text || '')
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '_')
    .replace(/^_+|_+$/g, '')
    .slice(0, 40) || 'connector';
}

function prefixedName(connector, toolName) {
  return `${sanitizeIdentifier(connector.label || connector.id)}${NAME_SEP}${sanitizeIdentifier(toolName)}`;
}

// A real MCP server's tool description is documentation, not a short label —
// Notion's own descriptions run to a couple thousand characters with usage
// guidance and worked JSON examples (confirmed against the user's live
// Notion connector). Feeding the whole thing to classifyActionRisk()'s
// keyword scan found far more incidental matches than genuine ones (e.g.
// "SharePoint" mentioned in passing, "in sidebar order" describing a list) —
// only the first sentence, where a tool's description almost always states
// its actual purpose, goes into the risk check. Applies equally to an API
// operation's summary or a CLI command's description — none of the three
// mechanisms get a pass on this.
function shortDescription(text) {
  const clean = String(text || '').replace(/\s+/g, ' ').trim();
  const firstSentence = clean.match(/^[^.!?]*[.!?]/)?.[0] || clean;
  return firstSentence.slice(0, 200);
}

/**
 * 'safe' | 'notable' | 'risky' for one connector tool — the ONE place this
 * computation happens, so `getToolDeclarations()` (which decides `confirm`
 * for real) and `server.js`'s `publicConnector()` (which shows an
 * informational "always confirms" badge on the detail page) can never drift
 * out of sync with each other. Mechanism-agnostic — called identically for
 * an mcp tool, an api operation, or a cli command.
 *
 * The tool's own NAME is only scanned for risky keywords when its
 * description is too thin to say anything real — a real, substantive
 * description is trusted as the authoritative signal about what a tool
 * actually does, over a bare identifier. This matters for a real, live
 * pattern: a generic multi-purpose gateway/executor tool (Composio's
 * COMPOSIO_MULTI_EXECUTE_TOOL is the confirmed live example — one dispatcher
 * tool that runs whatever underlying action was found via search, across
 * every connected service) can have "execute"/"run"/"manage" baked into its
 * own name as a structural word-part with NO relationship to whether a given
 * call is actually dangerous — that word alone was making EVERY action
 * routed through it always require confirmation, regardless of what the
 * action actually was, which is the opposite of what a risk classifier is
 * for. Name-scanning stays for the original motivating case this was built
 * for (an under-described tool like a bare "updatePet" with nothing else to
 * go on) — it only steps back once there's real prose to read instead.
 */
export function classifyToolRisk(name, description) {
  const label = shortDescription(description);
  const hasRealDescription = label.trim().length >= 15;
  return classifyActionRisk({ kind: hasRealDescription ? '' : name, label });
}

/**
 * Every real tool/operation/command a connector currently offers, in a
 * uniform pre-permission shape: `{name, description, parameters,
 * dispatchName}`. `name` is what a standing permission is keyed on;
 * `dispatchName` is what the underlying client (mcp/api/cli) expects back
 * when the tool is actually called — for mcp this is the server's own
 * (unsanitized) tool name, for api/cli it's the same already-clean `name`
 * used everywhere else, since those two are built sanitized from the start.
 */
function rawToolsFor(connector) {
  if (connector.type === 'mcp') {
    // Tool names/schemas come from the server itself — only known once
    // connected, so a connector that isn't connected yet simply contributes
    // nothing (its Connect flow / a "refresh tools" action populates
    // `mcpTools` on the record).
    return (connector.mcpTools || []).map((t) => ({
      name: t.name,
      description: t.description || `Call "${t.name}" on ${connector.label}.`,
      parameters: t.inputSchema || { type: 'object', properties: {}, required: [] },
      dispatchName: t.name,
    }));
  }
  if (connector.type === 'api') {
    return apiClient.toolDeclarations(connector.config).map((d) => ({ ...d, dispatchName: d.name }));
  }
  if (connector.type === 'cli') {
    return cliClient.toolDeclarations(connector.config).map((d) => ({ ...d, dispatchName: d.name }));
  }
  return [];
}

/** Every currently enabled connector's tool declarations, in the plain `{name, description, parameters}` shape every adapter expects — plus enough bookkeeping for dispatch() to route a call back to the right connector. */
export function getToolDeclarations() {
  const declarations = [];
  for (const connector of listConnectors()) {
    if (!connector.enabled) continue;

    if (connector.type === 'files') {
      declarations.push(...filesConnector.toolDeclarations().map((d) => ({ ...d, connectorId: connector.id })));
      continue;
    }
    if (connector.type === 'browser') {
      declarations.push(...browserConnector.toolDeclarations().map((d) => ({ ...d, connectorId: connector.id })));
      continue;
    }
    if (connector.type !== 'mcp' && connector.type !== 'api' && connector.type !== 'cli') continue;

    // The same standing-permission check, the same prefixing, for all three
    // user-facing mechanisms — none gets special-cased. Three values now:
    // 'blocked' (legacy 'never' reads the same) skips the tool entirely;
    // 'ask' lets it through but is carried alongside the declaration so the
    // confirm computation below can honor it; a key's absence, or any other
    // value, means allowed with no extra confirmation beyond what risk alone
    // would require. This reintroduces a genuine per-tool "ask" as a real
    // user preference — a deliberate, user-confirmed reversal of this file's
    // earlier design (see CLAUDE.md's permissions-rebuild history), NOT a
    // relapse into the old conflated 3-way dropdown: it still can't override
    // a `risky` tool's forced confirm below, which stays automatic and
    // un-silenceable exactly as before.
    const permissions = connector.config?.toolPermissions || {};
    for (const tool of rawToolsFor(connector)) {
      const permission = permissions[tool.name];
      if (permission === 'blocked' || permission === 'never') continue;
      declarations.push({
        connectorId: connector.id,
        name: prefixedName(connector, tool.name),
        description: tool.description,
        parameters: tool.parameters,
        dispatchName: tool.dispatchName,
        askAlways: permission === 'ask',
      });
    }
  }

  // Runtime confirmation: `risky` always forces it (the floor a standing
  // permission can never lower — "regardless of what standing permissions
  // I've configured"), and now `askAlways` (set above, from the user's own
  // per-tool "Ask every time" choice) also forces it for an otherwise-safe
  // tool. Neither can suppress the other; only two independent ways to end
  // up needing confirmation, never a way to end up with less than risk alone
  // would already require.
  const withRisk = declarations.map((d) => {
    const risk = classifyToolRisk(d.name, d.description);
    const { askAlways, ...rest } = d;
    return { ...rest, confirm: risk === 'risky' || askAlways ? 'always' : undefined };
  });

  // Rebuilt every call (connectors can change between turns) so
  // runConnectorTool() can find a name's full declaration — including its
  // dispatchName bookkeeping — without recomputing everything.
  declarationIndex = new Map(withRisk.map((d) => [d.name, d]));
  return withRisk;
}

let declarationIndex = new Map();

/** Runs a connector tool by its (prefixed, for mcp/api/cli) or plain (for files/browser) name. Throws on failure — callers (skills/index.js's runSkill) already catch and wrap. */
export async function runConnectorTool(name, args) {
  const decl = declarationIndex.get(name);
  if (!decl) throw new Error(`Unknown connector tool: ${name}`);
  const connector = getConnector(decl.connectorId);
  if (!connector || !connector.enabled) throw new Error('That connector is no longer available.');

  // files/browser tool names are never prefixed (see getToolDeclarations() —
  // only mcp/api/cli go through prefixedName()), since those two connector
  // types are singletons and can never collide with each other. decl.name is
  // already the right dispatch name for them.
  if (connector.type === 'files') return filesConnector.dispatch(decl.name, args, connector.config);
  if (connector.type === 'browser') return browserConnector.dispatch(decl.name, args);
  if (connector.type === 'api') return apiClient.dispatch(decl.dispatchName, args, connector.config);
  if (connector.type === 'cli') return cliClient.dispatch(decl.dispatchName, args, connector.config);
  if (connector.type === 'mcp') {
    const flow = connector.config?.connectFlow;
    if (flow?.kind === 'stdio') {
      return mcpClient.callTool(connector.id, flow, decl.dispatchName, args);
    }
    // 'oauth_dcr' or 'oauth_guided' — a real remote server, reached over the
    // Streamable HTTP transport with a bearer token oauth.js keeps fresh.
    return mcpRemoteClient.callTool(connector.id, flow?.url, decl.dispatchName, args);
  }
  throw new Error(`Unknown connector type: ${connector.type}`);
}

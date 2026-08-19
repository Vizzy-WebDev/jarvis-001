// Streamable HTTP MCP transport — the wire format real hosted MCP servers
// speak (Notion/GitHub/Slack/Google/Stripe-style). Rewritten from scratch as
// part of the connector-catalog rebuild, treating MCP as a genuine peer next
// to the new API/CLI mechanisms rather than pre-existing code left alone.
//
// The wire-format facts below are unchanged by the rewrite — real properties
// of the real protocol, not something the rewrite gets to relitigate:
// every JSON-RPC call is a POST to one URL, carrying a bearer token and an
// `Mcp-Session-Id` once the server assigns one. A response comes back as
// either plain `application/json` or `text/event-stream` (SSE) — a server
// MAY upgrade to streaming even for what's logically a single request/
// response, so both are parsed rather than assuming JSON. Plain `fetch()`,
// no new dependency.

import { getAccessToken } from './oauth.js';
import { getConnector, updateConnector, recordStatus } from './store.js';

const PROTOCOL_VERSION = '2024-11-05';
const CLIENT_INFO = { name: 'jarvis', version: '1.0.0' };

const sessionByConnector = new Map(); // connectorId -> mcpSessionId
const serverInfoByConnector = new Map(); // connectorId -> last initialize() result's serverInfo (or null) — see getServerInfo() below

/** Parses an SSE-framed response body, returning the JSON-RPC message from its `data:` line(s). Minimal on purpose — this client only ever needs the single message a request/response call produces, not a long-lived event stream. */
function parseSseMessage(text) {
  const dataLines = text
    .split(/\r?\n/)
    .filter((line) => line.startsWith('data:'))
    .map((line) => line.slice(5).trim());
  if (!dataLines.length) throw new Error('The server sent an empty response.');
  return JSON.parse(dataLines.join('\n'));
}

async function parseResponseBody(res) {
  const contentType = res.headers.get('Content-Type') || '';
  const text = await res.text();
  return contentType.includes('text/event-stream') ? parseSseMessage(text) : JSON.parse(text);
}

async function rpcCall(connectorId, url, method, params) {
  // null for a connector verified (at connect time, via oauth.js's
  // probeAuthorization) to need no authorization at all — the Authorization
  // header is only set when there's an actual token to send, rather than
  // always requiring one and making a no-auth MCP server unreachable by
  // construction (see CLAUDE.md's MCP architecture audit).
  const accessToken = await getAccessToken(connectorId);
  const headers = {
    'Content-Type': 'application/json',
    Accept: 'application/json, text/event-stream',
  };
  if (accessToken) headers.Authorization = `Bearer ${accessToken}`;
  const sessionId = sessionByConnector.get(connectorId);
  if (sessionId) headers['Mcp-Session-Id'] = sessionId;

  const res = await fetch(url, {
    method: 'POST',
    headers,
    body: JSON.stringify({ jsonrpc: '2.0', id: 1, method, params: params || {} }),
  });

  const returnedSessionId = res.headers.get('Mcp-Session-Id');
  if (returnedSessionId) sessionByConnector.set(connectorId, returnedSessionId);

  if (res.status === 401) {
    // A real 401 on an actual call is authoritative, even for a connector
    // saved as needing no authorization at all — some servers (confirmed
    // live: Google's Gmail/Drive MCP servers) answer the handshake calls
    // anonymously and only gate real tool use, which oauth.js's connect-time
    // probe alone can't see (see discoverProtectedResource()'s header
    // comment). Retiring a wrong 'none' conclusion here means the NEXT
    // Connect attempt runs the full discovery + registration chain instead
    // of the same wrong shortcut forever.
    const connector = getConnector(connectorId);
    const flow = connector?.config?.connectFlow;
    const message = 'This service needs you to sign in — open it in App Control and click Connect.';
    if (flow?.kind === 'none') {
      updateConnector(connectorId, { config: { connectFlow: { ...flow, kind: 'oauth_dcr' } } });
    }
    recordStatus(connectorId, 'error', message);
    throw new Error(message);
  }

  if (!res.ok) throw new Error(`The service responded with an error (${res.status}) to "${method}".`);

  const message = await parseResponseBody(res);
  if (message.error) throw new Error(message.error.message || 'The service reported an error.');
  return message.result;
}

/** The MCP initialize handshake — establishes the session id every subsequent call reuses. Also caches the server's own `serverInfo` (name, and — an optional, fairly new part of the spec — a declared icon) for getServerInfo() below, since this is the only place that data is ever available. */
export async function initialize(connectorId, url) {
  const result = await rpcCall(connectorId, url, 'initialize', {
    protocolVersion: PROTOCOL_VERSION,
    capabilities: {},
    clientInfo: CLIENT_INFO,
  });
  serverInfoByConnector.set(connectorId, result?.serverInfo || null);
  return result;
}

/**
 * Whatever the server declared about itself in its own `initialize`
 * response, as of the last time initialize() actually ran for this
 * connector (every listTools()/callTool() call already triggers one) — a
 * synchronous read of already-fetched data, never a new network round
 * trip. Returns null if this connector was never initialized this session,
 * or if the server declared nothing. Used by
 * connectors/icon-resolver.js's mcpDeclaredIcon() to prefer a server's own
 * real icon over guessing one from its domain, when one is actually there.
 */
export function getServerInfo(connectorId) {
  return serverInfoByConnector.get(connectorId) || null;
}

export async function listTools(connectorId, url) {
  await initialize(connectorId, url);
  const result = await rpcCall(connectorId, url, 'tools/list', {});
  return result.tools || [];
}

export async function callTool(connectorId, url, toolName, args) {
  if (!sessionByConnector.has(connectorId)) await initialize(connectorId, url);
  const result = await rpcCall(connectorId, url, 'tools/call', { name: toolName, arguments: args || {} });
  if (result?.isError) {
    const text = result.content?.map((c) => c.text).filter(Boolean).join(' ') || 'The tool reported an error.';
    throw new Error(text);
  }
  return result;
}

/** Drops the cached session (e.g. after reconnecting) — the next call re-initializes fresh. */
export function forgetSession(connectorId) {
  sessionByConnector.delete(connectorId);
}

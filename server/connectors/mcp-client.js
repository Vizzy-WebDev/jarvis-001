// A hand-written MCP (Model Context Protocol) stdio client — newline-
// delimited JSON-RPC 2.0 over a spawned process's stdin/stdout. Rewritten
// from scratch as part of the connector-catalog rebuild, treating MCP as a
// genuine peer next to the new API/CLI mechanisms rather than pre-existing
// code left untouched.
//
// Still hand-written rather than `@modelcontextprotocol/sdk`, on the same
// grounds as before: keeps `package.json` unchanged (`Start Jarvis.bat` only
// runs `npm install` when `node_modules` is missing, so a new dependency
// risks a broken double-click launch for a non-technical user). MCP's stdio
// surface used here is small: `initialize`, an `initialized` notification,
// `tools/list`, `tools/call`.
//
// One client per configured MCP connector, kept alive across calls — same
// "one long-lived process, not one per action" reasoning as
// control/ps-bridge.js, and the request/response id-matching pattern here is
// deliberately the same shape as that file.

import { spawn } from 'node:child_process';
import readline from 'node:readline';

const REQUEST_TIMEOUT_MS = 15 * 1000;
const PROTOCOL_VERSION = '2024-11-05';
const CLIENT_INFO = { name: 'jarvis', version: '1.0.0' };

const clientsByConnector = new Map(); // connectorId -> { proc, pending, nextId }

function sendRequest(client, method, params) {
  const id = client.nextId++;
  const payload = JSON.stringify({ jsonrpc: '2.0', id, method, params: params || {} });
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      client.pending.delete(id);
      reject(new Error(`MCP server timed out responding to "${method}".`));
    }, REQUEST_TIMEOUT_MS);
    client.pending.set(id, { resolve, reject, timer });
    client.proc.stdin.write(payload + '\n', (err) => {
      if (err) {
        client.pending.delete(id);
        clearTimeout(timer);
        reject(new Error(`Could not reach the MCP server: ${err.message}`));
      }
    });
  });
}

function sendNotification(client, method, params) {
  // No id, so no response is ever expected — used only for the
  // post-initialize `notifications/initialized` handshake step.
  client.proc.stdin.write(JSON.stringify({ jsonrpc: '2.0', method, params: params || {} }) + '\n');
}

function handleResponseLine(client, line) {
  if (!line.trim()) return;
  let message;
  try {
    message = JSON.parse(line);
  } catch {
    return; // a stray non-JSON line (some servers log to stdout by mistake) — ignore rather than crash
  }
  if (message.id === undefined) return; // a notification from the server — nothing we act on here
  const pending = client.pending.get(message.id);
  if (!pending) return;
  client.pending.delete(message.id);
  clearTimeout(pending.timer);
  if (message.error) pending.reject(new Error(message.error.message || 'MCP server returned an error.'));
  else pending.resolve(message.result);
}

function failAllPending(client, reason) {
  for (const [, pending] of client.pending) {
    clearTimeout(pending.timer);
    pending.reject(new Error(reason));
  }
  client.pending.clear();
}

async function ensureClient(connectorId, { command, args, env }) {
  const existing = clientsByConnector.get(connectorId);
  if (existing && !existing.proc.killed) return existing;

  const proc = spawn(command, args || [], {
    env: { ...process.env, ...(env || {}) },
    windowsHide: true,
  });
  const client = { proc, pending: new Map(), nextId: 1 };
  clientsByConnector.set(connectorId, client);

  proc.on('error', (err) => failAllPending(client, `Could not start the MCP server: ${err.message}`));
  proc.on('exit', () => {
    failAllPending(client, 'The MCP server process exited.');
    clientsByConnector.delete(connectorId);
  });

  const lineReader = readline.createInterface({ input: proc.stdout });
  lineReader.on('line', (line) => handleResponseLine(client, line));

  // Minimal initialize handshake — protocolVersion is a plain string here
  // (no version-negotiation dance); real servers are tolerant of a
  // reasonably recent value. clientInfo just identifies Jarvis in logs.
  await sendRequest(client, 'initialize', { protocolVersion: PROTOCOL_VERSION, capabilities: {}, clientInfo: CLIENT_INFO });
  sendNotification(client, 'notifications/initialized');

  return client;
}

/** Connects (if needed) and returns the server's tool list — `[{name, description, inputSchema}]`. Throws on failure; callers treat that as "connector test failed". */
export async function listTools(connectorId, config) {
  const client = await ensureClient(connectorId, config);
  const result = await sendRequest(client, 'tools/list', {});
  return result.tools || [];
}

/** Calls one tool by name. Throws on failure. */
export async function callTool(connectorId, config, toolName, args) {
  const client = await ensureClient(connectorId, config);
  const result = await sendRequest(client, 'tools/call', { name: toolName, arguments: args || {} });
  if (result?.isError) {
    const text = result.content?.map((c) => c.text).filter(Boolean).join(' ') || 'The tool reported an error.';
    throw new Error(text);
  }
  return result;
}

/** Stops one connector's MCP process (connector disabled/removed) — a fresh one spawns automatically on next use. */
export function stopClient(connectorId) {
  const client = clientsByConnector.get(connectorId);
  if (!client) return;
  failAllPending(client, 'Stopped.');
  try {
    client.proc.kill();
  } catch {
    // Already exiting — fine either way.
  }
  clientsByConnector.delete(connectorId);
}

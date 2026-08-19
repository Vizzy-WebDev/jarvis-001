// Owns the one long-lived PowerShell process (server/control/agent.ps1) that
// actually moves the mouse, types, reads windows, and takes screenshots.
// One process instead of one-per-action — a cold `powershell.exe` start
// costs ~300ms, and a control session doing dozens of actions would waste
// seconds on process startup alone (see the design plan's rationale).
//
// Requests and responses share a single stdin/stdout pair, matched by a
// numeric id — send() queues a request, resolves the matching in-flight
// promise when a `{id, ok, result|error}` line comes back on stdout. Only
// one thing needs this at a time (a single control session), so no real
// concurrency control beyond the id map is needed.
//
// A real gotcha found while hand-testing agent.ps1: a synthetic mouse click
// does NOT reliably restore keyboard focus to the clicked window (Windows'
// anti-focus-stealing rules can suppress it even for a real hardware input
// event) — SendKeys then silently goes to whatever window last had focus
// instead. control/session.js MUST send `focus` immediately before any
// click/type/key batch on a window, every time, not just once per session.

import { spawn } from 'node:child_process';
import readline from 'node:readline';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const SCRIPT_PATH = path.join(__dirname, 'agent.ps1');
const REQUEST_TIMEOUT_MS = 20 * 1000;

let child = null;
let rl = null;
let nextId = 1;
const pending = new Map(); // id -> {resolve, reject, timer}

function stripParamBlock(scriptText) {
  return scriptText.replace(/# ---PARAM-BLOCK-START---[\s\S]*?# ---PARAM-BLOCK-END---/, '');
}

function buildEncodedCommand() {
  const raw = fs.readFileSync(SCRIPT_PATH, 'utf8');
  const body = stripParamBlock(raw);
  return Buffer.from(body, 'utf16le').toString('base64');
}

function failAllPending(reason) {
  for (const [, entry] of pending) {
    clearTimeout(entry.timer);
    entry.reject(new Error(reason));
  }
  pending.clear();
}

function handleLine(line) {
  if (!line.trim()) return;
  let msg;
  try {
    msg = JSON.parse(line);
  } catch {
    console.error('[ps-bridge] Could not parse line from agent.ps1:', line);
    return;
  }
  const entry = pending.get(msg.id);
  if (!entry) return; // a stray/duplicate line — nothing waiting on this id
  pending.delete(msg.id);
  clearTimeout(entry.timer);
  if (msg.ok) entry.resolve(msg.result);
  else entry.reject(new Error(msg.error || 'The control agent reported an error.'));
}

function spawnAgent() {
  // -STA: UI Automation and the clipboard both expect a single-threaded
  // apartment; PowerShell's default is MTA. Confirmed necessary by hand —
  // clipboard paste and UI Automation reads are the two primitives that
  // actually depend on it.
  const primaryArgs = [
    '-NoProfile',
    '-NonInteractive',
    '-ExecutionPolicy', 'Bypass',
    '-STA',
    '-File', SCRIPT_PATH,
  ];

  let proc;
  try {
    proc = spawn('powershell.exe', primaryArgs, { windowsHide: true });
  } catch (err) {
    console.warn('[ps-bridge] -File launch failed, falling back to -EncodedCommand:', err?.message);
    proc = spawn(
      'powershell.exe',
      ['-NoProfile', '-NonInteractive', '-STA', '-EncodedCommand', buildEncodedCommand()],
      { windowsHide: true }
    );
  }

  proc.stderr.on('data', (chunk) => {
    console.error('[ps-bridge] agent.ps1 stderr:', chunk.toString());
  });
  proc.on('error', (err) => {
    console.error('[ps-bridge] process error:', err.message);
  });
  proc.on('exit', (code) => {
    console.warn(`[ps-bridge] agent.ps1 exited (code ${code}) — will restart on next command.`);
    failAllPending('The control agent process exited unexpectedly.');
    child = null;
    rl = null;
  });

  const lineReader = readline.createInterface({ input: proc.stdout });
  lineReader.on('line', handleLine);

  return { proc, lineReader };
}

function ensureRunning() {
  if (child && !child.killed) return;
  const spawned = spawnAgent();
  child = spawned.proc;
  rl = spawned.lineReader;
}

/** Sends one command (`{command, ...params}`) to the agent and resolves with its `result`, or rejects with a plain-language error. Restarts the agent process first if it isn't running. */
export function send(command, params = {}) {
  ensureRunning();
  const id = nextId++;
  const payload = JSON.stringify({ id, command, ...params });

  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      pending.delete(id);
      reject(new Error(`Timed out waiting for "${command}" to finish.`));
    }, REQUEST_TIMEOUT_MS);
    pending.set(id, { resolve, reject, timer });
    child.stdin.write(payload + '\n', (err) => {
      if (err) {
        pending.delete(id);
        clearTimeout(timer);
        reject(new Error(`Could not reach the control agent: ${err.message}`));
      }
    });
  });
}

/** Hard-stops the agent process — used when a control session ends, so no PowerShell process lingers between sessions holding no useful state. A fresh one spawns automatically on the next send(). */
export function stopAgent() {
  failAllPending('Stopped.');
  if (child && !child.killed) {
    try {
      child.kill();
    } catch {
      // Already exiting — fine either way.
    }
  }
  child = null;
  rl = null;
}

export function isAgentRunning() {
  return Boolean(child && !child.killed);
}

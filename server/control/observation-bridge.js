// Manages the always-on-top blue "Jarvis can see your screen" badge
// (observation-indicator.ps1) as its own OS process — same lazy-spawn/
// status-file-polling shape as control/overlay-bridge.js's red control bar,
// kept as a wholly separate process and SSE event type on purpose: the red
// bar means "Jarvis is operating your mouse/keyboard right now" (which
// already implies it can see the screen too — no need for a second signal
// during a control session); this one means "Jarvis is *only* looking" —
// look_at_screen.js's one-off glance, or a screen_looks_like monitor's
// ongoing visual watch. Callers: look_at_screen.js (show for the few seconds
// it's actually capturing+describing) and monitor/engine.js (show for the
// whole lifetime of a screen_looks_like watch, not just its brief per-tick
// captures — see engine.js's own comment on why).
//
// Also broadcasts over the existing SSE channel (server/events.js) so the
// in-page dot (public/app.js) stays in sync with the desktop badge — one
// source of truth, two renderers, same pattern overlay-bridge.js already
// uses for the control banner.

import { spawn } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { dataFilePath } from '../store.js';
import { broadcast } from '../events.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const SCRIPT_PATH = path.join(__dirname, 'observation-indicator.ps1');
const STATUS_FILE = dataFilePath('observation-status');
const PORT = process.env.PORT ? Number(process.env.PORT) : 3000;

let child = null;
let active = false;

// Reference-counted rather than a plain on/off flag: look_at_screen.js and
// monitor/engine.js are two independent callers that can each want the
// badge lit at the same time (e.g. a screen_looks_like watch is already
// running when the user separately says "look at my screen") — a naive
// show()/hide() pair would let whichever call finishes first turn the badge
// off out from under the other one, even though observation is still
// genuinely happening. Each showIndicator() call gets its own token; the
// badge only actually goes dark once every outstanding token has been
// released via hideIndicator().
let nextToken = 1;
const activeTokens = new Set();

function writeStatusFile(status) {
  // Atomic write (temp + rename) — observation-indicator.ps1 polls this file
  // every 400ms and must never observe a half-written JSON blob mid-write.
  const tmp = `${STATUS_FILE}.${process.pid}.tmp`;
  fs.writeFileSync(tmp, JSON.stringify(status), 'utf8');
  fs.renameSync(tmp, STATUS_FILE);
}

function stripParamBlock(scriptText) {
  return scriptText.replace(/# ---PARAM-BLOCK-START---[\s\S]*?# ---PARAM-BLOCK-END---/, '');
}

/** Same locked-down-execution-policy fallback as overlay-bridge.js's buildEncodedCommand() — see that file for why this exists. */
function buildEncodedCommand() {
  const raw = fs.readFileSync(SCRIPT_PATH, 'utf8');
  const body = stripParamBlock(raw);
  const preamble = `$Port = ${PORT}\n$StatusFile = '${STATUS_FILE.replace(/'/g, "''")}'\n`;
  const full = preamble + body;
  return Buffer.from(full, 'utf16le').toString('base64');
}

function spawnIndicator() {
  const primaryArgs = [
    '-NoProfile',
    '-NonInteractive',
    '-ExecutionPolicy', 'Bypass',
    '-WindowStyle', 'Hidden',
    '-File', SCRIPT_PATH,
    '-Port', String(PORT),
    '-StatusFile', STATUS_FILE,
  ];

  let proc;
  try {
    proc = spawn('powershell.exe', primaryArgs, { stdio: 'ignore', windowsHide: true });
  } catch (err) {
    console.warn('[observation] -File launch failed, falling back to -EncodedCommand:', err?.message);
    proc = spawn(
      'powershell.exe',
      ['-NoProfile', '-NonInteractive', '-WindowStyle', 'Hidden', '-EncodedCommand', buildEncodedCommand()],
      { stdio: 'ignore', windowsHide: true }
    );
  }

  proc.on('error', (err) => {
    console.error('[observation] PowerShell process error:', err.message);
    child = null;
  });
  proc.on('exit', () => {
    child = null;
  });
  return proc;
}

function ensureRunning() {
  if (child && !child.killed) return;
  child = spawnIndicator();
}

/**
 * Shows the badge (spawning the process if it isn't already running) and
 * returns a token — pass that exact token to hideIndicator() when this
 * particular observation ends. `reason` is carried on the SSE event only,
 * for the in-page dot's tooltip; the desktop badge itself has no text.
 */
export function showIndicator(reason = 'Jarvis can see your screen') {
  const token = nextToken++;
  activeTokens.add(token);
  active = true;
  ensureRunning();
  writeStatusFile({ active: true });
  broadcast({ type: 'observation_status', active: true, reason });
  return token;
}

/**
 * Releases one showIndicator() token. The badge only actually hides once
 * every outstanding token has been released — see the reference-counting
 * comment above `activeTokens`. Safe to call with an already-released or
 * unknown token (Set.delete is a no-op) — a caller's own finally block
 * never needs to guard this.
 */
export function hideIndicator(token) {
  activeTokens.delete(token);
  if (activeTokens.size > 0) return;

  active = false;
  writeStatusFile({ active: false });
  broadcast({ type: 'observation_status', active: false });
  const toKill = child;
  child = null;
  if (toKill && !toKill.killed) {
    setTimeout(() => {
      try {
        if (!toKill.killed) toKill.kill();
      } catch {
        // Already exited on its own — the expected, common case.
      }
    }, 3000).unref();
  }
}

export function isIndicatorActive() {
  return active;
}

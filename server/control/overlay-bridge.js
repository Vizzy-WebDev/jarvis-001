// Manages the always-on-top red overlay (overlay.ps1) as its own OS
// process — spawned lazily on the first showOverlay(), left running while
// active, closed by the script itself once it sees {active:false} in the
// status file it polls. See overlay.ps1's header comment for the full
// division of labor between it and this file.
//
// Also broadcasts the same show/step/hide transitions over the existing SSE
// channel (server/events.js) so the in-page control banner (public/app.js)
// stays in sync with the overlay bar — one source of truth, two renderers.

import { spawn } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { dataFilePath } from '../store.js';
import { broadcast } from '../events.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const SCRIPT_PATH = path.join(__dirname, 'overlay.ps1');
const STATUS_FILE = dataFilePath('control-status');
const PORT = process.env.PORT ? Number(process.env.PORT) : 3000;

let child = null;
let active = false;
let currentStep = '';

function writeStatusFile(status) {
  // Atomic write (temp + rename) — overlay.ps1 polls this file every 400ms
  // and must never observe a half-written JSON blob mid-write.
  const tmp = `${STATUS_FILE}.${process.pid}.tmp`;
  fs.writeFileSync(tmp, JSON.stringify(status), 'utf8');
  fs.renameSync(tmp, STATUS_FILE);
}

function stripParamBlock(scriptText) {
  return scriptText.replace(/# ---PARAM-BLOCK-START---[\s\S]*?# ---PARAM-BLOCK-END---/, '');
}

/** Fallback launch path for a machine whose execution policy blocks `-File` even with `-ExecutionPolicy Bypass` on the command line (some locked-down corporate images do). Splices the param() block out (see overlay.ps1's markers) and prepends the values as plain variable assignments instead, since a `-Command`/`-EncodedCommand` blob can't bind `-Port`/`-StatusFile` the normal way. */
function buildEncodedCommand() {
  const raw = fs.readFileSync(SCRIPT_PATH, 'utf8');
  const body = stripParamBlock(raw);
  const preamble = `$Port = ${PORT}\n$StatusFile = '${STATUS_FILE.replace(/'/g, "''")}'\n`;
  const full = preamble + body;
  return Buffer.from(full, 'utf16le').toString('base64');
}

function spawnOverlay() {
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
    console.warn('[overlay] -File launch failed, falling back to -EncodedCommand:', err?.message);
    proc = spawn(
      'powershell.exe',
      ['-NoProfile', '-NonInteractive', '-WindowStyle', 'Hidden', '-EncodedCommand', buildEncodedCommand()],
      { stdio: 'ignore', windowsHide: true }
    );
  }

  proc.on('error', (err) => {
    console.error('[overlay] PowerShell process error:', err.message);
    child = null;
  });
  proc.on('exit', () => {
    child = null;
  });
  return proc;
}

function ensureRunning() {
  if (child && !child.killed) return;
  child = spawnOverlay();
}

/** Shows the overlay (spawning the process if it isn't already running) with the given step text. */
export function showOverlay(step = 'Working…') {
  active = true;
  currentStep = step;
  ensureRunning();
  writeStatusFile({ active: true, step });
  broadcast({ type: 'control_status', active: true, step });
}

/** Updates the step text of an already-visible overlay. A no-op if nothing is currently showing — callers don't need to guard this themselves. */
export function updateStep(step) {
  if (!active) return;
  currentStep = step;
  writeStatusFile({ active: true, step });
  broadcast({ type: 'control_status', active: true, step });
}

/** Hides the overlay. The PowerShell process closes itself once it polls and sees `active:false`; a short grace kill below is just a backstop in case that poll never happens (e.g. the process hung). */
export function hideOverlay() {
  active = false;
  currentStep = '';
  writeStatusFile({ active: false });
  broadcast({ type: 'control_status', active: false });
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

export function isOverlayActive() {
  return active;
}

export function currentOverlayStep() {
  return currentStep;
}

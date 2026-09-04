// Security check #3: an unexpected local TCP listener appearing — a new
// process accepting connections that wasn't there before. Detection only;
// no attempt to close a port or kill a process, per the owner's own
// explicit scope.
//
// Uses PowerShell's `Get-NetTCPConnection`, not `netstat` text parsing —
// same preference CLAUDE.md's own environment note states for this
// project (structured, not locale/version-dependent output). A fresh,
// short-lived `powershell.exe` call per check (every few minutes, per
// source.js's own interval) rather than the persistent `control/ps-bridge.js`
// agent process — deliberately NOT importing that module here: it's built
// for interactive desktop control (clicks, screenshots) with its own
// request/response protocol and restart logic, real overkill for "run one
// read-only query and exit," and pulling it in would be the one path that
// could ever make this leaf-safe check transitively reach
// `control/session.js`.
//
// **First-seen-is-baseline, same pattern as config-integrity.js's own
// hash history** — a fresh Jarvis install, or any restart, has no meaningful
// prior state to compare against; the FIRST check just records what's
// listening right now rather than treating a totally fresh baseline as N
// new findings.

import { execFile } from 'node:child_process';
import { getDb } from '../../../../db.js';

const STATE_KEY = 'ops_diag_listeners';
const CHECK_TIMEOUT_MS = 8000;

// This app's own real listening port — the one entry that's ALWAYS
// expected and must never itself be flagged. Recomputed the same way
// server.js itself does rather than importing server.js (which would
// re-run its own top-level startup code — never something a diagnostic
// check should risk triggering).
function ownPort() {
  return process.env.PORT ? Number(process.env.PORT) : 3000;
}

function listListeningPorts() {
  return new Promise((resolve) => {
    const script =
      "Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | " +
      "Select-Object LocalPort, LocalAddress, OwningProcess | ConvertTo-Json -Compress";
    execFile(
      'powershell.exe',
      ['-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-Command', script],
      { timeout: CHECK_TIMEOUT_MS, windowsHide: true },
      (err, stdout) => {
        if (err || !stdout?.trim()) return resolve(null); // couldn't check — not a finding, just unknown this round
        try {
          const parsed = JSON.parse(stdout);
          const rows = Array.isArray(parsed) ? parsed : [parsed];
          resolve(rows.map((r) => ({ port: r.LocalPort, address: r.LocalAddress, pid: r.OwningProcess })).filter((r) => r.port));
        } catch {
          resolve(null);
        }
      }
    );
  });
}

function readState() {
  const row = getDb().prepare('SELECT value FROM app_state WHERE key = ?').get(STATE_KEY);
  if (!row) return { knownPorts: [] };
  try {
    const parsed = JSON.parse(row.value);
    return Array.isArray(parsed?.knownPorts) ? parsed : { knownPorts: [] };
  } catch {
    return { knownPorts: [] };
  }
}

function writeState(state) {
  getDb()
    .prepare('INSERT INTO app_state (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value')
    .run(STATE_KEY, JSON.stringify(state));
}

export const id = 'security-listeners';

export async function probe() {
  const current = await listListeningPorts();
  if (current === null) return { ok: true }; // couldn't check this round — never a false finding from our own tooling failing

  const currentPorts = [...new Set(current.map((r) => r.port))].filter((p) => p !== ownPort());
  const state = readState();
  const isFirstRun = state.knownPorts.length === 0 && !state.everChecked;

  const newPorts = isFirstRun ? [] : currentPorts.filter((p) => !state.knownPorts.includes(p));

  writeState({ knownPorts: currentPorts, everChecked: true });

  if (newPorts.length) {
    const withProcess = newPorts.map((p) => {
      const match = current.find((r) => r.port === p);
      return `${p}${match?.pid ? ` (pid ${match.pid})` : ''}`;
    });
    return { ok: false, detail: `New local listening port(s) since the last check: ${withProcess.join(', ')}.` };
  }
  return { ok: true };
}

// Deliberately NO remedy() — detection only.

// Picks the strongest code-isolation backend actually available on this
// machine, checked in order: wsl (a real Linux distro is installed) ->
// windows-sandbox (WindowsSandbox.exe present) -> restricted (a plain,
// explicitly weaker fallback — see restricted-backend.js's own doc
// comment). Nothing here launches anything long-lived; this only answers
// "which backend should runner.js use right now."
//
// wsl.exe writes UTF-16LE to stdout/stderr on Windows, not UTF-8 — decoding
// with Node's default 'utf8' produces a null byte after every character
// (confirmed live: "Windows Subsystem..." came back as "W\0i\0n\0d\0o\0w\0s...").
// Always request a raw Buffer and decode it as 'utf16le' ourselves.

import { execFile } from 'node:child_process';
import fs from 'node:fs';

const WSL_TIMEOUT_MS = 5000;
const WINDOWS_SANDBOX_PATH = 'C:\\Windows\\System32\\WindowsSandbox.exe';

function runWsl(args) {
  return new Promise((resolve) => {
    execFile('wsl.exe', args, { encoding: 'buffer', timeout: WSL_TIMEOUT_MS, windowsHide: true }, (err, stdout, stderr) => {
      resolve({
        // A real exit code (even a non-zero one, e.g. "no distributions
        // installed") means wsl.exe ran and answered. A missing `.code`
        // (spawn itself failed — ENOENT, etc.) means wsl.exe isn't usable
        // at all on this machine.
        ranOk: !err || typeof err.code === 'number',
        stdout: stdout ? stdout.toString('utf16le') : '',
        stderr: stderr ? stderr.toString('utf16le') : '',
      });
    });
  });
}

/** Every installed WSL distro name, or an empty list if WSL itself isn't usable or has none installed. Exported for direct testing, per this file's pure-logic-module pattern. */
export async function listWslDistros() {
  const result = await runWsl(['-l', '-q']); // -q: names only, no header/state columns to parse
  if (!result.ranOk) return [];
  return result.stdout
    .split(/\r?\n/)
    .map((line) => line.trim())
    // wsl.exe's -q output can still include a trailing empty line, and (on
    // some builds) a lone "*" marking the default distro on its own line —
    // neither is a real distro name.
    .filter((line) => line && line !== '*');
}

/** {backend: 'wsl'|'windows-sandbox'|'restricted', distro?, distros?} — the one call sandbox/runner.js and GET /api/sandbox/status both use. */
export async function detectBackend() {
  const distros = await listWslDistros();
  if (distros.length) {
    return { backend: 'wsl', distro: distros[0], distros };
  }
  if (fs.existsSync(WINDOWS_SANDBOX_PATH)) {
    // Detected honestly, but see runner.js: there is no working
    // windows-sandbox execution backend yet (Windows Sandbox has no
    // scriptable "run this and hand back stdout" story the way `wsl -d
    // <distro> -- <cmd>` does — it boots a full disposable VM per run and
    // needs a shared-folder + startup-script + completion-signal dance to
    // automate at all). Rather than ship an unverified runner for a backend
    // this machine can't even exercise, detection reports it truthfully and
    // runner.js falls through to `restricted` with a clear note, the same
    // "don't claim something works on faith" rule this project already
    // applies to the connector catalog (see CLAUDE.md's Figma entry).
    return { backend: 'windows-sandbox' };
  }
  return { backend: 'restricted' };
}

/** Plain-language steps shown on the fallback — see GET /api/sandbox/status. */
export function wslSetupSteps() {
  return [
    'Open Command Prompt or PowerShell as Administrator.',
    'Type: wsl --install',
    'Restart your computer when it asks.',
    "After restarting, a window will open to finish setup — pick a username and password for it (this is separate from your Windows login, and you won't need to remember it often).",
    "That's it — Jarvis will automatically start using it next time.",
  ];
}

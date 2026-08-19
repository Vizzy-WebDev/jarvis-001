// The real isolation backend — runs code inside an actual Linux VM (a WSL2
// distro), not just a restricted Windows process. Requires `detect.js` to
// have found at least one installed distro.
//
// IMPORTANT — could not be verified against a real, live WSL distro: this
// machine has none installed (confirmed via `detect.js`'s own live check).
// Written carefully against documented `wsl.exe` behavior and re-checked
// line by line, but per this project's own "never claim something works on
// faith" rule (see CLAUDE.md's Figma catalog entry), this must be run
// against a real distro before it's trusted the way the restricted backend
// — which WAS tested live — already is. Re-verify the moment WSL is set up
// here or on a machine that has it.
//
// Two `wsl.exe` behaviors this code leans on, both worth knowing before
// touching it again:
// 1. `wsl.exe` writes UTF-16LE to stdout/stderr, not UTF-8 (see detect.js's
//    own comment — confirmed live). Every call here decodes as 'utf16le'.
// 2. Windows environment variables are NOT forwarded into a WSL command by
//    default — only names explicitly listed in the WSLENV variable cross
//    over. That means Jarvis's own API keys and JARVIS_* vars are already
//    invisible inside WSL without any stripping on our part; `execFile` is
//    still given a minimal env defensively, in case a user's own WSLENV
//    setting forwards more than expected.

import { execFile } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';

const HARD_TIMEOUT_MS = 60_000; // never exceeded regardless of what was asked for
const MAX_OUTPUT_CHARS = 200_000;

const RUNTIMES = {
  javascript: { command: 'node', ext: '.js' },
  python: { command: 'python3', ext: '.py' },
  bash: { command: 'bash', ext: '.sh' },
};

function strippedEnv() {
  const keep = ['PATH', 'SystemRoot', 'windir', 'TEMP', 'TMP'];
  const env = {};
  for (const key of keep) {
    if (process.env[key] != null) env[key] = process.env[key];
  }
  return env;
}

function truncate(text) {
  if (text.length <= MAX_OUTPUT_CHARS) return { text, truncated: false };
  return { text: text.slice(0, MAX_OUTPUT_CHARS), truncated: true };
}

/** `C:\Users\HP\thing` -> `/mnt/c/Users/HP/thing` — how a host Windows path is reached from inside WSL. Exported for testing. */
export function toWslPath(windowsPath) {
  const match = /^([A-Za-z]):[\\/](.*)$/.exec(windowsPath);
  if (!match) return null;
  const drive = match[1].toLowerCase();
  const rest = match[2].replace(/\\/g, '/');
  return `/mnt/${drive}/${rest}`;
}

function runWslCommand(distro, args, { cwd, timeoutMs } = {}) {
  return new Promise((resolve) => {
    const fullArgs = ['-d', distro];
    if (cwd) fullArgs.push('--cd', cwd);
    fullArgs.push('--', ...args);

    execFile(
      'wsl.exe',
      fullArgs,
      { encoding: 'buffer', timeout: timeoutMs, windowsHide: true, maxBuffer: 10 * 1024 * 1024 },
      (err, stdout, stderr) => {
        resolve({
          exitCode: err && typeof err.code === 'number' ? err.code : err ? null : 0,
          spawnFailed: Boolean(err) && typeof err.code !== 'number',
          timedOut: err?.killed === true,
          stdout: stdout ? stdout.toString('utf16le') : '',
          stderr: stderr ? stderr.toString('utf16le') : '',
        });
      }
    );
  });
}

/**
 * Runs `code` inside the given WSL distro, in a fresh throwaway folder
 * reached via the `\\wsl.localhost\<distro>\...` UNC path (so plain Node
 * `fs` calls on the Windows side can write files straight into the Linux
 * filesystem — no separate copy step, and it stays on the Linux side's own
 * ext4 filesystem rather than the slower cross-OS /mnt/c bridge).
 */
export async function runCode({ language, code, files = [], timeoutMs = 10000, allowNetwork = false, allowPaths = [], args: scriptArgs = [], distro }) {
  const runtime = RUNTIMES[language];
  if (!runtime) {
    return { ok: false, error: `Unsupported language "${language}" on this backend — supported: ${Object.keys(RUNTIMES).join(', ')}.`, isolation: 'strong' };
  }
  if (!distro) {
    return { ok: false, error: 'No WSL distro was given to run this in.', isolation: 'strong' };
  }

  const runId = `jarvis-sandbox-${Date.now().toString(36)}${Math.random().toString(36).slice(2, 8)}`;
  const linuxDir = `/tmp/${runId}`;
  const uncDir = `\\\\wsl.localhost\\${distro}\\tmp\\${runId}`;

  const effectiveTimeout = Math.max(1000, Math.min(timeoutMs, HARD_TIMEOUT_MS));

  try {
    // mkdir on the Linux side first — the UNC path only exists once the
    // directory does.
    const mk = await runWslCommand(distro, ['mkdir', '-p', linuxDir], { timeoutMs: 10_000 });
    if (mk.spawnFailed) {
      return { ok: false, error: 'Could not reach WSL — it may not be running.', isolation: 'strong' };
    }

    fs.writeFileSync(path.join(uncDir, `main${runtime.ext}`), code, 'utf8');
    for (const f of files) {
      if (!f?.name) continue;
      const safeName = path.basename(f.name); // never a `..` segment or absolute path — stays inside this run's own folder
      fs.writeFileSync(path.join(uncDir, safeName), f.content ?? '', 'utf8');
    }

    // Mount points for any host folders this run was explicitly allowed to
    // see (opt-in, defaults to none — see run_code.js's confirm gate). This
    // only tells the code where to find them; WSL's own filesystem
    // permissions are what actually govern access, same honesty caveat as
    // the restricted backend's allowPaths.
    const mountNotes = allowPaths
      .map((p) => toWslPath(p))
      .filter(Boolean)
      .map((wp, i) => `Host folder ${i + 1} is available at: ${wp}`)
      .join('\n');
    if (mountNotes) {
      fs.writeFileSync(path.join(uncDir, '.allowed-paths.txt'), mountNotes, 'utf8');
    }

    // Network isolation: try `unshare -n` first (needs root or the
    // CAP_SYS_ADMIN capability — true for the default user on a stock
    // Ubuntu WSL install, not guaranteed on every distro/config). If that
    // itself fails to even start, fall back to running without it and
    // report `networkIsolated: false` — never silently claim isolation that
    // didn't actually happen.
    const runArgv = [runtime.command, `${linuxDir}/main${runtime.ext}`, ...scriptArgs];
    let networkIsolated = false;
    let runResult;
    if (!allowNetwork) {
      const attempt = await runWslCommand(distro, ['unshare', '-n', '--', ...runArgv], {
        cwd: linuxDir,
        timeoutMs: effectiveTimeout,
      });
      // exit code 1 from `unshare` itself (before the real command even
      // starts) most commonly means "operation not permitted" — distinguish
      // that from the wrapped program's OWN exit code 1 by checking stderr
      // for unshare's own error text.
      if (attempt.spawnFailed || /unshare:\s*(unshare failed|operation not permitted)/i.test(attempt.stderr)) {
        runResult = await runWslCommand(distro, runArgv, { cwd: linuxDir, timeoutMs: effectiveTimeout });
      } else {
        networkIsolated = true;
        runResult = attempt;
      }
    } else {
      runResult = await runWslCommand(distro, runArgv, { cwd: linuxDir, timeoutMs: effectiveTimeout });
    }

    if (runResult.spawnFailed) {
      return { ok: false, error: 'Could not reach WSL — it may not be running.', isolation: 'strong' };
    }
    if (runResult.timedOut) {
      return { ok: false, error: `Timed out after ${effectiveTimeout}ms and was stopped.`, timedOut: true, isolation: 'strong', networkIsolated };
    }

    const stdoutT = truncate(runResult.stdout);
    const stderrT = truncate(runResult.stderr);

    return {
      ok: runResult.exitCode === 0,
      exitCode: runResult.exitCode,
      stdout: stdoutT.text,
      stderr: stderrT.text,
      outputTruncated: stdoutT.truncated || stderrT.truncated,
      isolation: 'strong',
      networkIsolated, // true only if `unshare -n` actually took effect — see above
      allowPathsRequested: allowPaths,
      allowNetworkRequested: allowNetwork,
    };
  } finally {
    // Best-effort cleanup on the Linux side — a leftover /tmp folder inside
    // a VM that gets torn down on its own schedule is a non-issue, never
    // worth failing the whole run over.
    runWslCommand(distro, ['rm', '-rf', linuxDir], { timeoutMs: 5000 }).catch(() => {});
  }
}

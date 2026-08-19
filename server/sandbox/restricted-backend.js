// The fallback backend — used whenever `detect.js` couldn't find a real WSL
// distro (and, for now, also whenever it found `windows-sandbox`; see
// runner.js's own comment on why that tier has no working runner yet).
//
// THIS IS NOT A REAL SECURITY BOUNDARY. It is a plain child process on the
// same Windows account as everything else Jarvis does — a stripped
// environment and a fresh temp folder keep an honest script from
// accidentally touching Jarvis's own secrets or files, but nothing here
// stops deliberately hostile code from reading the rest of the disk,
// reaching the network, or outliving its timeout by spawning its own
// children. Every result this backend returns carries `isolation: 'weak'`
// specifically so nothing upstream (a skill, a UI label) can present it as
// more than it is — see `detect.js`'s `wslSetupSteps()` for the real fix.

import { spawn, execFile } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';

const MAX_OUTPUT_CHARS = 200_000; // generous for real debugging output, small enough to never flood a reply

const RUNTIMES = {
  javascript: { command: process.execPath, ext: '.js', args: (file, scriptArgs) => [file, ...scriptArgs] },
  python: { command: 'python', ext: '.py', args: (file, scriptArgs) => [file, ...scriptArgs] },
};

// A minimal env for the child process — PATH and the handful of vars a
// normal interpreter needs to start, nothing from Jarvis's own process (API
// keys, JARVIS_* overrides, etc.). Confirmed live: a `JARVIS_TEST_SECRET`
// set on the parent process does NOT reach the child. What's NOT guaranteed
// stripped, also confirmed live: Windows still surfaces a few of its own
// per-logon-session identity variables (USERNAME, USERPROFILE, USERDOMAIN,
// LOGONSERVER, SYSTEMDRIVE) in the child even though they're not in `keep`
// below — this looks like CreateProcessW-level OS behavior, not anything
// this code's `env` option controls. None of those are secrets (the
// Windows username is already visible in ordinary file paths throughout
// this project), so it doesn't undermine the actual goal, but don't
// describe this as a fully custom/blank environment — it isn't one.
function strippedEnv() {
  const keep = ['PATH', 'SystemRoot', 'windir', 'TEMP', 'TMP', 'ComSpec', 'PATHEXT', 'HOMEDRIVE', 'HOMEPATH'];
  const env = {};
  for (const key of keep) {
    if (process.env[key] != null) env[key] = process.env[key];
  }
  return env;
}

/** Kills the whole process tree, not just the immediate child — a runaway script that spawns its own children would otherwise survive the timeout. `taskkill /t` is the Windows equivalent of killing a process group. */
function killTree(pid) {
  execFile('taskkill', ['/pid', String(pid), '/t', '/f'], () => {});
}

function truncate(text) {
  if (text.length <= MAX_OUTPUT_CHARS) return { text, truncated: false };
  return { text: text.slice(0, MAX_OUTPUT_CHARS), truncated: true };
}

/**
 * Runs `code` in a fresh throwaway folder under the OS temp dir, using a
 * plain Node/Python child process — see the file-level warning above for
 * what this backend does and does not protect against.
 */
export async function runCode({ language, code, files = [], timeoutMs = 10000, allowNetwork = false, allowPaths = [], args: scriptArgs = [] }) {
  const runtime = RUNTIMES[language];
  if (!runtime) {
    return {
      ok: false,
      error: `Unsupported language "${language}" on this backend — supported: ${Object.keys(RUNTIMES).join(', ')}.`,
      isolation: 'weak',
    };
  }

  const runId = `jarvis-sandbox-${Date.now().toString(36)}${Math.random().toString(36).slice(2, 8)}`;
  const workDir = path.join(os.tmpdir(), runId);
  fs.mkdirSync(workDir, { recursive: true });

  try {
    const mainFile = path.join(workDir, `main${runtime.ext}`);
    fs.writeFileSync(mainFile, code, 'utf8');
    for (const f of files) {
      if (!f?.name) continue;
      // No `..` segments or absolute paths — every extra file lands inside
      // this run's own throwaway folder, never escapes it.
      const safeName = path.basename(f.name);
      fs.writeFileSync(path.join(workDir, safeName), f.content ?? '', 'utf8');
    }

    const result = await new Promise((resolve) => {
      let stdout = '';
      let stderr = '';
      let timedOut = false;

      const child = spawn(runtime.command, runtime.args(mainFile, scriptArgs), {
        cwd: workDir,
        env: strippedEnv(),
        windowsHide: true,
      });

      const timer = setTimeout(() => {
        timedOut = true;
        killTree(child.pid);
      }, Math.max(1000, Math.min(timeoutMs, 60_000))); // hard-capped at 60s regardless of what was asked for

      child.stdout.on('data', (chunk) => { stdout += chunk.toString(); });
      child.stderr.on('data', (chunk) => { stderr += chunk.toString(); });

      child.on('error', (err) => {
        clearTimeout(timer);
        resolve({ ok: false, error: err?.message || 'Could not start the interpreter.', exitCode: null });
      });

      child.on('close', (exitCode) => {
        clearTimeout(timer);
        if (timedOut) {
          resolve({ ok: false, error: `Timed out after ${timeoutMs}ms and was stopped.`, exitCode: null, timedOut: true });
        } else {
          resolve({ ok: exitCode === 0, exitCode, stdout, stderr });
        }
      });
    });

    const stdoutT = truncate(result.stdout || '');
    const stderrT = truncate(result.stderr || '');

    return {
      ok: Boolean(result.ok),
      exitCode: result.exitCode ?? null,
      stdout: stdoutT.text,
      stderr: stderrT.text,
      outputTruncated: stdoutT.truncated || stderrT.truncated,
      error: result.error,
      timedOut: Boolean(result.timedOut),
      isolation: 'weak',
      // Neither is actually enforced on this backend — reported honestly so
      // nothing upstream claims a guarantee this backend can't back up.
      // `allowPaths` isn't restricted at all (the child runs as the same
      // Windows account as everything else); `allowNetwork` isn't blocked
      // either (there is no simple per-process firewall without elevated
      // privileges). Real enforcement of both is what the `wsl` backend is
      // for.
      warnings: [
        'This machine has no WSL distro set up, so this ran with weak isolation: nothing actually blocked network access or restricted which files this code could reach.',
      ],
      allowPathsRequested: allowPaths,
      allowNetworkRequested: allowNetwork,
    };
  } finally {
    try {
      fs.rmSync(workDir, { recursive: true, force: true });
    } catch {
      // Best-effort cleanup — a leftover temp folder is a minor annoyance, not a failure worth surfacing.
    }
  }
}

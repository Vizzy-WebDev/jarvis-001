// The CLI mechanism: a command-line tool already installed on the user's PC,
// reached by actually running it. One of Jarvis's three peer integration
// mechanisms (alongside mcp and api) — see connectors/store.js's header
// comment. Runs on the host by design (connecting to a local tool is the
// whole point), not inside the Stage 4 sandbox.
//
// **Never a raw shell string, anywhere in this file.** A tool is a fixed
// argv template (`command.argv`, e.g. `['status', '--branch', '{branch}']`)
// with named placeholders; a call only ever substitutes a declared
// placeholder with the exact value supplied for it, one argv entry each, and
// runs via `spawn(command, argv, {shell: false})` — the same discipline as
// `skills/open_app.js`'s allowlist. Nothing about a call can ever inject an
// extra flag or chain a second command; only the literal, saved template
// shape can run.
//
// Discovery (`discoverCommands`) runs the real tool's own `--help` and asks
// a model to turn that real output into a proposed list of subcommands —
// the model never invents an executable command directly, it only proposes
// data (name/description/argv template/args) that the user reviews, edits,
// and ticks before anything is saved. This is what makes a CLI connector's
// tools "reflect what that connector actually provides" rather than a guess.

import { spawn } from 'node:child_process';
import { askModel } from '../ai.js';

const HELP_TIMEOUT_MS = 10 * 1000;
const RUN_TIMEOUT_MS = 30 * 1000;
const MAX_OUTPUT_BYTES = 200 * 1000;

function runCapture(command, argv, { cwd, timeoutMs } = {}) {
  return new Promise((resolve) => {
    let child;
    try {
      child = spawn(command, argv, { cwd: cwd || undefined, shell: false, windowsHide: true });
    } catch (err) {
      resolve({ ok: false, error: `Couldn't run "${command}" — is it installed and on your PATH? (${err?.message || 'unknown error'})` });
      return;
    }

    let out = '';
    let err = '';
    let settled = false;
    const finish = (result) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve(result);
    };

    const timer = setTimeout(() => {
      try {
        child.kill();
      } catch {
        // Already exiting — fine either way.
      }
      finish({ ok: false, error: `"${command}" took too long to respond and was stopped.` });
    }, timeoutMs || RUN_TIMEOUT_MS);

    child.stdout?.on('data', (chunk) => {
      if (out.length < MAX_OUTPUT_BYTES) out += chunk.toString();
    });
    child.stderr?.on('data', (chunk) => {
      if (err.length < MAX_OUTPUT_BYTES) err += chunk.toString();
    });
    child.on('error', (spawnErr) => {
      finish({ ok: false, error: `Couldn't run "${command}" — is it installed and on your PATH? (${spawnErr?.message || 'unknown error'})` });
    });
    child.on('close', (code) => {
      finish({ ok: true, code, stdout: out.slice(0, MAX_OUTPUT_BYTES), stderr: err.slice(0, MAX_OUTPUT_BYTES) });
    });
  });
}

const DISCOVERY_SYSTEM =
  'You turn a command-line program\'s own --help output into a small, safe list of subcommands Jarvis could offer as tools. ' +
  'Reply with JSON only: {"commands": [{"name": "short_snake_case_name", "description": "one plain sentence", ' +
  '"argv": ["literal-or-{placeholder}", ...], "args": [{"name": "placeholder", "description": "..."}]}]}. ' +
  'Rules: only propose subcommands that actually appear in the given help text — never invent one. ' +
  'Every "{placeholder}" in argv must have a matching entry in "args". Keep each argv entry a single literal word or ' +
  'flag, or a single {placeholder} — never a shell operator, pipe, redirect, or semicolon, and never combine ' +
  'multiple words or flags into one argv entry. Propose at most 12 of the most generally useful subcommands.';

/**
 * Runs `<command> --help`, then asks a model to turn the real output into a
 * proposed subcommand list — for the user to review/edit/tick before saving.
 * Returns `{helpText, proposed}`; never writes anything to the connector
 * record itself (server.js's route does that once the user confirms).
 */
export async function discoverCommands(command) {
  const help = await runCapture(command, ['--help'], { timeoutMs: HELP_TIMEOUT_MS });
  if (!help.ok) throw new Error(help.error);
  const helpText = (help.stdout + '\n' + help.stderr).trim();
  if (!helpText) throw new Error(`"${command}" ran but printed no help text to read from.`);

  const result = await askModel({
    prompt: `Here is the real --help output for "${command}":\n\n${helpText.slice(0, 6000)}`,
    system: DISCOVERY_SYSTEM,
    json: true,
  });
  if (!result.ok) throw new Error(result.error || 'Could not turn that help text into a subcommand list.');
  const proposed = Array.isArray(result.data?.commands) ? result.data.commands : [];
  return { helpText, proposed };
}

/** `{name, description, parameters}` per saved, enabled command. */
export function toolDeclarations(config) {
  return (config?.commands || []).map((cmd) => {
    const properties = {};
    const required = [];
    for (const a of cmd.args || []) {
      properties[a.name] = { type: 'string', description: a.description || undefined };
      if (a.required !== false) required.push(a.name);
    }
    return {
      name: cmd.name,
      description: cmd.description || `Runs "${config.command} ${(cmd.argv || []).join(' ')}".`,
      parameters: { type: 'object', properties, required },
    };
  });
}

/** Runs one saved command by name, substituting its argv template's placeholders with the supplied args. Throws on failure — callers (connectors/index.js) already catch and wrap. */
export async function dispatch(name, args, config) {
  const cmd = (config?.commands || []).find((c) => c.name === name);
  if (!cmd) throw new Error(`Unknown CLI command: ${name}`);

  const finalArgv = [];
  for (const entry of cmd.argv || []) {
    const match = /^\{([^}]+)\}$/.exec(entry);
    if (!match) {
      finalArgv.push(entry); // a literal argv word/flag from the saved template — never user-influenced
      continue;
    }
    const key = match[1];
    const value = args?.[key];
    if (value === undefined || value === '') {
      throw new Error(`Missing required value "${key}" for this command.`);
    }
    finalArgv.push(String(value));
  }

  const result = await runCapture(config.command, finalArgv, { cwd: config.cwd });
  if (!result.ok) throw new Error(result.error);
  if (result.code !== 0) {
    throw new Error(`"${config.command}" exited with an error (code ${result.code}): ${result.stderr || result.stdout || 'no output'}`);
  }
  return { ok: true, stdout: result.stdout, stderr: result.stderr };
}

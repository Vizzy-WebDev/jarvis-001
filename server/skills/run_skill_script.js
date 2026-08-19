// Skill: runs one of an installed Skill's own helper scripts through the
// sandbox (server/sandbox/) and returns its output. This is the one
// deliberate exception to store/skill-files.js/read_skill_file.js's "Jarvis
// only ever reads a Skill's files, never executes them" rule — see CLAUDE.md for
// why that rule exists (the scheduler/briefing run unattended with the
// confirm gate switched off, so "run it, ask first" would silently skip
// asking). This is the "separate, deliberately-designed feature" that rule
// pointed at.
//
// Approval is per-SKILL, not per-call: the first attempt for a given Skill
// fails with a plain explanation and the sandbox never runs; only after the
// user agrees and the model calls approve_skill_scripts does
// `scriptsApproved` get set, and every later call for that same Skill runs
// immediately with no further prompt. This mirrors allow_folder.js's own
// "ask once in conversation, remembered after" shape — deliberately NOT the
// generic per-call confirm:'always' gate (which would ask again on every
// single script run; the plan calls for "confirm once, then remembered").

import path from 'node:path';
import { getSkill, resolveSkillPath, readSkillFile, listSkillFiles } from './store/skill-files.js';
import { runCode } from '../sandbox/runner.js';

const EXT_TO_LANGUAGE = {
  '.js': 'javascript',
  '.mjs': 'javascript',
  '.py': 'python',
  '.sh': 'bash', // only the wsl backend can actually run this — see runner.js; restricted-backend reports it as unsupported rather than silently failing
};
const DEFAULT_TIMEOUT_MS = 15_000;
const MAX_SIBLING_FILES = 20; // a script needing more local files than this is unusual — cap rather than stuff an unbounded folder into one sandbox call

export default {
  name: 'run_skill_script',
  description:
    "Run one of an installed Skill's own helper scripts (from its folder) through Jarvis's sandbox and get back " +
    "its output. Only use a script path a Skill's own instructions actually pointed you to — never guess one. " +
    "The first use for a given Skill needs the user's one-time approval; later runs of that same Skill's " +
    "scripts don't ask again.",
  parameters: {
    type: 'object',
    properties: {
      skill: { type: 'string', description: 'The Skill this script belongs to (its tool name).' },
      path: { type: 'string', description: 'The script\'s path relative to that Skill\'s folder, e.g. "scripts/build.py".' },
      args: { type: 'array', items: { type: 'string' }, description: "Optional arguments the script expects, if its instructions mention any." },
    },
    required: ['skill', 'path'],
  },
  async run({ skill: skillName, path: relPath, args: scriptArgs }) {
    const skill = getSkill(skillName);
    if (!skill) return { ok: false, error: `Unknown skill: "${skillName}".` };

    const ext = path.extname(String(relPath || '')).toLowerCase();
    const language = EXT_TO_LANGUAGE[ext];
    if (!language) {
      return { ok: false, error: `"${relPath}" isn't a script type Jarvis can run (supported: ${Object.keys(EXT_TO_LANGUAGE).join(', ')}).` };
    }

    if (!skill.scriptsApproved) {
      return {
        ok: false,
        needsApproval: true,
        error:
          `Running "${skill.name}"'s scripts needs the user's one-time OK first. Ask them if it's fine for this ` +
          "Skill to run its own scripts through Jarvis's sandbox, and if they agree, call approve_skill_scripts " +
          'for this skill, then try this again.',
      };
    }

    let scriptContent;
    try {
      resolveSkillPath(skillName, relPath); // throws first on an escaping path, before anything is read
      scriptContent = readSkillFile(skillName, relPath);
    } catch (err) {
      return { ok: false, error: err?.message || `Could not read "${relPath}" from that skill.` };
    }

    // Sibling files ride along in case the script imports/reads them — the
    // same folder-relative shape it'd see run directly from disk. A file
    // that fails to read (too large, not text) is skipped rather than
    // failing the whole run over one unreadable asset.
    const siblingFiles = [];
    for (const rel of listSkillFiles(skillName).slice(0, MAX_SIBLING_FILES)) {
      if (rel === relPath) continue;
      try {
        siblingFiles.push({ name: path.basename(rel), content: readSkillFile(skillName, rel) });
      } catch {
        // Skip — the script can still run without it.
      }
    }

    const result = await runCode({
      language,
      code: scriptContent,
      files: siblingFiles,
      args: Array.isArray(scriptArgs) ? scriptArgs.map(String) : [],
      timeoutMs: DEFAULT_TIMEOUT_MS,
      allowNetwork: false,
      allowPaths: [],
    });

    return {
      ok: result.ok,
      stdout: result.stdout,
      stderr: result.stderr || undefined,
      exitCode: result.exitCode,
      error: result.error,
      timedOut: result.timedOut || undefined,
      isolation: result.isolation,
      warnings: result.warnings,
    };
  },
};

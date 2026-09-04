// Unpack/pack a Skill upload — the Upload, Replace, and Download actions on
// the Skills screen. Shells out to PowerShell's Expand-Archive/
// Compress-Archive for the zip half, same as the old skills-fs.js did: Node
// has no built-in zip support and this project deliberately runs on 4
// dependencies (no new one for this). Array args throughout, nothing built
// from string interpolation, matching skills/open_app.js's launch
// discipline.
//
// Claude's real upload dialog accepts three file types — `.zip`, `.skill`
// (confirmed, from two independent sources, to be nothing but a `.zip`
// renamed — same internal requirement), and a bare `.md` file (a skill with
// no extra resources, just SKILL.md's own text). A raw-body upload
// (`express.raw`, see server.js) never reliably carries the browser's
// original filename — and adding a multipart-parser dependency just to read
// one would break the no-new-dependency rule above — so which of the three
// this is gets decided by content, not by trusting an extension: a real ZIP
// archive always starts with a fixed 4-byte signature; anything else is
// decoded as plain text and treated as a bare SKILL.md.
//
// A dependency-free leaf module (only spawns a process + touches fs) — see
// skill-files.js's header comment for the circular-import invariant this
// keeps.

import fs from 'node:fs';
import path from 'node:path';
import { spawn } from 'node:child_process';
import { dataDir } from '../../store.js';
import { installFromDirectory, replaceSkillContents, installFromMarkdown, replaceSkillMarkdown } from './skill-files.js';

// ZIP local-file-header signature (PK\x03\x04) — every real .zip/.skill
// starts with this. PK\x05\x06 is the End-Of-Central-Directory signature an
// EMPTY archive starts with instead (no local file header at all) — included
// for completeness, though an empty archive would fail the "has a SKILL.md"
// check moments later regardless.
const ZIP_SIGNATURES = [
  Buffer.from([0x50, 0x4b, 0x03, 0x04]),
  Buffer.from([0x50, 0x4b, 0x05, 0x06]),
];

function looksLikeZip(buffer) {
  return Boolean(buffer?.length >= 4) && ZIP_SIGNATURES.some((sig) => buffer.subarray(0, 4).equals(sig));
}

// How many wrapper-folder levels to tolerate before giving up — covers the
// single most common real-world mistake (zipping the folder itself instead
// of its contents, which real users hit often enough to be independently
// documented) plus a margin for a doubly-wrapped one (a GitHub zip export
// extracted then re-zipped, which nests it again), without walking a
// pathological or unrelated zip's directory tree forever.
const MAX_WRAPPER_DEPTH = 4;

function scratchDir() {
  const dir = path.join(dataDir(), 'skills', `.tmp-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`);
  fs.mkdirSync(dir, { recursive: true });
  return dir;
}

// A real, confirmed live bug: `-Command` followed by SEVERAL SEPARATE argv
// entries (as this used to pass them: ['-Command', 'Expand-Archive',
// '-LiteralPath', tmpZip, ...]) is not "one command with flags" the way it
// looks — powershell.exe's own CLI parser takes only the token immediately
// after `-Command` as the command name and re-interprets every token AFTER
// that as trailing "CommandParameters" it reconstructs itself, which does
// NOT reliably preserve a single argv item (like a path) as one atomic
// value once it contains a space. This project's own folder path
// (`...\CLAUDE PROJECT\Jarvis-001\...`) has exactly that space, and it
// broke `Expand-Archive` outright: "A positional parameter cannot be found
// that accepts argument '...zip'" — `-LiteralPath` itself was never
// recognized as a flag once the path split apart. Confirmed live via the
// new install-from-GitHub-repo feature (which reuses this same function
// unchanged), but this affects every zip Upload/Replace on this exact class
// of machine (any install path with a space in it) — not new, just newly
// surfaced. Fixed by building ONE single, fully-formed command STRING
// ourselves (each value individually single-quoted, embedded `'` doubled
// per PowerShell's own escaping rule) and passing exactly one argv item
// after `-Command` — powershell.exe then parses it as one ordinary command
// line, the same as typing it at a prompt, with no trailing-parameter
// reinterpretation to go wrong.
function quotePwshArg(value) {
  return `'${String(value).replace(/'/g, "''")}'`;
}

function runPowerShell(args) {
  return new Promise((resolve, reject) => {
    // The FIRST token is the cmdlet name itself (e.g. "Expand-Archive") and
    // must stay bare — quoting it turns the whole line into a plain string
    // expression instead of a command invocation, which is its own distinct
    // parse failure ("Unexpected token '-LiteralPath'..."), confirmed live
    // by this fix's own first attempt. Every token AFTER that is either a
    // real flag (starts with '-', stays unquoted) or a value (quoted).
    const [cmd, ...rest] = args;
    const commandString = [cmd, ...rest.map((a) => (/^-/.test(a) ? a : quotePwshArg(a)))].join(' ');
    const ps = spawn('powershell.exe', ['-NoProfile', '-NonInteractive', '-Command', commandString], { stdio: ['ignore', 'ignore', 'pipe'] });
    let stderr = '';
    ps.stderr.on('data', (chunk) => { stderr += chunk; });
    ps.on('error', (err) => reject(new Error(err.message)));
    ps.on('exit', (code) => {
      if (code !== 0) reject(new Error(stderr.trim() || 'PowerShell command failed.'));
      else resolve();
    });
  });
}

/**
 * Walks down through wrapper folders looking for the one that actually
 * holds SKILL.md and/or skill.toml (either alone is a valid Skill folder —
 * see skill-files.js's isSkillFolder()) — handling the single most common
 * real-world zip mistake (zipping the folder itself instead of its
 * contents, sometimes more than once, e.g. a GitHub zip export that was
 * extracted and then re-zipped by hand). Only descends while a directory
 * contains exactly one subfolder and neither file of its own — an archive
 * with multiple top-level items is left alone rather than guessed at, and
 * the walk gives up after `MAX_WRAPPER_DEPTH` levels either way.
 */
function findSkillRoot(startDir) {
  let dir = startDir;
  for (let depth = 0; depth < MAX_WRAPPER_DEPTH; depth++) {
    if (fs.existsSync(path.join(dir, 'SKILL.md')) || fs.existsSync(path.join(dir, 'skill.toml'))) return dir;
    const entries = fs.readdirSync(dir, { withFileTypes: true });
    const dirs = entries.filter((e) => e.isDirectory());
    const files = entries.filter((e) => !e.isDirectory());
    if (dirs.length !== 1 || files.length !== 0) return dir; // ambiguous or nothing left to descend into — stop and let the caller report "no SKILL.md or skill.toml"
    dir = path.join(dir, dirs[0].name);
  }
  return dir;
}

/** Unpacks a zip buffer into a fresh scratch folder and returns the directory that actually holds SKILL.md (see findSkillRoot() above). Caller must clean up the returned `cleanup()` when done. */
async function unpackToScratch(buffer) {
  const tmpDir = scratchDir();
  const tmpZip = `${tmpDir}.zip`;
  fs.writeFileSync(tmpZip, buffer);

  try {
    await runPowerShell(['Expand-Archive', '-LiteralPath', tmpZip, '-DestinationPath', tmpDir, '-Force']);
  } catch (err) {
    fs.rmSync(tmpDir, { recursive: true, force: true });
    fs.rmSync(tmpZip, { force: true });
    throw new Error(`Could not open that zip: ${err.message}`);
  }
  fs.rmSync(tmpZip, { force: true });

  return { sourceDir: findSkillRoot(tmpDir), cleanup: () => fs.rmSync(tmpDir, { recursive: true, force: true }) };
}

/** Upload — a brand-new skill from a `.zip`/`.skill` buffer (skill folder at the archive root, per Claude's own upload rule; see findSkillRoot() for how far a mis-zipped wrapper folder is tolerated). */
async function installFromZip(buffer, reservedNames = new Set()) {
  const { sourceDir, cleanup } = await unpackToScratch(buffer);
  try {
    return installFromDirectory(sourceDir, reservedNames);
  } finally {
    cleanup();
  }
}

// Matches "github.com/<owner>/<repo>" with an optional leading scheme,
// optional trailing slash/path/query, and an optional ".git" suffix — the
// shapes a user is actually likely to paste (a browser address bar URL, a
// git clone URL, or just "owner/repo" copied from somewhere).
const GITHUB_URL_RE = /^(?:https?:\/\/)?(?:www\.)?github\.com\/([\w.-]+)\/([\w.-]+?)(?:\.git)?(?:[/?#].*)?$/i;

/** Pulls {owner, repo} out of whatever the user pasted, or null if it doesn't look like a GitHub repo link at all. */
export function parseGithubRepoUrl(input) {
  const match = GITHUB_URL_RE.exec(String(input || '').trim());
  if (!match) return null;
  return { owner: match[1], repo: match[2] };
}

/**
 * Install — a brand-new skill fetched directly from a public GitHub
 * repository link, per the user's own explicit choice of "a GitHub
 * repository link" over "a direct file link." Two plain, unauthenticated
 * `fetch()` calls, no new dependency: first the repo's own metadata (just to
 * read its real default branch — never assumed to be "main"), then the
 * zipball for that branch. GitHub's own zipball always wraps its contents in
 * one top-level folder (`owner-repo-<sha>/`) — findSkillRoot() above already
 * unwraps exactly this shape (its own comment describes this precise case),
 * so no special-casing is needed here beyond reusing unpackToScratch(). A
 * repository holding more than one Skill in subfolders isn't supported —
 * same restriction installFromDirectory() already applies to a zip upload
 * (SKILL.md/skill.toml must resolve at the root, never searched for deeper).
 */
export async function installFromGithubRepo(url, reservedNames = new Set()) {
  const parsed = parseGithubRepoUrl(url);
  if (!parsed) throw new Error('That doesn\'t look like a GitHub repository link — try something like "github.com/someone/some-skill".');
  const { owner, repo } = parsed;

  let defaultBranch;
  try {
    const metaRes = await fetch(`https://api.github.com/repos/${owner}/${repo}`, {
      headers: { Accept: 'application/vnd.github+json' },
    });
    if (!metaRes.ok) {
      throw new Error(metaRes.status === 404 ? "That repository doesn't exist, or isn't public." : `GitHub responded with ${metaRes.status}.`);
    }
    const meta = await metaRes.json();
    defaultBranch = meta.default_branch;
    if (!defaultBranch) throw new Error("Couldn't work out that repository's default branch.");
  } catch (err) {
    throw new Error(err?.message || 'Could not reach GitHub.');
  }

  let buffer;
  try {
    const zipRes = await fetch(`https://api.github.com/repos/${owner}/${repo}/zipball/${defaultBranch}`);
    if (!zipRes.ok) throw new Error(`GitHub responded with ${zipRes.status} while downloading the repository.`);
    buffer = Buffer.from(await zipRes.arrayBuffer());
  } catch (err) {
    throw new Error(err?.message || 'Could not download that repository.');
  }

  return installFromZip(buffer, reservedNames);
}

/** Replace — swaps an existing skill's whole folder content for what's in a new `.zip`/`.skill`, same identity (folder name / enabled state) as before. */
async function replaceFromZip(name, buffer) {
  const { sourceDir, cleanup } = await unpackToScratch(buffer);
  try {
    return replaceSkillContents(name, sourceDir);
  } finally {
    cleanup();
  }
}

/**
 * Upload — the single entry point `POST /api/skills/upload` calls, for
 * whichever of Claude's three real formats the bytes turn out to be: a real
 * ZIP archive (covers both `.zip` and a `.skill` renamed copy of one — see
 * this file's header comment for why that's decided by content, not by a
 * claimed filename) unpacks and installs like any Skill folder; anything
 * else is decoded as text and installed as a bare SKILL.md.
 */
export async function installFromUpload(buffer, reservedNames = new Set()) {
  if (!buffer || !buffer.length) throw new Error('That upload was empty.');
  if (looksLikeZip(buffer)) return installFromZip(buffer, reservedNames);
  return installFromMarkdown(buffer.toString('utf8'), reservedNames);
}

/** Replace — the single entry point `POST /api/skills/:name/replace` calls, same content-sniffed dispatch as installFromUpload() above. */
export async function replaceFromUpload(name, buffer) {
  if (!buffer || !buffer.length) throw new Error('That upload was empty.');
  if (looksLikeZip(buffer)) return replaceFromZip(name, buffer);
  return replaceSkillMarkdown(name, buffer.toString('utf8'));
}

/** Download — packs a skill's current folder into a `.zip` buffer, folder-as-root (mirrors what Upload expects back in). */
export async function buildSkillZip(skillRootDir) {
  const tmpZip = path.join(dataDir(), 'skills', `.download-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}.zip`);
  try {
    // Compress the folder's *contents* at the zip root (trailing `\*`), not
    // the folder itself as a nested entry — matches what Upload/Replace
    // expect to unpack back (SKILL.md directly at the root).
    await runPowerShell(['Compress-Archive', '-Path', `${skillRootDir}\\*`, '-DestinationPath', tmpZip, '-Force']);
    return fs.readFileSync(tmpZip);
  } finally {
    fs.rmSync(tmpZip, { force: true });
  }
}

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

function runPowerShell(args) {
  return new Promise((resolve, reject) => {
    const ps = spawn('powershell.exe', ['-NoProfile', '-NonInteractive', '-Command', ...args], { stdio: ['ignore', 'ignore', 'pipe'] });
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
 * holds SKILL.md — handling the single most common real-world zip mistake
 * (zipping the folder itself instead of its contents, sometimes more than
 * once, e.g. a GitHub zip export that was extracted and then re-zipped by
 * hand). Only descends while a directory contains exactly one subfolder and
 * no SKILL.md of its own — an archive with multiple top-level items is left
 * alone rather than guessed at, and the walk gives up after
 * `MAX_WRAPPER_DEPTH` levels either way.
 */
function findSkillRoot(startDir) {
  let dir = startDir;
  for (let depth = 0; depth < MAX_WRAPPER_DEPTH; depth++) {
    if (fs.existsSync(path.join(dir, 'SKILL.md'))) return dir;
    const entries = fs.readdirSync(dir, { withFileTypes: true });
    const dirs = entries.filter((e) => e.isDirectory());
    const files = entries.filter((e) => !e.isDirectory());
    if (dirs.length !== 1 || files.length !== 0) return dir; // ambiguous or nothing left to descend into — stop and let the caller report "no SKILL.md"
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

// Folder-based Skill storage — one folder per Skill under data/skills/, each
// holding a SKILL.md (YAML frontmatter + instructions) plus whatever
// supporting files it needs. Replaces server/skills-fs.js as part of the
// from-scratch Skills rebuild (see CLAUDE.md's Skills section) — this is not
// a patch of the old file, it is written fresh against Anthropic's real
// SKILL.md spec (docs.claude.com), which the old build only approximated.
//
// A dependency-free leaf module, same reasoning as task-store.js/
// briefing-config.js: skills/index.js's dynamic import() loop must never
// have a path back here — see CLAUDE.md's circular-import invariant. Only
// store.js's dataDir() is imported from server/.
//
// A Skill's *content* lives entirely on disk as plain files — nothing about
// it is duplicated into data/skills.json, which holds only small runtime
// state per skill (enabled, installedAt, updatedAt, source, scriptsApproved)
// keyed by folder name.
//
// Deliberately dropped from the old build, to match Claude's real interface
// rather than Jarvis's own earlier invention: a separate `title` field (the
// `name` slug IS what's shown, exactly like Claude's own skills list shows
// "notion-second-brain", not a prettified title) and per-skill `settings.json`
// forms (Claude's skills have no configurable-parameters UI at all — a Skill
// is zero-argument, triggered by its description matching the request).

import fs from 'node:fs';
import path from 'node:path';
import { dataDir, readJson, writeJson } from '../../store.js';

const STATE_FILE = 'skills';
const MAX_FILE_BYTES = 512 * 1024; // matches connectors/files.js's MAX_READ_BYTES reasoning

// Anthropic's real spec (docs.claude.com/agents-and-tools/agent-skills/overview):
// name <= 64 chars, lowercase letters/digits/hyphens only, and must not
// contain "anthropic" or "claude" (reserved words). Stricter than a bare
// filesystem-safety regex on purpose, so a rejection here is the same reason
// a real Claude surface would reject it, not a Jarvis-only restriction.
const NAME_RE = /^[a-z][a-z0-9-]{0,63}$/;
const RESERVED_WORDS = ['anthropic', 'claude'];

// Anthropic's API docs give 1024 chars for `description`; the claude.ai Help
// Centre article gives 200. Using the more permissive documented limit here
// — noted rather than silently picking one, since the two official sources
// disagree (see CLAUDE.md's Skills research notes).
const MAX_DESCRIPTION = 1024;

function skillsDir() {
  const dir = path.join(dataDir(), 'skills');
  if (!fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true });
  return dir;
}

/** Turns free text into a valid slug — lowercase, spaces/underscores/etc collapsed to single hyphens, trimmed to the 64-char cap. Never throws; `validateName()` is the boundary that rejects. */
export function slugify(text) {
  return String(text || 'skill')
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 64) || 'skill';
}

/** Throws a plain-language error if `name` doesn't meet Anthropic's real SKILL.md `name` rules — the one place that check lives, so every write path (create, replace, an uploaded zip's own frontmatter) enforces the same rule. */
export function validateName(name) {
  const n = String(name || '');
  if (!NAME_RE.test(n)) {
    throw new Error('A skill name can only use lowercase letters, numbers, and hyphens (up to 64 characters), and must start with a letter.');
  }
  if (RESERVED_WORDS.some((w) => n.includes(w))) {
    throw new Error(`A skill name can't contain "${RESERVED_WORDS.find((w) => n.includes(w))}".`);
  }
}

export function validateDescription(description) {
  const d = String(description || '').trim();
  if (!d) throw new Error('Please describe what this skill does and when to use it.');
  if (d.length > MAX_DESCRIPTION) throw new Error(`Description is too long (${d.length} characters, limit ${MAX_DESCRIPTION}).`);
}

/** The one place a skill's folder path is built — every reader/writer in this
 * file goes through it, and it's the only function allowed to touch fs with
 * a caller-influenced name. Throws (never silently coerces) on anything that
 * isn't already a valid slug, so a bad name fails loudly at the boundary
 * instead of producing a path that might not point where it looks like it
 * does — same discipline as uploads.js's safeName() and connectors/files.js's
 * requireAllowed(). */
export function skillRoot(name) {
  validateName(name);
  return path.join(skillsDir(), name);
}

/** Resolves `relPath` inside a skill's folder and refuses anything that
 * escapes it (a `..` segment, an absolute path, a symlink pointing outside) —
 * checked by comparing the resolved path's prefix, never by trusting the
 * string itself. Mirrors connectors/files.js's isAllowed()/requireAllowed(). */
export function resolveSkillPath(name, relPath) {
  const root = skillRoot(name);
  const resolved = path.resolve(root, String(relPath || ''));
  if (resolved !== root && !resolved.startsWith(root + path.sep)) {
    throw new Error(`"${relPath}" is outside this skill's folder.`);
  }
  return resolved;
}

// ---------- frontmatter parsing ----------
//
// Deliberately minimal, not a YAML library (no new dependency — the project
// runs on 4 packages). Handles exactly what a SKILL.md needs: a `---` fence,
// top-level `key: value` scalars (quoted or bare), inline `[a, b, c]` lists,
// `key:` followed by indented `- item` lines, and YAML block scalars
// (`>`/`|`). Anything else (nested maps, anchors) is left unparsed rather
// than thrown on, since a skill written for Claude Code may carry frontmatter
// keys (license:, metadata:, allowed-tools:, ...) this never needs to act on.

function stripQuotes(s) {
  const t = s.trim();
  if ((t.startsWith('"') && t.endsWith('"')) || (t.startsWith("'") && t.endsWith("'"))) {
    return t.slice(1, -1);
  }
  return t;
}

function parseInlineList(s) {
  const t = s.trim();
  if (!t.startsWith('[') || !t.endsWith(']')) return null;
  const inner = t.slice(1, -1).trim();
  if (!inner) return [];
  return inner.split(',').map((x) => stripQuotes(x));
}

/**
 * Splits a SKILL.md's raw text into { frontmatter, body }. `frontmatter` is a
 * plain object of top-level keys -> string | string[]. `body` is everything
 * after the closing `---`, unmodified. Returns `{ frontmatter: {}, body: text }`
 * untouched if there's no fence at all, so a plain instructions-only file
 * still works rather than erroring.
 */
export function parseSkillMd(text) {
  const src = String(text || '');
  const lines = src.split(/\r?\n/);
  if (lines[0]?.trim() !== '---') {
    return { frontmatter: {}, body: src };
  }
  let end = -1;
  for (let i = 1; i < lines.length; i++) {
    if (lines[i].trim() === '---') { end = i; break; }
  }
  if (end === -1) {
    return { frontmatter: {}, body: src };
  }

  const frontmatter = {};
  let pendingListKey = null;
  let i = 1;
  while (i < end) {
    const line = lines[i];
    if (!line.trim()) { i++; continue; }

    const listItem = line.match(/^\s+-\s?(.*)$/);
    if (listItem && pendingListKey) {
      const arr = Array.isArray(frontmatter[pendingListKey]) ? frontmatter[pendingListKey] : [];
      arr.push(stripQuotes(listItem[1]));
      frontmatter[pendingListKey] = arr;
      i++;
      continue;
    }

    if (/^\s/.test(line)) {
      i++;
      continue;
    }

    const m = line.match(/^([A-Za-z0-9_-]+):\s*(.*)$/);
    if (!m) { pendingListKey = null; i++; continue; }
    const key = m[1];
    const rest = m[2];

    const blockScalar = rest.match(/^([>|])[+-]?\s*$/);
    if (blockScalar) {
      const folded = blockScalar[1] === '>';
      const bodyLines = [];
      let baseIndent = null;
      let j = i + 1;
      for (; j < end; j++) {
        const raw = lines[j];
        if (!raw.trim()) { bodyLines.push(''); continue; }
        const indentMatch = raw.match(/^(\s+)/);
        if (!indentMatch) break;
        if (baseIndent === null) baseIndent = indentMatch[1].length;
        if (indentMatch[1].length < baseIndent) break;
        bodyLines.push(raw.slice(baseIndent));
      }
      while (bodyLines.length && bodyLines[bodyLines.length - 1] === '') bodyLines.pop();
      frontmatter[key] = folded
        ? bodyLines.map((l) => (l === '' ? '\n' : l)).join(' ').replace(/ ?\n ?/g, '\n').trim()
        : bodyLines.join('\n');
      pendingListKey = null;
      i = j;
      continue;
    }

    if (rest === '') {
      pendingListKey = key;
      i++;
      continue;
    }
    pendingListKey = null;

    const inlineList = parseInlineList(rest);
    if (inlineList !== null) {
      frontmatter[key] = inlineList;
    } else {
      frontmatter[key] = stripQuotes(rest);
    }
    i++;
  }

  const body = lines.slice(end + 1).join('\n').replace(/^\n+/, '');
  return { frontmatter, body };
}

/** The inverse of parseSkillMd — used whenever Jarvis itself writes a
 * SKILL.md (create/edit/replace). Keeps the file human-editable and
 * Claude-Code-compatible: quoted scalars, `[a, b]` for short lists. Always
 * writes exactly `name` and `description`, preserving any other frontmatter
 * key (e.g. `allowed-tools`) already present via `extra`. */
export function serializeSkillMd({ name, description, body, extra = {} }) {
  const lines = ['---', `name: ${JSON.stringify(name)}`, `description: ${JSON.stringify(description)}`];
  for (const [key, value] of Object.entries(extra)) {
    if (key === 'name' || key === 'description') continue;
    if (value === undefined || value === null || value === '') continue;
    if (Array.isArray(value)) {
      lines.push(`${key}: [${value.map((v) => JSON.stringify(String(v))).join(', ')}]`);
    } else {
      lines.push(`${key}: ${JSON.stringify(String(value))}`);
    }
  }
  lines.push('---', '', String(body || '').trim(), '');
  return lines.join('\n');
}

// ---------- runtime state (data/skills.json) ----------

function loadState() {
  const data = readJson(STATE_FILE, { installed: {} });
  if (!data || typeof data !== 'object' || Array.isArray(data.installed)) {
    return { installed: {} };
  }
  return { installed: data.installed || {} };
}

function saveState(state) {
  writeJson(STATE_FILE, state);
}

function stateFor(name) {
  return loadState().installed[name] || null;
}

function setStateFor(name, patch) {
  const state = loadState();
  const current = state.installed[name] || {};
  state.installed[name] = { ...current, ...patch };
  saveState(state);
  return state.installed[name];
}

function removeStateFor(name) {
  const state = loadState();
  delete state.installed[name];
  saveState(state);
}

// ---------- reading ----------

function isSkillFolder(folderName) {
  if (!NAME_RE.test(folderName)) return false;
  const md = path.join(skillsDir(), folderName, 'SKILL.md');
  return fs.existsSync(md) && fs.statSync(md).isFile();
}

/** Every file in a skill's folder other than SKILL.md itself, as paths
 * relative to the folder (forward slashes). Flat + one level of subfolders is
 * plenty for what a Skill actually carries; deeper trees are still listed,
 * just walked recursively. */
export function listSkillFiles(name) {
  const root = skillRoot(name);
  const out = [];
  function walk(dir, prefix) {
    let entries;
    try {
      entries = fs.readdirSync(dir, { withFileTypes: true });
    } catch {
      return;
    }
    for (const entry of entries) {
      const rel = prefix ? `${prefix}/${entry.name}` : entry.name;
      if (entry.name === 'SKILL.md' && !prefix) continue;
      if (entry.isDirectory()) {
        walk(path.join(dir, entry.name), rel);
      } else {
        out.push(rel);
      }
    }
  }
  walk(root, '');
  return out;
}

/**
 * Every user Skill on disk — frontmatter + runtime state, the lightweight
 * "list" level (never the full instructions body). **This is the only
 * function any Skills UI or tool-declaration list may call to enumerate
 * Skills** — it reads `data/skills/` folders directly and has no code path
 * that can ever return a built-in ability or a connector tool. See
 * CLAUDE.md's permanent Skills rule for why this matters.
 */
export function listUserSkills() {
  let names;
  try {
    names = fs.readdirSync(skillsDir(), { withFileTypes: true })
      .filter((e) => e.isDirectory())
      .map((e) => e.name)
      .filter(isSkillFolder);
  } catch {
    names = [];
  }

  return names.map((folderName) => {
    const mdPath = path.join(skillsDir(), folderName, 'SKILL.md');
    const raw = fs.readFileSync(mdPath, 'utf8');
    const { frontmatter } = parseSkillMd(raw);
    const state = stateFor(folderName) || {};
    return {
      // `name` is what a model calls as a tool AND what the UI displays —
      // always the folder name, so it can never drift from the path this
      // skill actually lives at even if a downloaded SKILL.md's own `name:`
      // field disagrees.
      name: folderName,
      declaredName: frontmatter.name || folderName,
      description: frontmatter.description || '',
      allowedTools: Array.isArray(frontmatter['allowed-tools'])
        ? frontmatter['allowed-tools']
        : (typeof frontmatter['allowed-tools'] === 'string' ? [frontmatter['allowed-tools']] : []),
      enabled: state.enabled !== false, // default on
      source: state.source || { type: 'user' },
      installedAt: state.installedAt || null,
      updatedAt: state.updatedAt || null,
      // Whether the user has already agreed to let THIS skill's own helper
      // scripts run through the sandbox (server/skills/run_skill_script.js).
      scriptsApproved: state.scriptsApproved === true,
    };
  });
}

/** The full instructions body — read only when a skill is actually invoked or opened, never as part of the per-turn tool-declaration list. */
export function readSkillBody(name) {
  const mdPath = path.join(skillRoot(name), 'SKILL.md');
  const raw = fs.readFileSync(mdPath, 'utf8');
  return parseSkillMd(raw).body;
}

/** Raw SKILL.md text + parsed pieces, for the detail page's code view and the edit form. */
export function readSkillMd(name) {
  const mdPath = path.join(skillRoot(name), 'SKILL.md');
  const raw = fs.readFileSync(mdPath, 'utf8');
  const { frontmatter, body } = parseSkillMd(raw);
  return { raw, frontmatter, body };
}

/** Reads one file from inside a skill's folder (not SKILL.md itself, though nothing stops that) — what `read_skill_file.js` and `run_skill_script.js` use to open a Skill's supporting files. Size-capped, and path-contained via resolveSkillPath() above. */
export function readSkillFile(name, relPath) {
  const resolved = resolveSkillPath(name, relPath);
  const stat = fs.statSync(resolved); // throws (ENOENT) if missing — caller wraps
  if (stat.isDirectory()) throw new Error(`"${relPath}" is a folder, not a file.`);
  if (stat.size > MAX_FILE_BYTES) {
    throw new Error(`"${relPath}" is too large to read this way (${Math.round(stat.size / 1024)}KB).`);
  }
  return fs.readFileSync(resolved, 'utf8');
}

export function getSkill(name) {
  if (!isSkillFolder(name)) return null;
  return listUserSkills().find((s) => s.name === name) || null;
}

// ---------- writing ----------

/**
 * `reservedNames` (a Set, optional) lets a caller rule out any name already
 * used by a built-in code skill or an active connector tool — this module
 * never imports skills/index.js (see this file's header comment), so it
 * can't know that list on its own; skills/index.js's reservedSkillNames()
 * is what a caller passes in.
 */
function uniqueName(base, reservedNames = new Set()) {
  let name = slugify(base);
  let n = 2;
  const taken = (candidate) => {
    if (reservedNames.has(candidate)) return true;
    try {
      return fs.existsSync(path.join(skillsDir(), candidate));
    } catch {
      return false;
    }
  };
  while (taken(name)) {
    name = `${slugify(base)}-${n++}`;
  }
  return name;
}

/**
 * Creates a new skill folder from name/description/instructions — the
 * "Write skill instructions" form (see the create form's screenshot-matched
 * fields: Skill name / Description / Instructions). `name` is free text;
 * it's slugified and, if that collides, disambiguated with a trailing
 * number, same as the old build did — but validated against Anthropic's
 * real `name` rules (see validateName()) once slugified, not just made
 * filesystem-safe.
 */
export function createSkill({ name, description, instructions }, reservedNames = new Set()) {
  if (!name?.trim()) throw new Error('A skill needs a name.');
  validateDescription(description);
  if (!instructions?.trim()) throw new Error('A skill needs instructions.');

  const slug = uniqueName(name, reservedNames);
  validateName(slug);
  const root = skillRoot(slug);
  fs.mkdirSync(root, { recursive: true });

  const md = serializeSkillMd({ name: slug, description: description.trim(), body: instructions.trim() });
  fs.writeFileSync(path.join(root, 'SKILL.md'), md, 'utf8');

  const now = new Date().toISOString();
  setStateFor(slug, { enabled: true, source: { type: 'user' }, installedAt: now, updatedAt: now });
  return getSkill(slug);
}

/** Overwrites an existing skill's SKILL.md (description/instructions from the edit form), preserving any other frontmatter key (e.g. `allowed-tools`) the form doesn't know about. */
export function updateSkillMd(name, { description, instructions }) {
  const { frontmatter } = readSkillMd(name);
  if (description !== undefined) validateDescription(description);

  const md = serializeSkillMd({
    name: frontmatter.name || name,
    description: description !== undefined ? description.trim() : frontmatter.description || '',
    body: instructions !== undefined ? instructions.trim() : readSkillBody(name),
    extra: frontmatter,
  });
  fs.writeFileSync(path.join(skillRoot(name), 'SKILL.md'), md, 'utf8');
  setStateFor(name, { updatedAt: new Date().toISOString() });
  return getSkill(name);
}

export function updateSkillState(name, patch = {}) {
  if (!getSkill(name)) throw new Error(`Unknown skill: ${name}`);
  const allowed = {};
  if ('enabled' in patch) allowed.enabled = Boolean(patch.enabled);
  // Set only by server/skills/approve_skill_scripts.js, after the user has
  // explicitly agreed — never silently reset by anything in this module.
  if ('scriptsApproved' in patch) allowed.scriptsApproved = Boolean(patch.scriptsApproved);
  setStateFor(name, { ...allowed, updatedAt: new Date().toISOString() });
  return getSkill(name);
}

export function deleteSkill(name) {
  const root = skillRoot(name);
  fs.rmSync(root, { recursive: true, force: true });
  removeStateFor(name);
}

/** Replaces an existing skill's entire folder content in place (the detail page's "Replace" action) — same identity (folder name / state) as before, content swapped for what's in `resolvedDir`. Requires a SKILL.md directly inside `resolvedDir`. The caller (skill-zip.js) is responsible for cleaning up `resolvedDir` afterward either way. */
export function replaceSkillContents(name, resolvedDir) {
  const mdPath = path.join(resolvedDir, 'SKILL.md');
  if (!fs.existsSync(mdPath)) throw new Error("That zip doesn't have a SKILL.md in it.");
  const root = skillRoot(name);
  if (!fs.existsSync(root)) throw new Error(`Unknown skill: ${name}`);
  fs.rmSync(root, { recursive: true, force: true });
  fs.cpSync(resolvedDir, root, { recursive: true });
  setStateFor(name, { updatedAt: new Date().toISOString() });
  return getSkill(name);
}

/**
 * Installs a brand-new skill from a bare `SKILL.md`'s raw text — the "no
 * extra files" half of Claude's real upload mechanism (`.md` alongside
 * `.zip`/`.skill`; see skill-zip.js's header comment for how the three are
 * told apart). Requires `name` and `description` in the frontmatter, per
 * Claude's own stated requirement text ("`.md` file must contain skill name
 * and description formatted in YAML") — checked separately so a rejection
 * says exactly which one is missing. The text is written to disk exactly as
 * uploaded, never re-serialized, matching how a zip upload preserves its
 * original SKILL.md byte-for-byte.
 */
export function installFromMarkdown(text, reservedNames = new Set()) {
  const { frontmatter } = parseSkillMd(text);
  if (!frontmatter.name?.trim()) throw new Error('That file needs a "name" in its YAML frontmatter.');
  validateDescription(frontmatter.description);

  const name = uniqueName(frontmatter.name, reservedNames);
  validateName(name);
  const root = skillRoot(name);
  fs.mkdirSync(root, { recursive: true });
  fs.writeFileSync(path.join(root, 'SKILL.md'), String(text).trim() + '\n', 'utf8');

  const now = new Date().toISOString();
  setStateFor(name, { enabled: true, source: { type: 'upload' }, installedAt: now, updatedAt: now });
  return getSkill(name);
}

/** Replace — the bare-`.md` counterpart to replaceSkillContents(): swaps an existing skill's entire folder (dropping any bundled files it had) for just a fresh SKILL.md, same identity as before. Same name/description requirement as installFromMarkdown() above. */
export function replaceSkillMarkdown(name, text) {
  const { frontmatter } = parseSkillMd(text);
  if (!frontmatter.name?.trim()) throw new Error('That file needs a "name" in its YAML frontmatter.');
  validateDescription(frontmatter.description);

  const root = skillRoot(name);
  if (!fs.existsSync(root)) throw new Error(`Unknown skill: ${name}`);
  fs.rmSync(root, { recursive: true, force: true });
  fs.mkdirSync(root, { recursive: true });
  fs.writeFileSync(path.join(root, 'SKILL.md'), String(text).trim() + '\n', 'utf8');
  setStateFor(name, { updatedAt: new Date().toISOString() });
  return getSkill(name);
}

/** Installs a brand-new skill from an already-unpacked directory (skill-zip.js's job to unpack + find the right subfolder) — the Upload flow. Requires a SKILL.md directly inside `resolvedDir`, not searched for in subfolders (picking the wrong one out of a folder with several projects would be worse than failing loudly). */
export function installFromDirectory(resolvedDir, reservedNames = new Set()) {
  const mdPath = path.join(resolvedDir, 'SKILL.md');
  if (!fs.existsSync(mdPath)) throw new Error("That zip doesn't have a SKILL.md in it.");

  const raw = fs.readFileSync(mdPath, 'utf8');
  const { frontmatter } = parseSkillMd(raw);
  const baseName = frontmatter.name || path.basename(resolvedDir);
  const name = uniqueName(baseName, reservedNames);
  validateName(name);
  const root = skillRoot(name);
  fs.cpSync(resolvedDir, root, { recursive: true });

  const now = new Date().toISOString();
  setStateFor(name, { enabled: true, source: { type: 'upload' }, installedAt: now, updatedAt: now });
  return getSkill(name);
}

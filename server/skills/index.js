// Auto-loads every skill file in this folder. To add a new ability later,
// just drop a new file in server/skills/ that default-exports the same
// shape as the existing ones — nothing else in the app needs to change.
//
// Skills that create, change, or delete something lasting can set
// `confirm: 'always'` (or `'ifUnclear'`, gated on the turn's transcription
// confidence). The first call then returns a summary and a one-time token
// instead of acting; the model reads the summary back to the user (see
// prompt.js's system instruction) and only a second call carrying that
// token actually runs it. This is the voice-clarity read-back layer —
// see CLAUDE.md's design notes for why confidence scores alone aren't
// enough to catch a plausible mishearing like "seven" heard as "eleven".

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { listUserSkills, listSkillFiles, readSkillBody } from './store/skill-files.js';
import { getToolDeclarations as getConnectorDeclarations, runConnectorTool } from '../connectors/index.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

const skillFiles = fs
  .readdirSync(__dirname)
  .filter((f) => f.endsWith('.js') && f !== 'index.js');

const skills = new Map();

for (const file of skillFiles) {
  const mod = await import(`./${file}`);
  const skill = mod.default;
  if (!skill || !skill.name || typeof skill.run !== 'function') {
    console.warn(`[skills] Skipping ${file} — doesn't export a valid skill.`);
    continue;
  }
  skills.set(skill.name, skill);
}

// ---------- folder skills (installed Skills — SKILL.md + supporting files) ----------
//
// A Skill here is a folder under data/skills/<name>/
// (server/skills/store/skill-files.js), not a file in this directory — it
// can be installed/edited/removed while the server is running, unlike the
// built-in code skills above which are fixed at startup. Each enabled one is
// turned into an ordinary, zero-argument tool declaration so the model can
// call it like any other skill; calling it just hands back its instructions
// for the model to carry out with its normal tools — plus the list of any
// other files in its folder and a reminder that `read_skill_file` is how to
// open them.
//
// The declaration list (getToolDeclarations()) only ever reads a folder's
// SKILL.md *frontmatter* (listUserSkills(), cheap, no body) — the full
// instructions are read only here, inside run(), the moment the skill is
// actually called. Same progressive-disclosure shape Claude's own skills
// use: a one-line description costs nothing per turn; the real content is
// paid for only when it's used.
//
// `allowedTools` (from a downloaded SKILL.md's `allowed-tools:` — no Jarvis
// screen offers to set it, see the Skills spec) is communicated to the model
// as a plain-language note, not hard-enforced by filtering the live tool
// list mid-turn — doing that properly would need the tool-calling loop
// (models/runner.js) to track "we're mid-skill" across steps, which doesn't
// exist yet. Worth revisiting if a skill is ever seen reaching for a tool it
// shouldn't.
function folderSkillToTool(folder) {
  return {
    name: folder.name,
    description: folder.description,
    parameters: { type: 'object', properties: {}, required: [] },
    meta: false,
    kind: 'skill',
    async run() {
      const instructions = readSkillBody(folder.name);
      const toolNote = folder.allowedTools?.length
        ? `\n\nOnly use these of your abilities while doing this: ${folder.allowedTools.join(', ')}.`
        : '';
      const files = listSkillFiles(folder.name);
      const filesNote = files.length
        ? `\n\nThis skill's folder also has these files: ${files.join(', ')}. Use read_skill_file ` +
          `(skill: "${folder.name}") to open any of them. You can read them, never run them.`
        : '';
      return { ok: true, instructions: instructions + toolNote + filesNote };
    },
  };
}

function enabledFolderSkills() {
  return listUserSkills()
    .filter((f) => f.enabled)
    .map(folderSkillToTool);
}

// ---------- connector skills (server/connectors/ — Stage 3) ----------
//
// A connector tool (browser/files/mcp/cli/http) reaches the model exactly
// like a playbook does: connectors/index.js already worked out its risk
// tier and confirm requirement, so this is just a thin wrapper handing that
// same shape to the merge below and routing run() to runConnectorTool().
function connectorSkills() {
  return getConnectorDeclarations().map((d) => ({
    name: d.name,
    description: d.description,
    parameters: d.parameters,
    confirm: d.confirm,
    meta: false,
    kind: 'connector',
    async run(args) {
      try {
        return await runConnectorTool(d.name, args);
      } catch (err) {
        return { ok: false, error: err?.message || 'That connector ran into a problem.' };
      }
    },
  }));
}

/**
 * Tool declarations in the shape every model adapter expects — internal
 * fields (confirm, summarize, meta, ...) never leak into this.
 * `includeMeta: false` drops meta skills (schedule_task, cancel_task,
 * list_tasks, configure_briefing, remember_about_me, open_section) — they
 * only make sense in a live conversation, so an unattended/background turn
 * (a scheduled task's own prompt action) shouldn't be handed them. Default
 * stays `true` so existing callers (live chat, via runner.js) are
 * unaffected. Enabled folder Skills are always included — they're
 * conversation abilities either way, same as any built-in, non-meta skill.
 */
export function getToolDeclarations({ includeMeta = true } = {}) {
  const builtIns = Array.from(skills.values()).filter((s) => includeMeta || !s.meta);
  return [...builtIns, ...enabledFolderSkills(), ...connectorSkills()].map((s) => ({
    name: s.name,
    description: s.description,
    parameters: s.parameters,
  }));
}

/**
 * Skills offered for unattended/structured use (scheduled task actions,
 * briefing sources) — a public shape a UI can render a picker from. `meta`
 * skills (scheduling, cancelling, remembering, navigating — see each
 * file's `meta: true`) only make sense inside a live conversation, so
 * they're left out by default: attaching "schedule a task" as the action
 * of a scheduled task is a confusing loop, not a useful automation. A
 * skill opts in or out of this list on its own file; nothing here needs
 * editing when a new skill is added. Enabled folder Skills are included
 * too — a Skill is just as valid a task action or briefing source as a
 * built-in one.
 */
/**
 * Every runnable skill — built-in, user Skill, or connector tool — tagged
 * with `kind: 'builtin' | 'skill' | 'connector'`. This is a general-purpose
 * enumeration (used by the task/briefing action pickers, where offering a
 * built-in ability alongside a user Skill is legitimate — see
 * schedule_task.js). **It is NOT safe for a Skills-only UI to filter this
 * down by `kind` and call it done** — use `listUserSkills()`
 * (./store/skill-files.js) directly for that, per CLAUDE.md's permanent
 * Skills rule: a Skills screen must be built on a source that cannot
 * structurally return a native ability, not on a filtered version of this.
 */
export function listSkills({ includeMeta = false } = {}) {
  const builtIns = Array.from(skills.values()).filter((s) => includeMeta || !s.meta);
  return [...builtIns, ...enabledFolderSkills(), ...connectorSkills()].map((s) => ({
    name: s.name,
    description: s.description,
    parameters: s.parameters,
    meta: Boolean(s.meta),
    kind: s.kind || 'builtin',
  }));
}

export function hasSkill(name) {
  if (skills.has(name)) return true;
  if (listUserSkills().some((f) => f.name === name && f.enabled)) return true;
  return connectorSkills().some((s) => s.name === name);
}

// ---------- managing installed Skills (Skills screen) ----------
//
// This module doesn't do any of the actual file I/O for install/edit/remove
// — server.js's routes call server/skills/store/skill-files.js and
// skill-zip.js directly for that (create/update/delete a folder, unpack a
// zip). What lives here is just the one thing that store can't know on its
// own: which tool names are already reserved by a built-in code skill or an
// active connector, so a new Skill's name never collides with one.

/** Every reserved tool name a new Skill must not collide with — every built-in code skill, every active connector tool (skill-files.js's own uniqueName() only checks against other Skill folders, since it can't import this file — see its header comment). */
export function reservedSkillNames() {
  return new Set([...skills.keys(), ...connectorSkills().map((s) => s.name)]);
}

// ---------- confirmation layer ----------

const pending = new Map(); // token -> { name, args, expiresAt }
const CONFIRM_TTL_MS = 5 * 60 * 1000;

function makeToken() {
  return `c${Date.now().toString(36)}${Math.random().toString(36).slice(2, 10)}`;
}

function requiresConfirmation(skill, ctx) {
  // Unattended runs (scheduled tasks, briefing sources) were consented to
  // once at creation time — there is nobody present to answer a live
  // read-back, so the gate is bypassed entirely rather than stalling the
  // run on a confirm_token nobody will ever send back. Interactive chat
  // (pipeline/live engines) never sets this, so the normal read-back flow
  // there is unaffected.
  if (ctx?.autoConfirm) return false;
  if (!skill.confirm || skill.confirm === 'never') return false;
  if (skill.confirm === 'ifUnclear') return Boolean(ctx?.lowConfidence);
  return true; // 'always'
}

/**
 * The pending record for a valid, unexpired token belonging to THIS skill,
 * or null. Consumes the token either way (one-time use) once it's looked up
 * at all — a token is meant to represent one specific "yes", not a reusable
 * key.
 *
 * Deliberately does NOT compare the resent arguments against what was
 * originally asked about — the token's presence, matched to the right skill
 * name and not expired, IS the proof of consent; the args to actually run
 * with are the ones stored on the token record (see runSkill() below), never
 * whatever the model resent alongside it. An earlier version required the
 * resent args to byte-for-byte match the original (via a sorted-JSON key) —
 * fine for a skill with one or two simple string arguments, but a real bug
 * for anything with a complex/nested shape (a connector tool proxying a
 * multi-field object, e.g. Composio's toolkit-executor tools): a model
 * regenerating "the same" call after the user says yes has no guarantee of
 * producing an identical object, so the strict-equality check would silently
 * fail, mint a fresh token, and ask again — the user says yes, and the same
 * question comes back, looking like an infinite confirmation loop. Trusting
 * the token itself (random, single-use, skill-scoped, 5-minute TTL) is
 * simpler and doesn't have this failure mode.
 */
function consumePendingToken(skill, rawArgs) {
  const token = rawArgs?.confirm_token;
  if (!token) return null;
  const record = pending.get(token);
  if (!record) return null;
  pending.delete(token); // one-time use either way
  if (record.name !== skill.name) return null;
  if (Date.now() > record.expiresAt) return null;
  return record;
}

/** Finds a runnable skill by name — a built-in code skill first, then a currently-enabled folder Skill, then a connector tool (a disabled/removed one of either is treated as unknown, same as if it never existed). */
function findSkill(name) {
  if (skills.has(name)) return skills.get(name);
  const folder = listUserSkills().find((f) => f.name === name && f.enabled);
  if (folder) return folderSkillToTool(folder);
  return connectorSkills().find((s) => s.name === name) || null;
}

/**
 * Runs a named skill with the given arguments. `ctx` (optional) carries
 * {sessionId, modelId, lowConfidence, autoConfirm}, plus `reservedSkillNames`
 * (this module's own function, injected below) — a skill file under
 * server/skills/ can't import `reservedSkillNames` from this module directly
 * (that's the loader itself; see CLAUDE.md's circular-import invariant), so
 * this is how create_skill.js gets it without deadlocking the dynamic-import
 * loader at startup. Never throws.
 */
export async function runSkill(name, args, ctx = {}) {
  const skill = findSkill(name);
  if (!skill) {
    return { ok: false, error: `Unknown skill: ${name}` };
  }

  const cleanArgs = { ...(args || {}) };
  delete cleanArgs.confirm_token;

  try {
    if (requiresConfirmation(skill, ctx)) {
      const confirmed = consumePendingToken(skill, args || {});
      if (confirmed) {
        // Run with the ORIGINAL args captured when confirmation was asked
        // for, not whatever the model just resent — see
        // consumePendingToken()'s doc comment for why.
        return await skill.run(confirmed.args, { ...ctx, reservedSkillNames });
      }
      const token = makeToken();
      pending.set(token, { name: skill.name, args: cleanArgs, expiresAt: Date.now() + CONFIRM_TTL_MS });
      // `await` here works whether summarize() is sync (every existing
      // skill) or async (control_computer.js generates its plan via a real
      // model call before it has anything to read back) — awaiting a
      // plain string is a no-op, so this changes nothing for callers that
      // were already sync.
      const summary = typeof skill.summarize === 'function' ? await skill.summarize(cleanArgs) : skill.description;
      return { ok: true, needs_confirmation: true, summary, confirm_token: token };
    }

    return await skill.run(cleanArgs, { ...ctx, reservedSkillNames });
  } catch (err) {
    console.error(`[skills] "${name}" threw:`, err);
    return { ok: false, error: `Something went wrong running ${name}.` };
  }
}

console.log(`[skills] Loaded: ${Array.from(skills.keys()).join(', ')}`);

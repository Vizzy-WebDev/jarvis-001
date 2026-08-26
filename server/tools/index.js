// Auto-loads every executable capability ("tool") file in this folder. To
// add a new ability later, just drop a new file in server/tools/ that
// default-exports the same shape as the existing ones — nothing else in the
// app needs to change.
//
// This is the built-in-code half of what used to be one merged
// server/skills/index.js. It was split out (see CLAUDE.md's Structure block
// and the tools/skills split history) because "skill" was being used for two
// genuinely different things: a folder of instructions (server/skills/,
// data/skills/) vs. a real executable capability written in JavaScript
// (get_weather, open_app, run_code, ...). Those were always tagged apart in
// the data (`kind: 'builtin'|'skill'|'connector'`) and the Skills UI already
// read a source that could never return one of these — but the vocabulary
// itself ("runSkill", "hasSkill", `server/skills/*.js` holding built-ins)
// kept regenerating the confusion for anyone reading or extending the code.
// This module owns ONLY the built-in-code half now.
//
// server/capabilities.js is the composition seam that merges this with
// folder Skills (server/skills/index.js) and connector tools
// (server/connectors/index.js) into what a model actually sees, and owns the
// confirmation layer — this module has no confirm gate of its own.

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

const toolFiles = fs
  .readdirSync(__dirname)
  .filter((f) => f.endsWith('.js') && f !== 'index.js');

const tools = new Map();

for (const file of toolFiles) {
  const mod = await import(`./${file}`);
  const tool = mod.default;
  if (!tool || !tool.name || typeof tool.run !== 'function') {
    console.warn(`[tools] Skipping ${file} — doesn't export a valid tool.`);
    continue;
  }
  tools.set(tool.name, tool);
}

/**
 * Every loaded built-in tool, unfiltered internal shape (still carries
 * `confirm`/`summarize`/`meta`/`run`) — capabilities.js is the only intended
 * caller; it strips/uses these fields to build the model-facing declaration
 * list and the confirmation layer.
 */
export function rawTools() {
  return tools;
}

/**
 * Tool declarations in built-in tools alone, tagged `kind: 'builtin'`.
 * `includeMeta: false` drops meta tools (schedule_task, cancel_task,
 * list_tasks, configure_briefing, remember_about_me, open_section, ...) —
 * see each file's own `meta: true` comment for why. Default `true` so
 * existing callers are unaffected unless they opt out.
 */
export function listTools({ includeMeta = true } = {}) {
  return Array.from(tools.values())
    .filter((t) => includeMeta || !t.meta)
    .map((t) => ({
      name: t.name,
      description: t.description,
      parameters: t.parameters,
      confirm: t.confirm,
      meta: Boolean(t.meta),
      kind: 'builtin',
    }));
}

export function hasTool(name) {
  return tools.has(name);
}

export function getTool(name) {
  return tools.get(name) || null;
}

/** Every built-in tool's name — capabilities.js folds this together with folder-Skill and connector names into the full reserved set a new Skill's name must not collide with. */
export function reservedToolNames() {
  return new Set(tools.keys());
}

console.log(`[tools] Loaded: ${Array.from(tools.keys()).join(', ')}`);

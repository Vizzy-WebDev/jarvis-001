// Skill: reads a supporting file from inside one of the folder-based
// Skills (data/skills/<name>/...) — the reference doc, template, or example
// a SKILL.md's instructions point at, e.g. "the full tone rules are in
// reference/voice.md". This is what makes a skill folder's extra files mean
// anything to Jarvis; without it they'd just be inert bytes on disk.
//
// Deliberately read-only. A skill folder can contain a script, but Jarvis
// never executes one — only ever reads it as reference, same as any other
// file in the folder. See the design note in the Skills spec for why: the
// scheduler and briefing run unattended with the confirm gate switched off,
// so "run it, but ask first" would silently not ask at 7am. If a real need
// for execution ever comes up, that's a separate, deliberate feature.
//
// Every path is resolved and checked before any fs call, never trusted as a
// string — same discipline as connectors/files.js's requireAllowed().

import { resolveSkillPath, listSkillFiles, readSkillFile as readFile } from './store/skill-files.js';

export default {
  name: 'read_skill_file',
  description:
    "Read a supporting file from inside one of your installed Skills' own folder — " +
    'a reference document, template, or example it points you to. Use the exact path ' +
    "given in that Skill's instructions or file list. This only reads text; it never " +
    'runs anything.',
  parameters: {
    type: 'object',
    properties: {
      skill: { type: 'string', description: 'The Skill this file belongs to (its tool name).' },
      path: { type: 'string', description: 'The file\'s path relative to that Skill\'s folder, e.g. "reference/rules.md".' },
    },
    required: ['skill', 'path'],
  },
  // Deliberately NOT meta:true. `meta` (see skills/index.js) is for abilities
  // that only make sense in live conversation — background:true (scheduled
  // tasks, briefing) drops every meta tool from that turn's declarations. A
  // Skill with supporting files needs this ability just as much on a
  // schedule as live, so it stays a plain always-available tool. The one
  // side effect: it also shows up in the task-creation/briefing-source
  // picker (listSkills()) as a pickable action, taking a skill+path pair —
  // odd to select there, but harmless, and correct wins over tidy here.
  async run({ skill, path: relPath }) {
    try {
      resolveSkillPath(skill, relPath); // throws first on an escaping path, before anything is read
      const content = readFile(skill, relPath);
      return { ok: true, path: relPath, content };
    } catch (err) {
      const files = (() => {
        try {
          return listSkillFiles(skill);
        } catch {
          return [];
        }
      })();
      return {
        ok: false,
        error: err?.message || `Could not read "${relPath}" from that skill.`,
        availableFiles: files,
      };
    }
  },
};

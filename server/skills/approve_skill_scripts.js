// Skill: grants permission for ONE specific installed Skill's own helper
// scripts to run through the sandbox (server/sandbox/), from now on. This is
// the ONLY way a Skill's `scriptsApproved` flag is ever set — there's no
// settings-screen toggle for it, same reasoning as allow_folder.js's folder
// allowlist: a real, one-time consent belongs in a live conversation, not a
// checkbox nobody reads. `confirm: 'always'` reuses the existing
// read-back-and-confirm gate (skills/index.js) rather than trusting the
// model's own judgment about what counts as a real "yes."
//
// Meta skill — approving script execution only ever makes sense as part of
// a live, two-way conversation, never something a scheduled task should be
// able to trigger on its own.

import { getSkill, updateSkillState } from './store/skill-files.js';

export default {
  name: 'approve_skill_scripts',
  meta: true,
  description:
    "Grants permission for one specific installed Skill's own helper scripts to run (through Jarvis's sandbox) " +
    'from now on. Only call this after run_skill_script reported it needs approval, and the user has clearly agreed.',
  confirm: 'always',
  parameters: {
    type: 'object',
    properties: {
      skill: { type: 'string', description: 'The Skill (its tool name) whose scripts should be allowed to run.' },
    },
    required: ['skill'],
  },
  summarize(args) {
    const skill = getSkill(args.skill);
    const label = skill?.name || args.skill;
    return `Let "${label}" run its own scripts through Jarvis's sandbox — now, and every time from now on?`;
  },
  async run(args) {
    const skill = getSkill(args.skill);
    if (!skill) return { ok: false, error: `Unknown skill: "${args.skill}".` };
    updateSkillState(args.skill, { scriptsApproved: true });
    return { ok: true, approved: skill.name };
  },
};

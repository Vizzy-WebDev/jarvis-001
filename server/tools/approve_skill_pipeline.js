// Tool: grants permission for ONE specific installed Skill's skill.toml
// pipeline to actually RUN its steps, from now on. This is the ONLY way a
// Skill's `pipelineApproved` flag is ever set true for an uploaded/replaced
// pipeline — there's no settings-screen toggle for it, same reasoning as
// approve_skill_scripts.js: a real, one-time consent belongs in a live
// conversation, not a checkbox nobody reads. `confirm: 'always'` reuses the
// existing read-back-and-confirm gate (capabilities.js) rather than
// trusting the model's own judgment about what counts as a real "yes."
//
// A DIFFERENT gate from approve_skill_scripts.js's `scriptsApproved`, even
// though the shape is a near-copy: that one controls a Skill's own helper
// SCRIPTS running through the sandbox (server/tools/run_skill_script.js);
// this one controls a Skill's skill.toml PIPELINE running its fixed step
// sequence at all (server/skills/pipeline.js, dispatched from
// server/skills/index.js's folderSkillToTool()). Approving one never
// approves the other.
//
// Meta tool — approving pipeline execution only ever makes sense as part of
// a live, two-way conversation, never something a scheduled task should be
// able to trigger on its own.

import { getSkill, updateSkillState } from '../skills/store/skill-files.js';

export default {
  name: 'approve_skill_pipeline',
  meta: true,
  description:
    "Grants permission for one specific installed Skill's skill.toml pipeline to actually run its steps " +
    '(rather than staying instructions-only) from now on. Only call this after the model reported a Skill\'s ' +
    'pipeline needs approval, and the user has clearly agreed.',
  confirm: 'always',
  parameters: {
    type: 'object',
    properties: {
      skill: { type: 'string', description: 'The Skill (its tool name) whose pipeline should be allowed to run.' },
    },
    required: ['skill'],
  },
  summarize(args) {
    const skill = getSkill(args.skill);
    const label = skill?.name || args.skill;
    return `Let "${label}"'s pipeline run its steps automatically — now, and every time from now on?`;
  },
  async run(args) {
    const skill = getSkill(args.skill);
    if (!skill) return { ok: false, error: `Unknown skill: "${args.skill}".` };
    updateSkillState(args.skill, { pipelineApproved: true });
    return { ok: true, approved: skill.name };
  },
};

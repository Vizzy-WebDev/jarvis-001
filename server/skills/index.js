// Folder Skills only — SKILL.md instructions and/or a skill.toml fixed
// pipeline, under data/skills/<name>/. A folder needs one or both; neither
// is on its own an error (see store/skill-files.js's isSkillFolder()).
//
// This used to be one file that ALSO loaded every built-in executable
// capability (get_weather, open_app, run_code, ...) and merged in connector
// tools, under the name "skill" for all three. That's been split apart:
// built-in capabilities now live in server/tools/ (loaded by
// server/tools/index.js), and server/capabilities.js is the seam that
// merges tools + folder Skills (this file) + connectors into what a model
// actually sees, and owns the confirm-and-read-back gate. This file owns
// only the one thing its name means now: a folder of instructions and/or a
// pipeline on disk.
//
// server/skills/ must never import server/tools/index.js or
// server/capabilities.js — see CLAUDE.md's circular-import invariant. It
// DOES safely import server/tools/index.js's listTools()/getTool() (that
// module has zero imports back into server/skills/ or capabilities.js — no
// cycle) and server/ai.js's askModel (same: documented safe to import from
// anywhere, no path back to capabilities.js). What it never imports is
// capabilities.js's invoke() — that one genuinely would deadlock
// (capabilities.js -> this file -> pipeline.js -> capabilities.js), so a
// pipeline's tool-kind steps reach it only via `ctx.invoke`, injected by
// capabilities.js's own invoke() into every capability's ctx — see that
// file's invoke() for the other half of this.

import { listUserSkills, listSkillFiles, readSkillBody, readSkillToml } from './store/skill-files.js';
import { parseToml, validatePipeline } from './store/skill-toml.js';
import { runPipeline, confirmRequiringSteps } from './pipeline.js';
import { listTools } from '../tools/index.js';
import { askModel } from '../ai.js';

function knownToolNames() {
  return listTools({ includeMeta: false }).map((t) => t.name);
}

/**
 * Reads + parses + validates a folder's skill.toml, if it has one. Never
 * throws — a broken or missing file is reported through the return shape,
 * never breaks the surrounding tool-declaration build (this runs on every
 * turn, for every enabled folder Skill).
 *
 * Returns `{hasToml, pipeline, errors}` — `pipeline` is the parsed doc only
 * when `errors` is empty; otherwise null (so a caller can treat "has a
 * broken pipeline" and "has no pipeline" identically wherever it doesn't
 * need to tell them apart, and differently — surfacing `errors` — wherever
 * it does).
 */
function loadPipeline(name) {
  const raw = readSkillToml(name);
  if (raw === null) return { hasToml: false, pipeline: null, errors: [] };
  try {
    const doc = parseToml(raw);
    const { errors } = validatePipeline(doc, { knownToolNames: knownToolNames() });
    return { hasToml: true, pipeline: errors.length ? null : doc, errors };
  } catch (err) {
    return { hasToml: true, pipeline: null, errors: [err?.message || 'Could not parse skill.toml.'] };
  }
}

const INPUT_JSON_TYPE = { number: 'number', boolean: 'boolean' };

/** A pipeline's `[[inputs]]` as a JSON Schema — what the model sees as this tool's arguments. Falls back to the existing zero-argument shape when there are none (a pipeline with no declared inputs is called exactly like a plain instructions-only Skill always has been). */
function inputsToParameters(pipeline) {
  const properties = {};
  const required = [];
  for (const input of pipeline?.inputs || []) {
    if (!input?.name) continue;
    properties[input.name] = {
      type: INPUT_JSON_TYPE[input.type] || 'string',
      ...(input.description ? { description: input.description } : {}),
    };
    if (input.required) required.push(input.name);
  }
  return { type: 'object', properties, required };
}

// A Skill here is a folder under data/skills/<name>/
// (server/skills/store/skill-files.js) — it can be installed/edited/removed
// while the server is running, unlike a built-in tool which is fixed at
// startup. Each enabled one becomes an ordinary tool declaration so the
// model can call it like any other capability.
//
// Three shapes, decided by what's actually in the folder:
//   - SKILL.md only (today's original shape) — calling it hands back its
//     instructions for the model to carry out with its normal tools.
//   - skill.toml only, or SKILL.md + skill.toml — calling it RUNS the
//     pipeline's fixed steps in order (server/skills/pipeline.js) and
//     returns their results; SKILL.md's body, if present, rides along as
//     extra context for the model to weave in.
//   - skill.toml present but invalid or not yet approved — falls back to
//     instructions-only (or a plain "nothing to do yet" note if there's no
//     SKILL.md either), with a note explaining why the pipeline didn't run.
//     The tool declaration itself is never broken by a bad skill.toml.
//
// The declaration list only ever reads a folder's SKILL.md *frontmatter*
// and skill.toml's small, cheap-to-parse structure (listUserSkills() +
// loadPipeline(), both re-read every turn but neither does a model call or
// touches anything expensive) — SKILL.md's full instructions BODY is read
// only inside run(), the moment the skill is actually called. Same
// progressive-disclosure shape Claude's own skills use for the free-form
// text; the pipeline's shape (inputs, confirm-need) has to be known up
// front, the same way any other tool's parameters/confirm do.
//
// `allowedTools` (from a downloaded SKILL.md's `allowed-tools:` — no Jarvis
// screen offers to set it, see the Skills spec) is communicated to the model
// as a plain-language note, not hard-enforced by filtering the live tool
// list mid-turn — doing that properly would need the tool-calling loop
// (models/runner.js) to track "we're mid-skill" across steps, which doesn't
// exist yet. Worth revisiting if a skill is ever seen reaching for a tool it
// shouldn't.
function folderSkillToTool(folder) {
  const { pipeline, errors: pipelineErrors } = loadPipeline(folder.name);
  const pipelineNeedsApproval = Boolean(pipeline) && folder.pipelineApproved !== true;
  const runnablePipeline = pipeline && !pipelineNeedsApproval ? pipeline : null;
  const confirmSteps = runnablePipeline ? confirmRequiringSteps(runnablePipeline.steps) : [];

  return {
    name: folder.name,
    // SKILL.md's frontmatter description wins when both exist (matches the
    // precedence table: SKILL.md is the primary surface when present); a
    // pipeline-only Skill (no SKILL.md) falls back to skill.toml's own
    // top-level `description` — see skill-toml.js's header comment for why
    // that field exists at all.
    description: folder.description || runnablePipeline?.description || pipeline?.description || '',
    parameters: runnablePipeline ? inputsToParameters(runnablePipeline) : { type: 'object', properties: {}, required: [] },
    meta: false,
    kind: 'skill',
    confirm: confirmSteps.length ? 'always' : undefined,
    summarize() {
      const names = confirmSteps.map((s) => `"${s.id}"`).join(', ');
      return `Run the "${folder.name}" skill, which includes a step (${names}) that needs your OK — proceed?`;
    },
    async run(args, ctx = {}) {
      const instructions = readSkillBody(folder.name); // '' if this Skill has no SKILL.md at all
      const toolNote = folder.allowedTools?.length
        ? `\n\nOnly use these of your abilities while doing this: ${folder.allowedTools.join(', ')}.`
        : '';
      const files = listSkillFiles(folder.name);
      const filesNote = files.length
        ? `\n\nThis skill's folder also has these files: ${files.join(', ')}. Use read_skill_file ` +
          `(skill: "${folder.name}") to open any of them. You can read them, never run them.`
        : '';
      const instructionsResult = { ok: true, instructions: (instructions || '') + toolNote + filesNote };

      if (!pipeline) {
        // No skill.toml, or one that failed to parse/validate — today's
        // original behavior either way. A parse/validation failure is
        // surfaced to the Skills UI (server.js's detail route), not spliced
        // into what the model sees here — a broken pipeline shouldn't make
        // an otherwise-fine set of instructions read strangely.
        return instructionsResult;
      }

      if (pipelineNeedsApproval) {
        return {
          ...instructionsResult,
          pipelineNeedsApproval: true,
          instructions:
            instructionsResult.instructions +
            `\n\nThis skill also has a pipeline (skill.toml) that hasn't been approved to run yet. If the user ` +
            `wants it enabled, ask, and if they agree, call approve_skill_pipeline (skill: "${folder.name}").`,
        };
      }

      // capabilities.js's confirm gate is bypassed entirely by
      // ctx.autoConfirm BEFORE run() is ever called (that's the existing,
      // correct behavior for every other tool — an unattended run was
      // already consented to once, at setup). A pipeline is different: its
      // confirm-requiring step(s) were never individually seen at setup
      // time the way a single scheduled action's own confirm dialog would
      // be — "run this Skill" as a task action is not the same consent as
      // "run this Skill, INCLUDING a step that emails/deletes/pays." So
      // run() itself is the only place left that can catch this and refuse
      // — capabilities.js has already decided not to ask by the time we're
      // here. See the plan's Phase 4 gates: this is a deliberate exception
      // to autoConfirm's normal meaning, not a bug in it.
      if (confirmSteps.length && ctx.autoConfirm) {
        const names = confirmSteps.map((s) => `"${s.id}"`).join(', ');
        return {
          ok: false,
          error:
            `This skill's pipeline includes a step (${names}) that needs confirmation, so it can't run ` +
            'unattended. Run it from a live conversation instead, or remove that step from the pipeline.',
        };
      }

      // Every declared, required input must actually be present before
      // starting — otherwise the failure would surface confusingly deep
      // inside the first step that references the missing one, as an
      // "unresolved {{inputs.x}}" rather than a plain "you're missing an
      // argument."
      const missing = (runnablePipeline.inputs || []).filter((i) => i.required && !(i.name in (args || {})));
      if (missing.length) {
        return { ok: false, error: `Missing required input${missing.length > 1 ? 's' : ''}: ${missing.map((i) => `"${i.name}"`).join(', ')}.` };
      }

      const result = await runPipeline(runnablePipeline, {
        invoke: ctx.invoke,
        askModel,
        ctx: { ...ctx, pipelineInputs: args || {} },
      });

      return {
        ok: result.ok,
        steps: result.steps,
        error: result.error,
        // SKILL.md's body, when this folder has one, rides along as extra
        // context for the model to weave into its reply — the precedence
        // table's "steps run; SKILL.md appended as context" case.
        ...(instructions ? { instructions: instructions + toolNote + filesNote } : {}),
      };
    },
  };
}

/** Every currently-enabled folder Skill, as a runnable {name, description, parameters, meta: false, kind: 'skill', run} object — capabilities.js's builtInCapabilities()-equivalent for folder Skills. */
export function listFolderSkillTools() {
  return listUserSkills()
    .filter((f) => f.enabled)
    .map(folderSkillToTool);
}

/** The runnable object for one currently-enabled folder Skill, or null (a disabled/removed one is treated as unknown, same as if it never existed). */
export function getFolderSkillTool(name) {
  const folder = listUserSkills().find((f) => f.name === name && f.enabled);
  return folder ? folderSkillToTool(folder) : null;
}

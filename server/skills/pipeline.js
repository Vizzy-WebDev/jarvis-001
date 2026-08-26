// Executes a parsed, validated skill.toml pipeline (skill-toml.js's
// parseToml() output) — sequential, fixed order, no branching, no
// conditionals. Two step kinds: `tool` (dispatched through the injected
// `invoke`, i.e. capabilities.js's own dispatcher, so every existing gate —
// per-tool confirm, run_skill_script's scriptsApproved, the sandbox —
// applies exactly as it would to any other call) and `prompt` (one
// askModel call, no tools, no side effects).
//
// `invoke` is INJECTED here, never imported. This file lives under
// server/skills/, and capabilities.js -> skills/index.js -> pipeline.js ->
// capabilities.js would be exactly the class of deadlock the root
// CLAUDE.md's circular-import invariant exists to prevent — one hop
// further out than the tools/index.js case the invariant was written for.
// server/skills/index.js's folderSkillToTool() is where the real `invoke`
// comes from at runtime: capabilities.js's own invoke() now injects itself
// into every capability's `ctx` (`ctx.invoke`), the same mechanism already
// used for `ctx.reservedSkillNames` — see capabilities.js's invoke().
//
// `askModel` is injected too, for symmetry and so a test can pass a stub
// with no real model/quota involved — though unlike `invoke`, importing
// `../ai.js` directly here would in fact be perfectly safe (ai.js has no
// import path back to capabilities.js or skills/index.js; its own header
// comment says as much). Kept as an injected parameter anyway, matching the
// approved plan and keeping both of a pipeline's two step kinds handled the
// same way from this file's point of view.
//
// `getTool` (server/tools/index.js) IS imported directly — that's safe
// (tools/index.js has zero imports back into server/skills/ or
// capabilities.js) and is what lets this file defend, at the moment a step
// actually runs, against a step naming something that isn't a real,
// non-meta built-in tool — even if the pipeline's author-time validation
// (skill-toml.js's validatePipeline()) was somehow skipped, or the file was
// hand-edited after the pipeline was approved. Belt and suspenders: the
// Skills UI validates at author time (Phase 5), this validates again at
// run time.

import { getTool } from '../tools/index.js';
import { resolveTemplates } from './pipeline-template.js';

const DEFAULT_STEP_TIMEOUT_MS = 30_000;
const MAX_STEPS = 20;

function withTimeout(promise, ms, label) {
  let timer;
  const timeout = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error(`Step "${label}" timed out after ${Math.round(ms / 1000)}s.`)), ms);
  });
  return Promise.race([promise, timeout]).finally(() => clearTimeout(timer));
}

/**
 * Every tool-kind step in `steps` whose target tool declares a `confirm`
 * requirement — used by server/skills/index.js's folderSkillToTool() to
 * decide the WRAPPER tool's own `confirm` field. The whole pipeline
 * confirms once, up front (naming these steps in its read-back) rather
 * than pausing mid-run on step 3 — see that file for the full reasoning.
 * A `prompt` step never needs confirmation (it has no side effects); an
 * unrecognized/meta tool name is treated as non-confirming here (it will
 * fail loudly at run time instead — see runPipeline()'s per-step check).
 */
export function confirmRequiringSteps(steps) {
  return (steps || []).filter((s) => {
    if (typeof s?.tool !== 'string') return false;
    const tool = getTool(s.tool);
    return Boolean(tool?.confirm);
  });
}

/**
 * Runs a validated pipeline (`{inputs, steps}`) against real argument
 * values. `invoke`/`askModel` are injected (see this file's header). `ctx`
 * is passed through to every `invoke()` call with `autoConfirm: true`
 * forced — by the time this function runs, either the whole pipeline was
 * already confirmed once as a single unit (an interactive run whose
 * wrapper tool declared `confirm: 'always'`, per confirmRequiringSteps()
 * above) or no step needed confirmation at all; re-confirming per-step here
 * would defeat "confirm once, up front."
 *
 * `ctx.pipelineInputs` carries the real argument values for this run (the
 * model's actual tool-call arguments, matched against `pipeline.inputs`'
 * declared names).
 *
 * Never throws. Returns
 * `{ok, steps: [{id, ok, result|error}], error?}` — a step's own failure is
 * recorded, not propagated, unless it stops the whole run (a step without
 * `continue_on_error: true` stops the pipeline at that point; the partial
 * `steps` record up to and including the failure is still returned).
 */
export async function runPipeline(pipeline, { invoke, askModel, ctx = {} } = {}) {
  if (typeof invoke !== 'function') {
    return { ok: false, error: 'runPipeline() needs an invoke function.', steps: [] };
  }
  const steps = Array.isArray(pipeline?.steps) ? pipeline.steps : [];
  if (steps.length > MAX_STEPS) {
    return { ok: false, error: `This pipeline has ${steps.length} steps — the limit is ${MAX_STEPS}.`, steps: [] };
  }

  const inputs = ctx.pipelineInputs && typeof ctx.pipelineInputs === 'object' ? ctx.pipelineInputs : {};
  const stepResults = {}; // id -> {ok, result} | {ok:false, error}
  const record = [];

  function fail(id, error) {
    const failure = { id, ok: false, error };
    stepResults[id] = { ok: false, error };
    record.push(failure);
    return failure;
  }

  for (const step of steps) {
    const templateContext = { inputs, steps: stepResults };
    const timeoutMs = Number.isFinite(step.timeout_ms) && step.timeout_ms > 0 ? step.timeout_ms : DEFAULT_STEP_TIMEOUT_MS;

    let outcome;
    try {
      if (typeof step.tool === 'string') {
        const tool = getTool(step.tool);
        if (!tool || tool.meta) {
          throw new Error(`"${step.tool}" isn't a valid pipeline step tool.`);
        }
        const resolvedArgs = resolveTemplates(step.args || {}, templateContext);
        const result = await withTimeout(
          invoke(step.tool, resolvedArgs, { ...ctx, autoConfirm: true }),
          timeoutMs,
          step.id
        );
        outcome = result?.ok === false ? { ok: false, error: result.error || `"${step.tool}" failed.` } : { ok: true, result };
      } else {
        if (typeof askModel !== 'function') {
          throw new Error('This pipeline has a prompt step, but no model call is available to run it.');
        }
        const resolvedPrompt = resolveTemplates(step.prompt, templateContext);
        const modelResult = await withTimeout(
          askModel({ prompt: String(resolvedPrompt), background: Boolean(ctx.background) }),
          timeoutMs,
          step.id
        );
        outcome = modelResult?.ok === false ? { ok: false, error: modelResult.error } : { ok: true, result: { text: modelResult.text } };
      }
    } catch (err) {
      outcome = { ok: false, error: err?.message || 'This step failed.' };
    }

    stepResults[step.id] = outcome;
    record.push({ id: step.id, ...outcome });

    if (!outcome.ok && !step.continue_on_error) {
      return { ok: false, steps: record, error: `Step "${step.id}" failed: ${outcome.error}` };
    }
  }

  return { ok: true, steps: record };
}

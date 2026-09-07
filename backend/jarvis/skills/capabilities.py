"""Turning a Skill folder into something the model can call.

A Skill is not fixed at startup the way a built-in tool is — it can be installed,
edited or removed while the app runs — so this syncs the registry rather than
registering once.

Three shapes, decided by what is actually in the folder:

* **SKILL.md only** — calling it hands back its instructions, for the model to
  carry out with its ordinary tools.
* **skill.toml (with or without SKILL.md)** — calling it RUNS the pipeline's
  fixed steps in order and returns their results; the instructions, if any, ride
  along as context.
* **A pipeline that is invalid or not yet approved** — falls back to
  instructions, with a note saying why it did not run. A broken skill.toml never
  breaks the declaration itself.

Only the frontmatter and the pipeline's shape are read to build a declaration.
The instructions BODY is read when the Skill is actually called — the same
progressive disclosure the format was designed around.
"""

from __future__ import annotations

import logging
from typing import Any

from ..capabilities import CapabilityKind, CapabilityRegistry, CapabilitySpec, Risk
from . import files, pipelines

logger = logging.getLogger(__name__)

_INPUT_JSON_TYPE = {"number": "number", "boolean": "boolean"}
_NO_ARGUMENTS = {"type": "object", "properties": {}, "required": []}


def _load_pipeline(name: str, known_tools: set[str] | None) -> dict[str, Any]:
    """Never raises: this runs for every enabled Skill on every turn, and a
    broken file must not take the declaration list down with it."""
    raw = files.read_skill_toml(name)
    if raw is None:
        return {"hasToml": False, "pipeline": None, "errors": []}
    try:
        parsed = pipelines.parse(raw)
    except ValueError as err:
        return {"hasToml": True, "pipeline": None, "errors": [str(err)]}
    errors = pipelines.validate(parsed, known_tools=known_tools)
    return {"hasToml": True, "pipeline": None if errors else parsed, "errors": errors}


def _parameters(pipeline: pipelines.Pipeline) -> dict[str, Any]:
    """A pipeline's declared inputs as a schema. No inputs means the same
    zero-argument shape a plain instructions-only Skill has always had."""
    properties: dict[str, Any] = {}
    required: list[str] = []
    for declared in pipeline.inputs:
        name = declared.get("name")
        if not name:
            continue
        described = {"type": _INPUT_JSON_TYPE.get(declared.get("type"), "string")}
        if declared.get("description"):
            described["description"] = str(declared["description"])
        properties[name] = described
        if declared.get("required"):
            required.append(name)
    return {"type": "object", "properties": properties, "required": required}


def _instructions_for(folder: dict[str, Any]) -> str:
    body = files.read_skill_body(folder["name"])
    allowed = folder.get("allowedTools") or []
    # Communicated as a note rather than enforced by filtering the live tool
    # list: doing that properly needs the turn loop to track "we are mid-skill"
    # across steps, and a half-enforced restriction is worse than a stated one.
    tools_note = (f"\n\nOnly use these of your abilities while doing this: "
                  f"{', '.join(allowed)}." if allowed else "")
    supporting = files.list_skill_files(folder["name"])
    files_note = (f"\n\nThis skill's folder also has these files: {', '.join(supporting)}. "
                  f'Use read_skill_file (skill: "{folder["name"]}") to open any of them. '
                  f"You can read them, never run them." if supporting else "")
    return body + tools_note + files_note


def skill_spec(folder: dict[str, Any], *, registry: CapabilityRegistry,
               known_tools: set[str] | None = None) -> CapabilitySpec:
    name = folder["name"]
    loaded = _load_pipeline(name, known_tools)
    pipeline = loaded["pipeline"]
    needs_approval = pipeline is not None and folder.get("pipelineApproved") is not True
    runnable = pipeline if pipeline is not None and not needs_approval else None

    def run(ctx: Any = None, **args: Any) -> dict[str, Any]:
        instructions = _instructions_for(folder)
        if runnable is None:
            if needs_approval:
                return {"ok": True, "pipelineNeedsApproval": True,
                        "instructions": instructions +
                        f"\n\nThis skill also has a pipeline (skill.toml) that hasn't been "
                        f"approved to run yet. If the user wants it enabled, ask, and if "
                        f'they agree, call approve_skill_pipeline (skill: "{name}").'}
            # No pipeline, or one that failed to parse. The errors are surfaced
            # on the Skills screen, not spliced in here: a broken pipeline
            # should not make otherwise-fine instructions read strangely.
            return {"ok": True, "instructions": instructions}

        missing = [str(i["name"]) for i in runnable.inputs
                   if i.get("required") and i.get("name") not in args]
        if missing:
            # Caught here rather than surfacing deep inside the first step that
            # references one, as an unresolved-reference error nobody can act on.
            plural = "s" if len(missing) > 1 else ""
            listed = ", ".join(f'"{m}"' for m in missing)
            return {"ok": False, "error": f"Missing required input{plural}: {listed}."}

        result = pipelines.run(runnable, invoke=_step_invoker(ctx), ask=_step_asker(),
                               inputs=args)
        answer = {"ok": result["ok"], "steps": result["steps"], "error": result["error"]}
        if instructions.strip():
            answer["instructions"] = instructions
        return answer

    # Judged against THIS registry, not a global one: a sync against a different
    # catalogue would otherwise silently read another one's risk levels.
    confirm_steps = (pipelines.confirm_requiring_steps(
        runnable, needs_confirmation=lambda name: _needs_confirmation(registry, name))
        if runnable else [])
    description = (folder.get("description")
                   or (pipeline.description if pipeline else None)
                   or f"The {name} skill.")

    return CapabilitySpec(
        id=f"skill.{name}", name=name, description=description,
        input_schema=_parameters(runnable) if runnable else _NO_ARGUMENTS,
        # A pipeline whose steps need a human makes the WHOLE skill confirm
        # once, up front, naming them — rather than pausing halfway through.
        risk=Risk.MEDIUM if confirm_steps else Risk.LOW,
        handler=run, kind=CapabilityKind.SKILL, wants_context=True,
        timeout_s=300.0 if runnable else 20.0,
        summarize=((lambda _args, steps=tuple(confirm_steps):
                    f'Run the "{name}" skill, which includes a step that needs your OK '
                    f"({', '.join(steps)}) — go ahead?") if confirm_steps else None),
        # Folder Skills are few, unlike the built-in catalogue, and a Skill the
        # model does not know exists is one it never uses: confirmed by a live
        # test where a plainly-applicable Skill was passed over for a generic
        # built-in. Always visible is what makes real auto-invocation possible.
        tags=frozenset({"core"}),
    )


def _needs_confirmation(registry: CapabilityRegistry, tool_name: str) -> bool:
    spec = registry.get(tool_name)
    return spec is not None and spec.risk is not Risk.LOW


def _step_invoker(ctx: Any):
    """Run a pipeline's tool step through the REAL gate.

    Not a direct handler call: a step naming a confirm-gated or high-risk tool
    has to meet exactly the same policy any other call to it would.
    """
    def invoke(tool_name: str, args: dict[str, Any]) -> Any:
        from ..capabilities.execute import execute

        if ctx is None:
            raise RuntimeError("a pipeline step has no context to run in")
        result = execute(tool_name, args, ctx)
        if not result.ok:
            raise RuntimeError(result.error or f"{tool_name} did not work.")
        return result.value

    return invoke


def _step_asker():
    def ask(prompt: str) -> str:
        from ..ai import ask_model

        reply = ask_model(prompt)
        if not reply.ok:
            raise RuntimeError(reply.error or "no model was available")
        return reply.text

    return ask


def sync(registry: CapabilityRegistry, *, known_tools: set[str] | None = None) -> list[str]:
    """Register every enabled Skill and drop what is no longer there.

    Called at startup and after anything changes a Skill folder. Unregistering
    matters as much as registering: a deleted Skill still being callable is a
    model calling something that no longer exists.
    """
    if known_tools is None:
        known_tools = {s.name for s in registry.list(kind=CapabilityKind.TOOL)
                       if "meta" not in s.tags}

    wanted: list[str] = []
    for folder in files.list_user_skills():
        if not folder.get("enabled"):
            continue
        try:
            registry.register(skill_spec(folder, registry=registry,
                                         known_tools=known_tools))
            wanted.append(folder["name"])
        except Exception:  # noqa: BLE001 — one bad folder must not hide the rest
            logger.exception("could not register the %s skill", folder.get("name"))

    for spec in registry.list(kind=CapabilityKind.SKILL):
        if spec.name not in wanted:
            registry.unregister(spec.name)
    return sorted(wanted)

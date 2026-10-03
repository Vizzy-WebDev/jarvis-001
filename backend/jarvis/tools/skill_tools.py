"""Working with Skills: writing one, reading its files, running its scripts.

The two consent gates here are separate and stay separate. `approve_skill_scripts`
lets a Skill's own helper scripts run in the sandbox at all;
`approve_skill_pipeline` lets a `skill.toml` that ARRIVED from elsewhere execute
its steps. Different mechanisms, different defaults — a Skill written in the app
starts with its pipeline approved because the user wrote and reviewed it, and one
that was downloaded never does.

Both are one-time per Skill rather than the per-call confirmation everything else
uses: re-asking on every run would make a Skill unusable, and the question here is
genuinely about the Skill, not about this particular call.
"""

from __future__ import annotations

from typing import Any

from ..capabilities import CapabilitySpec, Risk

MAX_SCRIPT_TIMEOUT_S = 60.0
#: Scripts a Skill can carry. A Skill's helper script is Python here, matching
#: the sandbox: shipping a runner for a language the sandbox cannot isolate
#: would be a promise it cannot keep.
RUNNABLE_SUFFIXES = (".py",)


def _reserved_names(registry: Any) -> set[str]:
    """Names already taken by a built-in or a connector tool.

    The registry is passed IN, never imported: the loader imports every tool
    module, so a tool reaching for the shared registry is the cycle the loader
    invariant exists to prevent. Skills are checked against it so one can never
    shadow a real ability — a Skill named `get_time` would be silently answered
    by the wrong thing.
    """
    from ..capabilities import CapabilityKind

    return {s.name for s in registry.list() if s.kind is not CapabilityKind.SKILL}


def _create(registry: Any, name: str = "", description: str = "",
            instructions: str = "") -> dict[str, Any]:
    from ..skills import capabilities as skill_capabilities
    from ..skills import files

    try:
        skill = files.create_skill(name=name, description=description,
                                   instructions=instructions,
                                   reserved=_reserved_names(registry))
    except ValueError as err:
        return {"ok": False, "error": str(err)}

    # Callable the moment it exists: a Skill is not fixed at startup the way a
    # built-in is, and "you'll have to restart" would be a strange thing to say
    # about something just written.
    skill_capabilities.sync(registry)
    return {"ok": True, "name": skill["name"],
            "spoken_hint": "Say it's saved and can be used from now on, by name."}


def _read_file(skill: str = "", path: str = "") -> dict[str, Any]:
    from ..skills import files

    try:
        return {"ok": True, "skill": skill, "path": path,
                "content": files.read_skill_file(skill, path)}
    except FileNotFoundError:
        return {"ok": False, "error": f'"{path}" is not in that skill\'s folder.'}
    except ValueError as err:
        return {"ok": False, "error": str(err)}


def _approve_scripts(skill: str = "") -> dict[str, Any]:
    from ..skills import files

    folder = files.get_skill(skill)
    if folder is None:
        return {"ok": False, "error": "There's no skill by that name."}
    files.update_skill_state(skill, {"scriptsApproved": True})
    return {"ok": True, "skill": skill,
            "note": "Its helper scripts can run from now on. This was asked once, not per run."}


def _approve_pipeline(registry: Any, skill: str = "") -> dict[str, Any]:
    from ..skills import capabilities as skill_capabilities
    from ..skills import files

    folder = files.get_skill(skill)
    if folder is None:
        return {"ok": False, "error": "There's no skill by that name."}
    if not folder.get("hasToml"):
        return {"ok": False, "error": "That skill doesn't have a pipeline to approve."}
    files.update_skill_state(skill, {"pipelineApproved": True})
    # Its declaration changes shape once the pipeline may run: it gains the
    # pipeline's inputs, and possibly a confirmation requirement.
    skill_capabilities.sync(registry)
    return {"ok": True, "skill": skill,
            "note": "Its pipeline can run from now on."}


def _run_script(skill: str = "", path: str = "",
                args: list[str] | None = None) -> dict[str, Any]:
    from pathlib import Path

    from ..sandbox.runner import describe_isolation, run_python
    from ..skills import files

    folder = files.get_skill(skill)
    if folder is None:
        return {"ok": False, "error": "There's no skill by that name."}
    if not folder.get("scriptsApproved"):
        # No sandbox run at all until this is answered — the point of a one-time
        # gate is that the first run is the one that asks.
        return {"ok": False, "needsApproval": True,
                "error": (f'"{skill}" hasn\'t been approved to run its own scripts yet. Ask '
                          f"the user whether that's alright, explaining that it runs in an "
                          f"isolated sandbox, and if they agree call approve_skill_scripts "
                          f'(skill: "{skill}").')}

    if not str(path).lower().endswith(RUNNABLE_SUFFIXES):
        return {"ok": False,
                "error": f'Only {" or ".join(RUNNABLE_SUFFIXES)} scripts can be run — '
                         f'"{path}" is not one, though read_skill_file can still read it.'}

    try:
        code = files.read_skill_file(skill, path)
    except FileNotFoundError:
        return {"ok": False, "error": f'"{path}" is not in that skill\'s folder.'}
    except ValueError as err:
        return {"ok": False, "error": str(err)}

    supporting = {}
    for name in files.list_skill_files(skill):
        if name == path or "/" in name:
            continue
        try:
            supporting[Path(name).name] = files.read_skill_file(skill, name)
        except (ValueError, FileNotFoundError, UnicodeDecodeError):
            continue                     # a binary or oversized sibling is simply not copied

    result = run_python(code, timeout_s=MAX_SCRIPT_TIMEOUT_S, keep_files=False,
                        input_files=supporting)
    return {"ok": result.ok, "skill": skill, "path": path,
            "stdout": result.stdout, "stderr": result.stderr,
            "isolation": describe_isolation(),
            **({"error": result.error} if not result.ok else {})}


def build(registry: Any) -> list[CapabilitySpec]:
    """Built with the registry passed in — see `_reserved_names`."""
    return [
        CapabilitySpec(
            id="builtin.create_skill", name="create_skill",
            description=("Create a new Skill from what the user wants it to do, once you "
                         "understand it well enough to draft one. Use it after talking the "
                         "skill through — never on a first mention. Always read the drafted "
                         "name, description and instructions back before it is confirmed."),
            input_schema={"type": "object", "properties": {
                "name": {"type": "string",
                         "description": ('A short slug: lower-case words separated by hyphens, '
                                         'e.g. "weekly-status-report".')},
                "description": {"type": "string",
                                "description": ("One or two sentences: what it does AND when you "
                                                "should use it.")},
                "instructions": {"type": "string",
                                 "description": "Step-by-step instructions to follow when used."}},
                "required": ["name", "description", "instructions"]},
            # Writing a new callable ability is a change to what Jarvis can do, so
            # it is read back and confirmed rather than done quietly.
            risk=Risk.MEDIUM,
            handler=lambda **args: _create(registry, **args), timeout_s=15.0,
            summarize=lambda args: (f'Create a skill called "{args.get("name")}" — '
                                    f'{args.get("description")}?'),
        ),
        CapabilitySpec(
            id="builtin.read_skill_file", name="read_skill_file",
            description=("Read one of an installed Skill's own supporting files. Use it when a "
                         "Skill's instructions point at a file in its folder. You can read "
                         "them; you never run them this way."),
            input_schema={"type": "object", "properties": {
                "skill": {"type": "string", "description": "The Skill's name."},
                "path": {"type": "string",
                         "description": 'The path inside that folder, e.g. "reference/style.md".'}},
                "required": ["skill", "path"]},
            # Deliberately NOT tagged meta: a background turn drops meta tools, and a
            # Skill's supporting files need to work on a schedule as much as live.
            risk=Risk.LOW, handler=_read_file, timeout_s=10.0,
        ),
        CapabilitySpec(
            id="builtin.run_skill_script", name="run_skill_script",
            description=("Run one of an installed Skill's own helper scripts through the sandbox "
                         "and get its output. Only use a path that Skill's instructions actually "
                         "pointed you to — never guess one. The first use for a given Skill needs "
                         "the user's one-time approval; later runs of that Skill don't ask again."),
            input_schema={"type": "object", "properties": {
                "skill": {"type": "string", "description": "The Skill the script belongs to."},
                "path": {"type": "string",
                         "description": 'Its path inside that folder, e.g. "scripts/build.py".'},
                "args": {"type": "array", "items": {"type": "string"},
                         "description": "Arguments, if the instructions mention any."}},
                "required": ["skill", "path"]},
            risk=Risk.LOW, handler=_run_script, timeout_s=MAX_SCRIPT_TIMEOUT_S + 10,
        ),
        CapabilitySpec(
            id="builtin.approve_skill_scripts", name="approve_skill_scripts",
            description=("Record that the user has agreed to let one Skill's own helper scripts "
                         "run in the sandbox. Only call it after actually asking them and getting "
                         "a yes. Asked once per Skill, never per run."),
            input_schema={"type": "object", "properties": {
                "skill": {"type": "string", "description": "The Skill they agreed to."}},
                "required": ["skill"]},
            risk=Risk.MEDIUM, handler=_approve_scripts, timeout_s=10.0,
            summarize=lambda args: (f'Let the "{args.get("skill")}" skill run its own helper '
                                    f"scripts in the sandbox from now on?"),
        ),
        CapabilitySpec(
            id="builtin.approve_skill_pipeline", name="approve_skill_pipeline",
            description=("Record that the user has agreed to let one Skill's pipeline (its "
                         "skill.toml steps) actually run. Only after asking them and getting a "
                         "yes. Asked once per Skill."),
            input_schema={"type": "object", "properties": {
                "skill": {"type": "string", "description": "The Skill they agreed to."}},
                "required": ["skill"]},
            risk=Risk.MEDIUM,
            handler=lambda **args: _approve_pipeline(registry, **args), timeout_s=10.0,
            summarize=lambda args: (f'Let the "{args.get("skill")}" skill run its own fixed '
                                    f"steps from now on?"),
        ),
    ]

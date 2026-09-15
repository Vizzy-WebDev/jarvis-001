"""A local command-line program as a set of callable tools.

Every command is a SAVED TEMPLATE: a fixed program and a fixed argv list, where
only marked placeholders are filled from the model's arguments. That is the
whole safety property — §35 forbids a hidden unrestricted shell driven by
model-generated text, and a template means the model chooses VALUES, never the
command, never a flag, and never anything the shell would interpret.

No shell at all: the program is executed directly with an argument list, so
quoting, globbing and `;` have no meaning to anything.

**And no credentials.** Saving an API key writes it into this process's own
environment as well as the .env file, so a child that inherits `os.environ` can
read every key the user has configured — `git` had the same access to
`GEMINI_API_KEY` as a sandboxed script would have. The child gets
`jarvis/childenv.py`'s scrubbed environment instead, the same one the sandbox
uses: a program named in a saved template has no business holding the keys to
the model providers. A connector that genuinely needs a credential should be
given that one credential explicitly, never all of them by inheritance.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from typing import Any

from ..childenv import scrubbed_environment

DEFAULT_TIMEOUT_S = 60.0
MAX_OUTPUT_CHARS = 20000

_PLACEHOLDER = re.compile(r"^\{([^}]+)\}$")


def tool_declarations(config: dict[str, Any]) -> list[dict[str, Any]]:
    declarations = []
    for command in (config or {}).get("commands") or []:
        properties: dict[str, Any] = {}
        required: list[str] = []
        for argument in command.get("args") or []:
            if not argument.get("name"):
                continue
            described = {"type": "string"}
            if argument.get("description"):
                described["description"] = str(argument["description"])
            properties[argument["name"]] = described
            if argument.get("required", True):
                required.append(argument["name"])
        declarations.append({
            "name": command["name"],
            "description": (command.get("description")
                            or f'Runs "{config.get("command")} '
                               f'{" ".join(command.get("argv") or [])}".'),
            "parameters": {"type": "object", "properties": properties, "required": required},
        })
    return declarations


def dispatch(name: str, args: dict[str, Any], config: dict[str, Any], *,
             run: Any = None) -> dict[str, Any]:
    command = next((c for c in (config or {}).get("commands") or []
                    if c.get("name") == name), None)
    if command is None:
        raise KeyError(f"Unknown command: {name}")

    program = (config or {}).get("command")
    if not program:
        raise ValueError("That connector has no program to run.")
    if run is None and not shutil.which(program):
        raise FileNotFoundError(f'"{program}" is not installed on this computer.')

    argv: list[str] = []
    for entry in command.get("argv") or []:
        placeholder = _PLACEHOLDER.match(str(entry))
        if not placeholder:
            # A literal word or flag from the saved template. Never influenced
            # by the model — that is what makes this safe to run at all.
            argv.append(str(entry))
            continue
        key = placeholder.group(1)
        value = (args or {}).get(key)
        if value in (None, ""):
            raise ValueError(f'Missing required value "{key}" for this command.')
        argv.append(str(value))

    if run is None:
        # `env` is named rather than swept up in **kw so that the environment a
        # child is given stays visible at the call site — including to the
        # architecture test that checks every launch site has one.
        def run(*, env: dict[str, str] | None = None, **kw: Any) -> Any:  # noqa: ANN401
            return subprocess.run(env=env, **kw)  # noqa: S603 — a list, never a shell string

    try:
        completed = run(args=[program, *argv], capture_output=True, text=True,
                        timeout=command.get("timeoutS") or DEFAULT_TIMEOUT_S,
                        cwd=(config or {}).get("cwd"),
                        env=scrubbed_environment())
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f'"{program}" was still running and was stopped.'}

    stdout = (completed.stdout or "")[:MAX_OUTPUT_CHARS]
    stderr = (completed.stderr or "")[:MAX_OUTPUT_CHARS]
    if completed.returncode != 0:
        return {"ok": False, "exitCode": completed.returncode, "stdout": stdout,
                "stderr": stderr,
                "error": f'"{program}" exited with code {completed.returncode}: '
                         f"{stderr or stdout or 'no output'}"}
    return {"ok": True, "stdout": stdout, "stderr": stderr}

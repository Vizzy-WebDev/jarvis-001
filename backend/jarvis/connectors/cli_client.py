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

import json
import re
import shutil
import subprocess
from typing import Any, Callable

from ..childenv import scrubbed_environment

DEFAULT_TIMEOUT_S = 60.0
CHECK_TIMEOUT_S = 30.0
HELP_TIMEOUT_S = 20.0
MAX_OUTPUT_CHARS = 20000
MAX_PROPOSED = 12

_PLACEHOLDER = re.compile(r"^\{([^}]+)\}$")
_NAME = re.compile(r"[^a-z0-9_]+")

#: A `.cmd`/`.bat` file cannot be started without Windows' command interpreter,
#: which re-reads the arguments and treats these characters as instructions
#: (the "BatBadBut" class of bug). A value the MODEL supplies is refused if it
#: holds one when the target is such a script; an `.exe` is started directly and
#: never re-parsed, so it has no such limit.
BATCH_EXTENSIONS = (".cmd", ".bat")
_BATCH_UNSAFE = re.compile(r'[&|<>^%!"\r\n]')


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


def resolve_program(program: str) -> str | None:
    """The full path of an installed program, or None.

    By full path, not by bare name: on Windows a program installed through npm
    (`vercel`, `supabase`, `npm` itself) is a `.cmd` script, and starting it by
    its bare name fails with "The system cannot find the file specified" — the
    lookup that finds it only runs when the full path is given.
    """
    return shutil.which(program) if program else None


def child_environment(config: dict[str, Any]) -> dict[str, str]:
    """The scrubbed environment, plus only the key(s) given to THIS program.

    A CLI that reads its credential from an environment variable gets exactly
    that one, by name, from `.env` — never the user's other keys by inheritance.
    """
    from ..config import get_secret

    env = scrubbed_environment()
    for name, secret_ref in ((config or {}).get("env") or {}).items():
        value = get_secret(str(secret_ref)) if secret_ref else None
        if value and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(name)):
            env[str(name)] = value
    return env


def dispatch(name: str, args: dict[str, Any], config: dict[str, Any], *,
             run: Any = None, resolve: Callable[[str], str | None] | None = None) -> dict[str, Any]:
    command = next((c for c in (config or {}).get("commands") or []
                    if c.get("name") == name), None)
    if command is None:
        raise KeyError(f"Unknown command: {name}")

    program = (config or {}).get("command")
    if not program:
        raise ValueError("That connector has no program to run.")
    if resolve is None:
        resolve = resolve_program if run is None else (lambda p: p)
    target = resolve(program)
    if not target:
        raise FileNotFoundError(f'"{program}" is not installed on this computer.')
    is_batch = str(target).lower().endswith(BATCH_EXTENSIONS)

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
        if is_batch and _BATCH_UNSAFE.search(str(value)):
            raise ValueError(
                f'"{key}" cannot contain & | < > ^ % ! or quotes: "{program}" is started '
                "through Windows' command interpreter, which would treat them as instructions.")
        argv.append(str(value))

    if run is None:
        # `env` is named rather than swept up in **kw so that the environment a
        # child is given stays visible at the call site — including to the
        # architecture test that checks every launch site has one.
        def run(*, env: dict[str, str] | None = None, **kw: Any) -> Any:  # noqa: ANN401
            return subprocess.run(env=env, **kw)  # noqa: S603 — a list, never a shell string

    try:
        completed = run(args=[target, *argv], capture_output=True, text=True,
                        encoding="utf-8", errors="replace",
                        timeout=command.get("timeoutS") or DEFAULT_TIMEOUT_S,
                        cwd=(config or {}).get("cwd"),
                        env=child_environment(config))
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


# --- connecting: installed? signed in? ------------------------------------------

def _run_capture(target: str, argv: list[str], config: dict[str, Any],
                 timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run([target, *argv], capture_output=True, text=True,  # noqa: S603
                          encoding="utf-8", errors="replace", timeout=timeout,
                          cwd=(config or {}).get("cwd"), env=child_environment(config))


def check(config: dict[str, Any]) -> dict[str, Any]:
    """Whether this CLI can be used right now: `{ok, detail}`.

    Installed first — a missing program is the commonest failure and has its own
    plain answer, with the install command when the connector knows it. Then the
    connector's own test command (e.g. an account status), which is what proves
    a CLI that signs in is actually signed in; with none, the program's
    `--version` answering is the whole test.
    """
    program = (config or {}).get("command")
    if not program:
        return {"ok": False, "detail": "No program is set for this connector."}
    target = resolve_program(program)
    if not target:
        install = (config or {}).get("install")
        hint = f" Install it with: {install}" if install else ""
        return {"ok": False, "installed": False,
                "detail": f'"{program}" is not installed on this computer.{hint}'}
    argv = [str(a) for a in ((config or {}).get("test") or ["--version"])]
    try:
        done = _run_capture(target, argv, config, CHECK_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return {"ok": False, "installed": True,
                "detail": f'"{program} {" ".join(argv)}" did not answer in time.'}
    except OSError as err:
        return {"ok": False, "installed": True, "detail": f'Couldn\'t start "{program}": {err}'}
    output = ((done.stdout or "") + (done.stderr or "")).strip()
    if done.returncode != 0:
        first = output.splitlines()[0] if output else f"exit code {done.returncode}"
        signin = " Use Sign in to connect it." if (config or {}).get("login") else ""
        return {"ok": False, "installed": True, "detail": f"{first[:300]}{signin}"}
    return {"ok": True, "installed": True, "detail": None}


def start_login(config: dict[str, Any]) -> dict[str, Any]:
    """Start the CLI's OWN sign-in (it opens the browser itself) and return at once.

    The CLI keeps the resulting credential where it always does; Jarvis never
    sees it. The caller checks back with `check()` until it passes, the same way
    an MCP connection waits for its OAuth callback.
    """
    program = (config or {}).get("command")
    login = (config or {}).get("login")
    if not login:
        raise ValueError("This connector has no sign-in step.")
    target = resolve_program(program or "")
    if not target:
        raise FileNotFoundError(f'"{program}" is not installed on this computer.')
    subprocess.Popen([target, *[str(a) for a in login]],  # noqa: S603 — a list, never a shell
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     stdin=subprocess.DEVNULL, env=child_environment(config))
    return {"ok": True}


# --- which commands a CLI offers --------------------------------------------------

def _placeholders(argv: list[Any]) -> list[str]:
    return [m.group(1) for m in (_PLACEHOLDER.match(str(a)) for a in argv) if m]


def validate_commands(commands: Any) -> list[dict[str, Any]]:
    """Saved command templates, checked and normalised; raises ValueError.

    A template is a program's own words and flags plus `{placeholders}` the model
    fills. Each argv entry is ONE argument — no entry may hold whitespace (that
    would be several words pretending to be one, which is how a flag gets
    smuggled into a "value") — and every placeholder is described in `args`.
    """
    if not isinstance(commands, list):
        raise ValueError("Commands must be a list.")
    out, seen = [], set()
    for raw in commands:
        if not isinstance(raw, dict):
            raise ValueError("Each command must be an object.")
        name = _NAME.sub("_", str(raw.get("name") or "").lower()).strip("_")[:60]
        if not name:
            raise ValueError("Every command needs a name.")
        if name in seen:
            raise ValueError(f'Two commands are both called "{name}".')
        seen.add(name)
        argv = raw.get("argv")
        if not isinstance(argv, list) or not argv:
            raise ValueError(f'"{name}" needs at least one word after the program name.')
        argv = [str(a) for a in argv]
        for entry in argv:
            if not entry or re.search(r"\s", entry):
                raise ValueError(f'"{name}": each part must be one word, flag or '
                                 "{placeholder} — no spaces.")
        described = {str(a.get("name")): a for a in raw.get("args") or []
                     if isinstance(a, dict) and a.get("name")}
        args = []
        for key in _placeholders(argv):
            given = described.get(key) or {}
            args.append({"name": key,
                         "description": str(given.get("description") or f"The {key}."),
                         "required": given.get("required", True) is not False})
        command = {"name": name, "description": str(raw.get("description") or "").strip(),
                   "argv": argv, "args": args}
        if raw.get("timeoutS"):
            command["timeoutS"] = max(5.0, min(float(raw["timeoutS"]), 1800.0))
        out.append(command)
    return out


DISCOVERY_SYSTEM = (
    "You turn a command-line program's own --help output into a small, safe list of commands "
    "Jarvis could offer as tools. Reply with JSON only: {\"commands\": [{\"name\": "
    "\"short_snake_case_name\", \"description\": \"one plain sentence\", \"argv\": "
    "[\"literal-or-{placeholder}\", ...], \"args\": [{\"name\": \"placeholder\", "
    "\"description\": \"...\"}]}]}. Rules: only propose commands that actually appear in the "
    "given help text — never invent one. argv is everything AFTER the program name. Every "
    "\"{placeholder}\" in argv must have a matching entry in \"args\". Each argv entry is a "
    "single literal word or flag, or a single {placeholder} — never a shell operator, pipe, "
    "redirect or semicolon, and never several words in one entry. Prefer read-only commands "
    "and a machine-readable output flag (like --json) when the help text offers one. Propose "
    f"at most {MAX_PROPOSED} of the most generally useful commands."
)


def discover_commands(config: dict[str, Any], *, ask: Callable[..., Any] | None = None
                      ) -> dict[str, Any]:
    """Read `<program> --help` and propose command templates for the person to review.

    Never saves anything: the proposal comes back to the screen, where the person
    ticks what to keep. Every proposal is re-checked against the real help text —
    a command whose first word the help never mentions is dropped, because a
    model filling a gap is exactly what must not become a tool.
    """
    program = (config or {}).get("command")
    target = resolve_program(program or "")
    if not target:
        raise FileNotFoundError(f'"{program}" is not installed on this computer.')
    try:
        done = _run_capture(target, ["--help"], config, HELP_TIMEOUT_S)
    except subprocess.TimeoutExpired as err:
        raise ValueError(f'"{program} --help" did not finish in time.') from err
    help_text = ((done.stdout or "") + "\n" + (done.stderr or "")).strip()
    if not help_text:
        raise ValueError(f'"{program}" printed no help text to read.')

    if ask is None:
        from ..ai import ask as ask_model

        ask = ask_model
    answer = ask(f'Here is the real --help output for "{program}":\n\n{help_text[:6000]}',
                 system=DISCOVERY_SYSTEM, want_json=True)
    data = getattr(answer, "data", None)
    if data is None:
        try:
            data = json.loads(getattr(answer, "text", "") or "")
        except ValueError:
            data = None
    proposed_raw = (data or {}).get("commands") if isinstance(data, dict) else None
    words = set(re.findall(r"[A-Za-z0-9][A-Za-z0-9_-]*", help_text.lower()))
    proposed = []
    for raw in (proposed_raw or [])[:MAX_PROPOSED * 2]:
        try:
            command = validate_commands([raw])[0]
        except ValueError:
            continue
        first = command["argv"][0]
        if not first.startswith("-") and not _PLACEHOLDER.match(first) \
                and first.lower() not in words:
            continue
        proposed.append(command)
        if len(proposed) >= MAX_PROPOSED:
            break
    return {"proposed": proposed, "helpText": help_text[:4000]}

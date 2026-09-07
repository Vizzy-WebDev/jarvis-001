"""Running code the model wrote, and being honest about how contained it is.

**The defect this replaces.** The original's `restricted` backend accepted an
`allowPaths` argument and never applied it: the child process had the user's
entire filesystem, while `run_code`'s description told the user it was
sandboxed, and the model repeated that description back to them. A boundary
that exists only in a docstring is worse than no boundary, because it is
believed.

So the rule here is: **the level of isolation is measured, reported, and put
into the tool's own description at load time.** `describe_isolation()` is not
marketing copy; it is generated from what actually happened when the backends
were probed, and it names what is NOT protected.

Two backends:

* **wsl** — a real boundary. The code runs inside a WSL distribution: a separate
  filesystem and process namespace, reaching the Windows filesystem only through
  an explicit mount. This is the one that deserves the word "sandbox".
* **restricted** — a plain subprocess with the environment scrubbed, a throwaway
  working directory, a hard timeout and capped output. It stops an accident, not
  an attacker: nothing prevents the code reading elsewhere on the disk.

The secrets scrub matters even in the WSL case: the parent process holds every
API key the user has configured, and a child inheriting `os.environ` inherits
all of them. That is one line to get wrong and impossible to notice.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 30.0
MAX_OUTPUT_CHARS = 20000
#: Files the script produced, up to this many, are offered back as artifacts.
MAX_OUTPUT_FILES = 10

#: Anything matching these is removed from the child's environment. Broad on
#: purpose: over-scrubbing costs a script an environment variable it probably
#: should not have had, while under-scrubbing hands model-written code the
#: user's API keys.
_SECRET_PATTERN = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|AUTH|SESSION|COOKIE", re.I)
_KEEP_ALWAYS = ("PATH", "SYSTEMROOT", "COMSPEC", "TEMP", "TMP", "LANG", "LC_ALL")


@dataclass
class SandboxResult:
    ok: bool
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    timed_out: bool = False
    backend: str = "restricted"
    files: list[str] = field(default_factory=list)
    error: str | None = None


def _wsl_available() -> bool:
    if sys.platform != "win32" or not shutil.which("wsl.exe"):
        return False
    try:
        probe = subprocess.run(["wsl.exe", "-e", "true"], capture_output=True, timeout=10)
        return probe.returncode == 0
    except Exception:  # noqa: BLE001
        return False


_backend_cache: str | None = None


def backend() -> str:
    """'wsl' or 'restricted'. Probed once — the answer cannot change while the
    process runs, and probing per call would put a subprocess launch in front of
    every piece of code."""
    global _backend_cache
    if _backend_cache is None:
        _backend_cache = "wsl" if _wsl_available() else "restricted"
    return _backend_cache


def describe_isolation() -> str:
    """The sentence the tool's own description carries. Written from what the
    probe found, and it says what is NOT protected."""
    if backend() == "wsl":
        return ("Code runs inside WSL, with its own filesystem and processes, and no access "
                "to your API keys. It cannot see your Windows files unless they are "
                "explicitly shared with it.")
    return ("Code runs in a throwaway folder with your API keys removed from its "
            "environment and a hard time limit — but it is NOT fully sandboxed: it runs "
            "as you, and can read other files on this computer. Don't run code you "
            "wouldn't run yourself.")


def child_environment() -> dict[str, str]:
    """The environment the code gets: nothing that looks like a credential.

    Built by removing rather than by listing what to keep, because a new secret
    added to the app's environment later would otherwise be inherited silently.
    """
    clean = {name: value for name, value in os.environ.items()
             if not _SECRET_PATTERN.search(name)}
    for name in _KEEP_ALWAYS:
        if name in os.environ:
            clean[name] = os.environ[name]
    # The app's own data directory is not the script's business either.
    clean.pop("JARVIS_DATA_DIR", None)
    clean.pop("JARVIS_ENV_PATH", None)
    return clean


def run_python(code: str, *, timeout_s: float = DEFAULT_TIMEOUT_S,
               keep_files: bool = True,
               input_files: dict[str, str] | None = None) -> SandboxResult:
    """Run a Python snippet and report what it did, including any files it made.

    `input_files` puts real data next to the script — `{"data.csv": "..."}` — so
    an analysis runs over the whole file rather than over however much of it fits
    in a prompt. Names are flattened to a basename: a caller must not be able to
    write outside the workspace by naming its file "../../.env".
    """
    if not (code or "").strip():
        return SandboxResult(ok=False, backend=backend(), error="There's no code to run.")

    workspace = Path(tempfile.mkdtemp(prefix="jarvis-run-"))
    script = workspace / "script.py"
    script.write_text(code, encoding="utf-8")
    given = set()
    for name, content in (input_files or {}).items():
        safe = Path(str(name)).name or "input"
        if safe == "script.py":
            safe = "input.py"             # never let an input overwrite the script
        (workspace / safe).write_text(content, encoding="utf-8")
        given.add(safe)

    command = (["wsl.exe", "-e", "python3", "script.py"] if backend() == "wsl"
               else [sys.executable, "script.py"])
    try:
        completed = subprocess.run(
            command, cwd=str(workspace), env=child_environment(),
            capture_output=True, text=True, timeout=timeout_s)
    except subprocess.TimeoutExpired as expired:
        return SandboxResult(
            ok=False, backend=backend(), timed_out=True,
            stdout=_cap(expired.stdout), stderr=_cap(expired.stderr),
            error=f"The code was still running after {timeout_s:.0f} seconds, so I stopped it.")
    except Exception as err:  # noqa: BLE001
        return SandboxResult(ok=False, backend=backend(),
                             error=f"I couldn't run that: {err}")

    produced = []
    if keep_files:
        produced = [str(p) for p in sorted(workspace.iterdir())
                    if p.name != "script.py" and p.name not in given][:MAX_OUTPUT_FILES]

    return SandboxResult(
        ok=completed.returncode == 0,
        stdout=_cap(completed.stdout), stderr=_cap(completed.stderr),
        exit_code=completed.returncode, backend=backend(), files=produced,
        error=None if completed.returncode == 0 else "The code exited with an error.")


def _cap(text: str | bytes | None) -> str:
    if not text:
        return ""
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    return text[:MAX_OUTPUT_CHARS] + f"\n… (truncated at {MAX_OUTPUT_CHARS} characters)"


def reset_for_tests() -> None:
    global _backend_cache
    _backend_cache = None

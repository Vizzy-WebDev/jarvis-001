"""What a child process is allowed to see of this process's environment.

One definition, used by everything that launches a program: the sandbox running
model-written code, and a CLI connector running a saved command template. It
lived inside `sandbox/runner.py` first, which is where the problem was noticed —
but the parent holds every API key the user has configured, and a child
inheriting `os.environ` inherits all of them regardless of which subsystem
started it. A CLI connector's `git` had the same access to `GEMINI_API_KEY` as a
sandboxed script would have had, for the same reason and with none of the care.

Built by REMOVING rather than by listing what to keep: a secret added to the
app's environment later is scrubbed without anyone remembering to update a list,
and the failure direction of an over-broad pattern is a child missing an
environment variable it probably should not have had.

**Two deliberate exceptions**, both in `tools/`: `open_app.py` hands a target to
the Windows shell and `_http.py` hands a URL to the desktop's own browser. That
child is the user's own application — not model-written code, not a program a
connector template names — and it needs the real environment to behave normally.
`tests/test_architecture.py` names them, so a third one cannot appear quietly.

A zero-import leaf on purpose: anything may use it, including modules that must
stay importable in isolation.
"""

from __future__ import annotations

import os
import re

#: Anything matching this is removed from a child's environment. Broad on
#: purpose — see the module docstring on which direction it is safe to fail in.
SECRET_PATTERN = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|AUTH|SESSION|COOKIE", re.I)

#: Kept even if the pattern matched: without these a child cannot find a program
#: to run or a place to write a temporary file.
KEEP_ALWAYS = ("PATH", "SYSTEMROOT", "COMSPEC", "TEMP", "TMP", "LANG", "LC_ALL")

#: The app's own storage is not a child process's business either — and a test
#: run's scratch directory leaking into a child is how a child ends up writing
#: into the real one.
DROP_ALWAYS = ("JARVIS_DATA_DIR", "JARVIS_ENV_PATH")


def scrubbed_environment() -> dict[str, str]:
    """This process's environment with nothing that looks like a credential."""
    clean = {name: value for name, value in os.environ.items()
             if not SECRET_PATTERN.search(name)}
    for name in KEEP_ALWAYS:
        if name in os.environ:
            clean[name] = os.environ[name]
    for name in DROP_ALWAYS:
        clean.pop(name, None)
    return clean

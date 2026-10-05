"""Jarvis — a local voice/text personal assistant.

Persistence formats here (`data/*.json`, `.env`, the SQLite database) are kept stable so an existing
install's data keeps loading with no conversion step.

**One package, kept in several folders.** `jarvis` is a single Python package, but its parts live in
the group folders at the repository root (`conversation/`, `background/`, ...) so the major systems
are visible there. Each group folder is added to this package's search path below — the documented
`__path__` mechanism — so `jarvis.jobs` is `background/jobs/` and `jarvis.durable` is
`background/durable.py`. Module names do not depend on the folder, which means a relative import
(`from ..db import ...`) still resolves by NAME to `jarvis.db`, wherever that file sits.

A module name must exist in exactly one place: if two folders both held `jobs`, the first one found
would win silently. `tests/test_layout.py` fails on any such duplicate.
"""

from .paths import REPO_ROOT

__version__ = "0.1.0"

#: The group folders at the repository root, in search order after this folder itself. The one
#: list of them: the layout test and anything that ships Jarvis read it from here.
GROUPS = ("conversation", "intelligence", "abilities", "background", "self_awareness", "speech",
          "content")

__path__ += [str(REPO_ROOT / group) for group in GROUPS]

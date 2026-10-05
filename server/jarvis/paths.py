"""Where Jarvis's own folders are: the one place that knows the repository's shape.

Code used to find `data/`, `.env` and the built front end by counting folders up from its own
file, which silently changes meaning the moment a file moves. Everything that needs one of these
locations asks here instead. The overrides (`JARVIS_DATA_DIR`, `JARVIS_ENV_PATH`) are still read by
the modules that use them, on every call, exactly as before.

A leaf on purpose — it imports nothing of Jarvis — because the persistence leaves (`store.py`,
`config.py`) read it and must stay loadable on their own (`tests/test_architecture.py`).
"""

from __future__ import annotations

from pathlib import Path

#: The repository root: this file is `server/jarvis/paths.py`.
REPO_ROOT = Path(__file__).resolve().parent.parent.parent

#: Where the person's data lives when `JARVIS_DATA_DIR` is not set.
DEFAULT_DATA_DIR = REPO_ROOT / "data"

#: Where secrets live when `JARVIS_ENV_PATH` is not set.
DEFAULT_ENV_PATH = REPO_ROOT / ".env"

#: The built Next.js export the server hands the browser.
FRONTEND_DIR = REPO_ROOT / "frontend" / "out"

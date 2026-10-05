"""Discovery at startup — once, in the background, off the request path.

Behind `JARVIS_MODEL_DISCOVERY`, off by default like every other thing that starts
itself (`assembly.start_background_work`), so a test's own app never reaches out
to a real server unasked. A real launch (`main()`) turns it on.
"""

from __future__ import annotations

import os

ENABLE_ENV = "JARVIS_MODEL_DISCOVERY"


def start() -> bool:
    if os.environ.get(ENABLE_ENV) != "1":
        return False
    from .discovery import refresh_in_background

    refresh_in_background()
    return True

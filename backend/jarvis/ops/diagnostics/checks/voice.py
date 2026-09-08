"""Is the wake word's dependency actually installed?

Deliberately checks that the package can be FOUND, not that the model loads:
loading downloads a model and costs real seconds, and a diagnostic that
expensive would be either run too rarely to matter or a cost of its own. A
missing package is the failure this can honestly detect from here; a model that
will not load is reported by the detector's own status, where the user can see
it in context.
"""

from __future__ import annotations

import importlib.util
from typing import Any

from ..registry import Check


def probe() -> dict[str, Any]:
    if importlib.util.find_spec("openwakeword") is None:
        return {"ok": False,
                "detail": ("The wake-word package is not installed, so \"hey Jarvis\" "
                           "cannot work — voice still works if you press to talk.")}
    return {"ok": True}


CHECK = Check(id="voice", probe=probe)

"""Built-in capabilities, auto-loaded.

Adding an ability is one file here that exposes `SPEC` (a `CapabilitySpec`), or
`SPECS` for several, or `build(registry)` when it needs the registry itself.
Nothing else in the app changes.

**The import invariant this package must never break.** Nothing under
`jarvis/tools/` may import the loader, the executor, the orchestrator or the
gateway — directly or transitively. The loader imports every module here, so an
import back is a cycle; in the Node original the equivalent produced a deadlock
that looked like a hung server. A tool that needs something only the registry can
answer receives it through `build(registry)`, which is called WITH the registry
rather than reaching for it. `tests/test_architecture.py` asserts this rather
than trusting it.

**Risk (§7) is required on every tool**, with no default, so a new capability
cannot be treated as safe by forgetting to say what it is.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
from typing import Any

from ..capabilities import CapabilityRegistry, CapabilitySpec
from ..capabilities import registry as default_registry

logger = logging.getLogger(__name__)


def load_tools(registry: CapabilityRegistry | None = None) -> list[str]:
    """Register every tool module in this package. Returns the names loaded.

    The returned list is logged at startup on purpose: "did every tool file
    actually load" is otherwise invisible until something tries to call one.
    """
    target = registry or default_registry
    loaded: list[str] = []

    for info in pkgutil.iter_modules(__path__):
        if info.name.startswith("_"):
            continue
        module = importlib.import_module(f"{__name__}.{info.name}")
        for spec in _specs_of(module, target):
            target.register(spec)
            loaded.append(spec.name)

    logger.info("[tools] loaded %d: %s", len(loaded), ", ".join(sorted(loaded)))
    return sorted(loaded)


def _specs_of(module: Any, registry: CapabilityRegistry) -> list[CapabilitySpec]:
    builder = getattr(module, "build", None)
    if callable(builder):
        return list(builder(registry))
    if isinstance(getattr(module, "SPECS", None), (list, tuple)):
        return list(module.SPECS)
    spec = getattr(module, "SPEC", None)
    if isinstance(spec, CapabilitySpec):
        return [spec]
    logger.warning("[tools] %s exposes no SPEC/SPECS/build — skipped", module.__name__)
    return []

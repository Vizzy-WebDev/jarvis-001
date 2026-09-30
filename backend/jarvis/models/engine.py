"""Where the pieces meet: the current catalog, and the route a request would take.

`route()` is the one function both `explain_route` and the real call use to decide
the order endpoints are tried in, so what `explain_route` says is what happens.
"""

from __future__ import annotations

import threading

from . import config, drivers, state
from .catalog import Catalog, build
from .errors import NoEligibleEndpoint
from .resolve import Candidate, resolve
from .router import DefaultRouter, Router
from .types import Explanation, Rejection, Request

_lock = threading.Lock()
_catalog: tuple[object, object, Catalog] | None = None
router: Router = DefaultRouter()


def catalog(cfg: config.Config | None = None) -> Catalog:
    """Config ⊕ the last discovery ⊕ probes, rebuilt only when one of them changed."""
    global _catalog
    cfg = cfg or config.current()
    version = state.catalog_version()
    with _lock:
        if _catalog is not None and _catalog[0] is cfg and _catalog[1] == version:
            return _catalog[2]
    built = build(
        cfg.connections, cfg.aliases,
        driver_defaults={name: getattr(module, "DEFAULT_CAPABILITIES", {}) for name, module in drivers.DRIVERS.items()},
        quirk_caps={name: dict(p.capabilities) for name, p in cfg.quirk_profiles.items()},
        discovered={name: state.discovered(name) for name in cfg.connections},
        probed=state.probed())
    with _lock:
        _catalog = (cfg, version, built)
    return built


def route(request: Request, cfg: config.Config | None = None) -> tuple[list[Candidate], list[Rejection]]:
    """The eligible endpoints in the order they will be tried, and every rejection."""
    cfg = cfg or config.current()
    cat = catalog(cfg)
    eligible, rejected = resolve(request, cfg, cat)
    return router.rank(request, eligible, cfg, cat), rejected


def explain(request: Request) -> Explanation:
    ranked, rejected = route(request)
    return Explanation(ranked=tuple(c.endpoint.id for c in ranked), rejected=tuple(rejected),
                       emulated=tuple(c.endpoint.id for c in ranked if c.emulated))


def nothing_eligible(request: Request, rejected: list[Rejection], cat: Catalog) -> NoEligibleEndpoint:
    """The plain-language 'nothing can take this', naming why."""
    pin = request.requirements.pin
    if pin is not None and not cat.resolve_alias(pin):
        return NoEligibleEndpoint(
            f"The model this asked for (“{pin}”) isn't set up. Choose one on the Model Settings screen.",
            rejections=tuple(rejected))
    if not cat.endpoints:
        return NoEligibleEndpoint("No model is connected yet. Connect one on the Model Settings screen.",
                                  rejections=tuple(rejected))
    relevant = [r for r in rejected if r.reason != "not_pinned"]
    if pin is not None and relevant:
        why = relevant[0].detail
        return NoEligibleEndpoint(f"The model you picked can't take this request. {why}",
                                  rejections=tuple(rejected))
    reasons = sorted({r.detail for r in relevant})
    summary = " ".join(reasons[:3]) + (" …" if len(reasons) > 3 else "")
    return NoEligibleEndpoint(f"None of the connected models can take this request. {summary}".strip(),
                              rejections=tuple(rejected))

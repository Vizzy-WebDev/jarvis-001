"""Route: put the eligible endpoints in the order they will be tried.

(a) the request's `prefer` aliases, (b) the task class's route (else the default
route), (c) every other eligible endpoint — only when that route says
`allow_others`. `optimize` then orders them: quality keeps that order, cost sorts
by price, speed by measured latency (falling back to configured values); ties are
broken by the other two. An endpoint the request's affinity key last used goes
first while it stays eligible.

Kept behind the small `Router` interface so a different ranking can replace this
one without touching resolve or execute.
"""

from __future__ import annotations

import math
import threading
from collections import OrderedDict
from typing import Protocol

from . import state
from .catalog import Catalog, Endpoint
from .config import Config
from .resolve import Candidate
from .types import Request


class Router(Protocol):
    def rank(self, request: Request, candidates: list[Candidate], cfg: Config, cat: Catalog) -> list[Candidate]: ...

    def remember(self, affinity_key: str | None, endpoint_id: str) -> None: ...


def price_key(endpoint: Endpoint) -> float:
    if endpoint.pricing is None:
        return math.inf  # unknown is not cheap
    return endpoint.pricing.input + endpoint.pricing.output


def speed_key(endpoint: Endpoint) -> float:
    measured = state.latency_ms(endpoint.id)
    if measured is not None:
        return float(measured)
    return float(endpoint.latency_ms) if endpoint.latency_ms is not None else math.inf


class DefaultRouter:
    def __init__(self, affinity_size: int = 2000) -> None:
        self._affinity: OrderedDict[str, str] = OrderedDict()
        self._size = affinity_size
        self._lock = threading.Lock()

    def remember(self, affinity_key: str | None, endpoint_id: str) -> None:
        if not affinity_key:
            return
        with self._lock:
            self._affinity[affinity_key] = endpoint_id
            self._affinity.move_to_end(affinity_key)
            while len(self._affinity) > self._size:
                self._affinity.popitem(last=False)

    def affinity(self, affinity_key: str | None) -> str | None:
        if not affinity_key:
            return None
        with self._lock:
            return self._affinity.get(affinity_key)

    def rank(self, request: Request, candidates: list[Candidate], cfg: Config, cat: Catalog) -> list[Candidate]:
        by_id = {c.endpoint.id: c for c in candidates}
        route = cfg.route_for(request.task_class)
        optimize = request.optimize or route.optimize

        def from_aliases(names: tuple[str, ...]) -> list[list[Candidate]]:
            groups = []
            for name in names:
                group = [by_id[e.id] for e in cat.resolve_alias(name) if e.id in by_id]
                if group:
                    groups.append(group)
            return groups

        preferred = from_aliases(request.prefer)
        routed = from_aliases(route.aliases)
        others = [[c for c in candidates]] if route.allow_others else []

        def sort_key(c: Candidate) -> tuple[float, float]:
            if optimize == "speed":
                return (speed_key(c.endpoint), price_key(c.endpoint))
            return (price_key(c.endpoint), speed_key(c.endpoint))

        seen: set[str] = set()
        ordered: list[Candidate] = []

        def take(group: list[Candidate]) -> None:
            for c in group:
                if c.endpoint.id not in seen:
                    seen.add(c.endpoint.id)
                    ordered.append(c)

        # Preferred aliases always come first, in the order given; within an alias
        # that names a family, the endpoints are ordered like everything else.
        for group in preferred:
            take(sorted(group, key=sort_key))
        rest: list[Candidate] = []
        rest_seen: set[str] = set(seen)
        # Under quality the route's own order stands; an alias naming a family has no
        # order of its own, so its endpoints are ordered by the other two. Endpoints
        # outside the route keep config order.
        for group in [sorted(g, key=sort_key) for g in routed] + others:
            ranked_group = group
            for c in ranked_group:
                if c.endpoint.id not in rest_seen:
                    rest_seen.add(c.endpoint.id)
                    rest.append(c)
        if optimize in ("cost", "speed"):
            position = {c.endpoint.id: i for i, c in enumerate(rest)}
            rest.sort(key=lambda c: (*sort_key(c), position[c.endpoint.id]))
        take(rest)

        sticky = self.affinity(request.affinity_key)
        if sticky:
            for i, c in enumerate(ordered):
                if c.endpoint.id == sticky:
                    ordered.insert(0, ordered.pop(i))
                    break
        return ordered

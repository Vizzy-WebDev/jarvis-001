"""The capability registry — resolution and declaration, and nothing else.

Deliberately split from authorization. Composition (the three-source merge and the
model-facing declaration shaper) and authorization (permissions, confirmation) are
different concerns that must not share a file; §7's requirement that permissions be
"enforced independently of model behavior" is much easier to hold when the thing
enforcing them is not also the thing deciding what the model gets to see.

So this file answers exactly two questions: what capabilities exist, and what
does the model get told about them. Whether a given call may proceed is the
policy layer's job.
"""

from __future__ import annotations

import threading
from typing import Any, Iterable

from .spec import CapabilityKind, CapabilitySpec, Risk


class DuplicateCapability(RuntimeError):
    """Two capabilities claiming the same name.

    Raised rather than resolved by precedence: a name collision must never be settled
    silently, since a built-in shadowing a folder Skill of the same name would
    disappear without a word.
    """


class CapabilityRegistry:
    def __init__(self) -> None:
        self._by_name: dict[str, CapabilitySpec] = {}
        self._lock = threading.RLock()

    def register(self, spec: CapabilitySpec) -> CapabilitySpec:
        with self._lock:
            existing = self._by_name.get(spec.name)
            if existing is not None and existing.id != spec.id:
                raise DuplicateCapability(
                    f"{spec.name!r} is already registered by {existing.id!r} "
                    f"({existing.kind.value}); {spec.id!r} ({spec.kind.value}) cannot claim it"
                )
            self._by_name[spec.name] = spec
            return spec

    def unregister(self, name: str) -> None:
        with self._lock:
            self._by_name.pop(name, None)

    def get(self, name: str) -> CapabilitySpec | None:
        return self._by_name.get(name)

    def require(self, name: str) -> CapabilitySpec:
        spec = self.get(name)
        if spec is None:
            raise KeyError(f"No such capability: {name!r}")
        return spec

    def has(self, name: str) -> bool:
        return name in self._by_name

    def list(
        self,
        *,
        kind: CapabilityKind | None = None,
        max_risk: Risk | None = None,
        with_tags: Iterable[str] | None = None,
        without_tags: Iterable[str] | None = None,
    ) -> list[CapabilitySpec]:
        """One filtered enumeration, replacing three fixed-shape ones.

        One filter that returns whole specs, so every consumer sees every field
        (including risk) and none has to stitch a subset back together in a route.
        """
        order = {Risk.LOW: 0, Risk.MEDIUM: 1, Risk.HIGH: 2}
        want = set(with_tags or ())
        reject = set(without_tags or ())

        out = []
        for spec in self._by_name.values():
            if kind is not None and spec.kind is not kind:
                continue
            if max_risk is not None and order[spec.risk] > order[max_risk]:
                continue
            if want and not want.issubset(spec.tags):
                continue
            if reject and reject & spec.tags:
                continue
            out.append(spec)
        return sorted(out, key=lambda s: s.name)

    def declarations(self, specs: Iterable[CapabilitySpec] | None = None) -> list[dict[str, Any]]:
        """What a model is told: name, description, input schema. Nothing else.

        Handlers, risk, timeouts and tags are ours, not the model's. Risk in
        particular stays server-side deliberately — §7 requires the permission
        decision to be independent of model behaviour, and a model that can see
        the risk label is a model that can argue with it.
        """
        chosen = list(specs) if specs is not None else self.list()
        return [
            {"name": s.name, "description": s.description, "parameters": s.input_schema}
            for s in chosen
        ]

    def reset_for_tests(self) -> None:
        with self._lock:
            self._by_name.clear()


registry = CapabilityRegistry()

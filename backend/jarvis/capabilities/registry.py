"""The capability registry — resolution and declaration, and nothing else.

Deliberately split from authorization. In the Node implementation one file owns
the three-source merge (good), a model-facing declaration shaper with a
visibility policy baked in, JSON-Schema mutation, a hand-rolled search engine
with its own stemmer, three separate enumerations for three consumers, AND a
stateful confirm-token service with its own TTL map. Composition and
authorization are different concerns that happened to share a file; §7's
requirement that permissions be "enforced independently of model behavior" is
much easier to hold when the thing enforcing them is not also the thing
deciding what the model gets to see.

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

    Raised rather than resolved by precedence. The Node version silently lets a
    built-in shadow a folder Skill of the same name, with a reserved-name check
    consulted only at Skill-creation time — so a name collision introduced any
    other way disappears without a word.
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

        The Node version has `listCapabilities`, `listStepCandidates` and
        `hasCapability`, each projecting a different subset of fields — and
        neither of the first two carries `confirm`, which is why deciding whether
        a Skill's pipeline step needs confirmation had to be stitched together
        inside the HTTP route file. A single filter that returns whole specs
        cannot develop that problem.
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

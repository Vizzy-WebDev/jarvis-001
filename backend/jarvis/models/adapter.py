"""The one shape every provider module has — written down, and checked.

A provider module is a plain module, not a class: `providers/openai_chat.py` and the
rest each define these four functions and a `FORMAT`, and nothing else is shared
between them (no base class, no registry, no gateway). This Protocol states that shape
so it can be asserted (`tests/conformance/`) rather than remembered.

Everything a provider says that only makes sense in its own format — its status codes,
its error bodies, its field names — stays inside its module. What comes out is only
ever these neutral types.
"""

from __future__ import annotations

from typing import Any, Iterator, Mapping, Protocol, runtime_checkable

from .errors import ProviderError
from .request import ChatRequest
from .types import CheckResult, Discovered, ProviderEvent, RawError, Target


@runtime_checkable
class ProviderAdapter(Protocol):
    FORMAT: str

    def check(self, target: Target) -> CheckResult:
        """Is the connection good? The only question whose answer sets its status."""

    def discover(self, target: Target) -> list[Discovered]:
        """What models it offers. May raise `Unsupported`."""

    def stream(self, target: Target, request: ChatRequest, *,
               facts: Mapping[str, Any] | None = None) -> Iterator[ProviderEvent]:
        """Text as it arrives, then exactly one `Finished`. `facts` is what the provider
        reported about this model (an output ceiling, the reasoning levels it takes)."""

    def normalize_error(self, raw: RawError) -> ProviderError:
        """This provider's own failure — status, headers, body — as the neutral
        `ProviderError`, with its scope decided HERE from what the provider said."""

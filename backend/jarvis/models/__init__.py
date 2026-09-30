"""Jarvis's model layer. Callers ask for what they need — a task class, content,
the output they want, hard requirements, how sensitive the data is, what to
optimize — and the layer picks the model. Callers never name a model or a
provider; when they must steer, they use aliases defined in config.

The whole public interface:

    generate(request)            -> Response
    stream(request)              -> Iterator[Event]   (ends with Done or ErrorEvent)
    embed(space, inputs)         -> Embeddings
    explain_route(request)       -> Explanation       (no model is called)
    list_endpoints()             -> list[Endpoint]
    refresh_catalog(connection?) -> {connection: status}

See README.md for the concepts and DECISIONS.md for why it is shaped this way.
This package is imported piece by piece by the boundary (`client.py` drags the
turn loop in; `oneshot.py` must not), so nothing is imported here eagerly.
"""

from __future__ import annotations

from typing import Any, Iterator


def generate(request: Any) -> Any:
    from .execute import generate as _generate

    return _generate(request)


def stream(request: Any) -> Iterator[Any]:
    from .execute import stream as _stream

    return _stream(request)


def explain_route(request: Any) -> Any:
    from .engine import explain

    return explain(request)


def list_endpoints() -> list[Any]:
    from .engine import catalog

    return list(catalog().endpoints.values())


def refresh_catalog(connection: str | None = None) -> dict[str, Any]:
    from .discovery import refresh

    return refresh(connection)


def embed(space: str, inputs: list[str]) -> Any:
    from .embed import embed as _embed

    return _embed(space, inputs)

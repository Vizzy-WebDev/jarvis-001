"""Resolve: which endpoints could serve this request at all — and, for every one
that can't, exactly why.

Nothing here ranks and nothing here calls a model. `explain_route` and the real
call both start from this same function, which is what keeps them in agreement.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any, Iterable

from .. import config as secrets
from . import capabilities as caps_mod
from . import drivers, policy, state
from .catalog import Catalog, Connection, Endpoint
from .config import Config
from .prepared import OutputMode, Unexpressible
from .types import ImagePart, Message, Rejection, Request, Sealed, Tool, ToolCall, ToolResult


@dataclass(frozen=True)
class Candidate:
    endpoint: Endpoint
    connection: Connection
    #: How the requested output will be produced here (see prepared.PreparedOutput).
    output_mode: OutputMode = "text"
    #: Tool and output schemas, already translated into what this endpoint accepts.
    tools: tuple[Tool, ...] = ()
    output_schema: Any = None

    @property
    def emulated(self) -> bool:
        return self.output_mode in ("json_mode", "instructed")


# --- what a request needs --------------------------------------------------------------------

def needed_capabilities(request: Request) -> set[str]:
    needed = {"text_in", *request.requirements.capabilities}
    if request.tools:
        needed.add("tools")
    if any(isinstance(p, ImagePart) for i in request.items if isinstance(i, Message) for p in i.parts):
        needed.add("image_in")
    return needed


def _chars(value: Any) -> int:
    if isinstance(value, str):
        return len(value)
    try:
        return len(json.dumps(value, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return len(str(value))


def estimate_tokens(request: Request, image_tokens: int) -> int:
    """Characters ÷ 3, plus 10% — deliberately generous — and a fixed amount per
    picture, which characters can't measure."""
    chars = sum(len(s.label) + len(s.text) for s in request.instructions)
    chars += sum(len(t.name) + len(t.description) + _chars(t.parameters) for t in request.tools)
    images = 0
    for item in request.items:
        if isinstance(item, Message):
            for part in item.parts:
                if isinstance(part, ImagePart):
                    images += 1
                else:
                    chars += len(part.text)
        elif isinstance(item, ToolCall):
            chars += len(item.name) + _chars(item.raw_arguments if item.arguments is None else dict(item.arguments))
        elif isinstance(item, ToolResult):
            chars += _chars(item.content)
        elif isinstance(item, Sealed):
            chars += _chars(item.payload)
    if request.output.schema is not None:
        chars += _chars(dict(request.output.schema))
    return math.ceil(chars / 3 * 1.1) + images * image_tokens


# --- the filter ------------------------------------------------------------------------------

#: What each capability a request can need means, in the words a rejection is said in.
_CAN = {"text_in": "answer in text", "image_in": "look at pictures", "pdf_in": "read PDFs", "tools": "use tools",
        "parallel_tools": "use several tools at once", "embeddings": "make embeddings",
        "reasoning_control": "take a thinking level", "prompt_caching": "cache prompts",
        "streaming": "stream its reply", "json_mode": "answer in JSON",
        "structured_output_strict": "enforce an output format"}


def _health(endpoint: Endpoint, connection: Connection) -> Rejection | None:
    down = state.connection_down(connection.name)
    if down:
        return Rejection(endpoint.id, "connection_unreachable",
                         f"{connection.label or connection.name} didn't answer when Jarvis last checked ({down}).")
    refused = state.connection_refused(connection.name)
    if refused:
        return Rejection(endpoint.id, "connection_refused",
                         f"{connection.label or connection.name} refused Jarvis's key or account last time, so it's "
                         "resting. Check it on the Model Settings screen.")
    until = state.rate_limited_until(connection.name)
    if until:
        return Rejection(endpoint.id, "rate_limited",
                         f"{connection.label or connection.name} asked Jarvis to slow down; it's resting briefly.")
    until = state.resting_until(endpoint.id)
    if until:
        return Rejection(endpoint.id, "resting", "It failed several times in a row, so it's resting for a while.")
    if connection.secret_ref and not secrets.get_secret(connection.secret_ref):
        return Rejection(endpoint.id, "no_key", f"{connection.label or connection.name} has no key saved.")
    return None


def _translate(connection: Connection, cfg: Config, schema: Any) -> Any:
    driver = drivers.get(connection.driver)
    return driver.translate_schema(schema, dict(cfg.quirks_of(connection).wire))


def check(request: Request, endpoint: Endpoint, cfg: Config, cat: Catalog, *,
          estimate: int, spent: float, pinned: set[str] | None) -> Candidate | Rejection:
    connection = cat.connections[endpoint.connection]
    if pinned is not None and endpoint.id not in pinned:
        return Rejection(endpoint.id, "not_pinned", f"This request is pinned to “{request.requirements.pin}”.")
    unhealthy = _health(endpoint, connection)
    if unhealthy:
        return unhealthy
    if not policy.trust_allows(cfg, request.data_class, connection.trust):
        allowed = ", ".join(sorted(policy.allowed_trust(cfg, request.data_class) or ()))
        return Rejection(endpoint.id, "trust_not_allowed",
                         f"Your privacy settings only allow {request.data_class} data to go to {allowed} "
                         f"connections, and this one is {connection.trust}.")

    caps = endpoint.capabilities
    for name in sorted(needed_capabilities(request)):
        if not caps_mod.has(caps, name):
            return Rejection(endpoint.id, "missing_capability", f"It isn't known to {_CAN.get(name, f'support {name}')}.")

    ceiling = caps_mod.limit(caps, "max_context_tokens")
    wanted = request.requirements.min_context
    if wanted is not None and (ceiling is None or ceiling < wanted):
        return Rejection(endpoint.id, "context_too_small",
                         f"The request needs at least {wanted} tokens of context; "
                         f"this has {ceiling if ceiling is not None else 'an unknown amount'}.")
    if ceiling is not None and estimate > ceiling:
        return Rejection(endpoint.id, "context_too_small",
                         f"The request is about {estimate} tokens; this takes {ceiling}.")

    if policy.budget_spent(cfg, spent) and not policy.usable_after_budget(endpoint):
        return Rejection(endpoint.id, "budget_spent",
                         "This month's model budget is spent, and only endpoints known to be free stay in use.")
    if request.requirements.max_cost is not None:
        if endpoint.pricing is None:
            return Rejection(endpoint.id, "price_unknown", "The request has a cost limit and this price isn't known.")
        out_tokens = request.hints.max_output_tokens or caps_mod.limit(caps, "max_output_tokens") or 1024
        cost = endpoint.pricing.cost(input_tokens=estimate, output_tokens=out_tokens) or 0.0
        if cost > request.requirements.max_cost:
            return Rejection(endpoint.id, "over_max_cost",
                             f"It could cost about ${cost:.4f}, over the limit of ${request.requirements.max_cost}.")

    try:
        tools = tuple(Tool(t.name, t.description,
                           _translate(connection, cfg, dict(t.parameters) if isinstance(t.parameters, dict)
                                      else t.parameters))
                      for t in request.tools)
    except Unexpressible as err:
        return Rejection(endpoint.id, "schema_not_expressible", f"A tool's schema can't be expressed here: {err}")

    mode: OutputMode = "text"
    schema = None
    if request.output.kind == "json":
        required = request.output.enforcement == "required"
        if caps_mod.has(caps, "structured_output_strict"):
            try:
                schema = _translate(connection, cfg, dict(request.output.schema or {}))
                mode = "strict"
            except Unexpressible as err:
                if required:
                    return Rejection(endpoint.id, "schema_not_expressible",
                                     f"The output schema can't be expressed here: {err}")
        elif required:
            return Rejection(endpoint.id, "missing_capability",
                             "The output must match a schema exactly, and this can't enforce one "
                             "(no structured_output_strict).")
        if mode != "strict":
            mode = "json_mode" if caps_mod.has(caps, "json_mode") else "instructed"
            schema = dict(request.output.schema or {})
    return Candidate(endpoint=endpoint, connection=connection, output_mode=mode, tools=tools, output_schema=schema)


def resolve(request: Request, cfg: Config, cat: Catalog,
            endpoints: Iterable[Endpoint] | None = None) -> tuple[list[Candidate], list[Rejection]]:
    """Every endpoint either passes (a Candidate) or is rejected with its reason."""
    pinned: set[str] | None = None
    if request.requirements.pin is not None:
        pinned = {e.id for e in cat.resolve_alias(request.requirements.pin)}
    estimate = estimate_tokens(request, cfg.settings.image_tokens)
    spent = state.month_spend()
    eligible: list[Candidate] = []
    rejected: list[Rejection] = []
    for endpoint in endpoints if endpoints is not None else cat.endpoints.values():
        outcome = check(request, endpoint, cfg, cat, estimate=estimate, spent=spent, pinned=pinned)
        (eligible if isinstance(outcome, Candidate) else rejected).append(outcome)  # type: ignore[arg-type]
    return eligible, rejected

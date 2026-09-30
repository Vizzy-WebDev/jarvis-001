"""Embeddings, by named space.

A space (config `embedding_spaces`) is one model at one version, with a fixed
dimension: a primary endpoint and, optionally, backups that config declares serve
the identical model and version. Vectors from different models are not
comparable, so a space never substitutes anything else: if the primary and its
declared backups are all unavailable, the call fails. Data policy applies exactly
as it does to generation.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import config, drivers, policy, state
from .catalog import split_endpoint_id
from .discovery import conn_info
from .errors import InvalidRequest, ModelError, NoEligibleEndpoint, Unavailable


@dataclass(frozen=True)
class Embeddings:
    vectors: tuple[tuple[float, ...], ...]
    space: str
    model_version: str
    endpoint_id: str


def embed(space_name: str, inputs: list[str], *, data_class: str) -> Embeddings:
    cfg = config.current()
    space = cfg.embedding_spaces.get(space_name)
    if space is None:
        raise InvalidRequest(f"There's no embedding space called “{space_name}”.")
    version = space.model_version or split_endpoint_id(space.primary)[1]
    last: ModelError | None = None
    skipped: list[str] = []
    for endpoint_id in (space.primary, *space.backups):
        conn_name, model_id = split_endpoint_id(endpoint_id)
        conn = cfg.connections[conn_name]
        if not policy.trust_allows(cfg, data_class, conn.trust):
            skipped.append(f"{endpoint_id}: your privacy settings don't allow {data_class} data there")
            continue
        if state.connection_down(conn_name) or state.resting_until(endpoint_id) or state.rate_limited_until(conn_name):
            skipped.append(f"{endpoint_id}: resting after recent failures")
            continue
        driver = drivers.get(conn.driver)
        if not hasattr(driver, "embed"):
            raise InvalidRequest(f"{conn.label or conn_name} ({conn.driver}) has no embeddings call.")
        try:
            vectors = driver.embed(conn_info(conn, cfg), model_id, list(inputs))
        except ModelError as err:
            err.at(endpoint_id)
            if not err.retryable:
                raise
            if err.type in ("unavailable", "timeout"):
                state.record_failure(endpoint_id, str(err), threshold=cfg.settings.breaker_threshold,
                                     base_s=cfg.settings.breaker_base_s, max_s=cfg.settings.breaker_max_s)
            last = err
            continue
        wrong = [len(v) for v in vectors if len(v) != space.dimension]
        if wrong:
            raise InvalidRequest(f"{endpoint_id} returned {wrong[0]}-number vectors, but the space "
                                 f"“{space_name}” is {space.dimension}. It isn't the model this space was made "
                                 "with, so nothing was used.")
        state.record_success(endpoint_id)
        return Embeddings(tuple(tuple(v) for v in vectors), space_name, version, endpoint_id)
    if last is not None:
        raise Unavailable(f"The embedding model for “{space_name}” isn't answering, and nothing else may stand in "
                          f"for it. {last}")
    raise NoEligibleEndpoint(f"The embedding model for “{space_name}” can't be used right now: "
                             + "; ".join(skipped) + ".")

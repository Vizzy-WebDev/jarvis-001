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

from .. import config as secrets
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


# --- finding a model to embed with -------------------------------------------------------------

#: Most private first: a model on this computer never sends anything anywhere.
_TRUST_ORDER = {"local": 0, "zero_retention": 1, "standard": 2}


def candidates(data_class: str, cfg: config.Config | None = None) -> list[tuple[str, str]]:
    """(endpoint id, why it was not usable | "") for every endpoint that says it embeds, best
    first. Usable ones come first, ordered by how private the connection is, then by name so the
    choice never wobbles between runs."""
    from . import capabilities, engine

    cfg = cfg or config.current()
    cat = engine.catalog(cfg)
    found: list[tuple[int, str, str]] = []
    for endpoint in cat.endpoints.values():
        if not capabilities.has(endpoint.capabilities, "embeddings"):
            continue
        conn = cfg.connections.get(endpoint.connection)
        if conn is None:
            continue
        why = ""
        if not policy.trust_allows(cfg, data_class, conn.trust):
            why = f"your privacy settings don't allow {data_class} data on {conn.label or conn.name}"
        elif conn.secret_ref and not secrets.get_secret(conn.secret_ref):
            why = f"{conn.label or conn.name} has no key saved"
        found.append((1 if why else 0, _TRUST_ORDER.get(conn.trust, 3), endpoint.id, why))
    found.sort(key=lambda row: (row[0], row[1], row[2]))
    return [(row[2], row[3]) for row in found]


def ensure_space(name: str, *, data_class: str) -> config.EmbeddingSpace | None:
    """The named space, made on first use from an embedding model the person already has.

    Found or refused, never approximated (the same rule as `settings.ensure_pin`): an existing
    space is returned as it is — even when its model is gone, in which case `embed()` fails and
    the caller falls back — and a space is only ever MADE from an endpoint that says it embeds,
    on a connection the data policy allows. The dimension is learned from one tiny real call and
    written down once, so every later vector is checked against it. None when there is nothing
    to make one from (or the probe failed): the caller simply does without.
    """
    cfg = config.current()
    existing = cfg.embedding_spaces.get(name)
    if existing is not None:
        return existing
    for endpoint_id, why in candidates(data_class, cfg):
        if why:
            continue
        conn_name, model_id = split_endpoint_id(endpoint_id)
        conn = cfg.connections[conn_name]
        driver = drivers.get(conn.driver)
        if not hasattr(driver, "embed"):
            continue
        try:
            probe = driver.embed(conn_info(conn, cfg), model_id, ["hello"])
        except ModelError:
            continue
        if not probe or not probe[0]:
            continue
        return config.set_embedding_space(name, primary=endpoint_id, dimension=len(probe[0]),
                                          model_version=model_id).embedding_spaces.get(name)
    return None

"""Finding a memory by what it MEANS, not only by the words it shares with what was just said.

"What should I cook tonight?" shares no word with "Vegetarian — never suggest meat", and keyword
overlap (`orchestrator/context.select_memories`) misses it. A memory's meaning, as numbers from an
embedding model, finds it. This module owns everything about that, and nothing here is ever required:
**no embedding model, a model that isn't answering, a privacy setting that forbids it, or a message too
short to mean anything all fall back to the keyword path, silently and without delaying the turn**
beyond a short, stated timeout.

How it stays honest:

- **A vector is valid only while everything that made it still holds**: the memory's current text
  (`text_hash`), the embedding space, the model's version and the dimension. An edited memory or a
  switched model is simply "not indexed yet" — vectors from different models are never compared (the
  rule `models/embeddings.py` exists to enforce). A memory without a valid vector is ranked by keyword
  alone, never dropped.
- **Memories are embedded in the background, in one batch, not per turn** (`sync`). It is
  self-healing: a turn that finds memories unindexed starts it, so edits from the Memory screen, the
  tools and the review queue all need no write-side plumbing. Single-flight, with a cool-down after a
  failure so a provider that is down is not hammered.
- **The one per-turn cost** is embedding the person's message — only when memory is large enough to
  need a search at all and the message has enough words to mean something. It has a hard timeout, a small
  cache, and a cool-down after any failure, so a dead provider costs one timeout, not one per turn.
- **Search is plain numpy cosine over the stored float32 vectors.** Memory is a small curated list, so
  brute force is instant, and it needs no SQLite extension (which not every Python build can load).
- **Memory text is `personal` data** and every call says so; a `policies.data_classes` restriction is
  honoured exactly as it is for chat.

Imports the model layer lazily, inside functions, so loading this module (the turn loop's context
assembly does) never pulls the model layer in.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from collections import OrderedDict
from typing import Any

from . import store

logger = logging.getLogger(__name__)

SPACE = "memory"
DATA_CLASS = "personal"

#: A message with fewer words than this is "yes", "ok", "go on": there is nothing to match, and
#: embedding it would spend a call to learn nothing.
MIN_QUERY_WORDS = 3

#: How long a turn waits for the message's own embedding before carrying on without it.
QUERY_TIMEOUT_S = 1.5
#: How long after a failure or a timeout that no further call is attempted — a dead provider
#: costs one wait, not one per turn.
COOLDOWN_S = 120.0
#: How many recent messages' vectors are kept (retries, regenerations, repeated questions).
CACHE_ENTRIES = 64
#: Memories embedded per call, so one batch never exceeds what a provider accepts.
BATCH = 64

_lock = threading.RLock()
_cache: "OrderedDict[tuple[str, str, str], Any]" = OrderedDict()
_cool_until = 0.0
_cool_reason = ""
_syncing = False
_no_space_until = 0.0


def _now() -> float:
    return time.monotonic()


def text_of(memory: dict[str, Any]) -> str:
    """What is embedded for a memory: its category and its text (the category is meaning too)."""
    return f"{memory.get('category') or ''}: {memory.get('text') or ''}".strip()


def text_hash(memory: dict[str, Any]) -> str:
    return hashlib.sha256(text_of(memory).encode("utf-8")).hexdigest()[:20]


def reset_for_tests() -> None:
    global _cool_until, _cool_reason, _syncing, _no_space_until
    with _lock:
        _cache.clear()
        _cool_until, _cool_reason, _syncing, _no_space_until = 0.0, "", False, 0.0


# --- the space ---------------------------------------------------------------------------------

def _space(*, make: bool) -> Any | None:
    """The configured memory space; with `make`, one is made from an embedding model the person
    already has (found or refused — `models.embeddings.ensure_space`). A refusal is remembered for
    a while so the first turns do not each go looking again."""
    global _no_space_until
    from ..models import config as model_config

    try:
        existing = model_config.current().embedding_spaces.get(SPACE)
    except model_config.ConfigError:
        return None
    if existing is not None or not make:
        return existing
    with _lock:
        if _now() < _no_space_until:
            return None
    from ..models import embeddings

    try:
        made = embeddings.ensure_space(SPACE, data_class=DATA_CLASS)
    except Exception:  # noqa: BLE001 — not finding a model is "off", never an error
        logger.exception("could not look for an embedding model")
        made = None
    if made is None:
        with _lock:
            _no_space_until = _now() + COOLDOWN_S
    return made


def _version_of(space: Any) -> str:
    from ..models.catalog import split_endpoint_id

    return space.model_version or split_endpoint_id(space.primary)[1]


def _why_no_space() -> str:
    """Why there is no space: nothing to make one from, or something was found and did not answer."""
    from ..models import embeddings

    try:
        if any(not why for _, why in embeddings.candidates(DATA_CLASS)):
            return "the embedding model isn't answering"
    except Exception:  # noqa: BLE001
        logger.exception("could not look for an embedding model")
    return "no embedding model is set up"


# --- calling the model, with a deadline --------------------------------------------------------

class _Late(Exception):
    pass


def _within(seconds: float, fn: Any) -> Any:
    """Run `fn` on a daemon thread and wait at most `seconds` for it. A slow call is left to finish
    (it cannot be stopped) but is never waited on."""
    box: dict[str, Any] = {}

    def go() -> None:
        try:
            box["value"] = fn()
        except BaseException as err:  # noqa: BLE001 — handed back to the caller
            box["error"] = err

    thread = threading.Thread(target=go, name="memory-embedding", daemon=True)
    thread.start()
    thread.join(seconds)
    if thread.is_alive():
        raise _Late()
    if "error" in box:
        raise box["error"]
    return box["value"]


def _failed(reason: str) -> None:
    global _cool_until, _cool_reason
    with _lock:
        _cool_until, _cool_reason = _now() + COOLDOWN_S, reason


def _cooling() -> str | None:
    with _lock:
        return _cool_reason if _now() < _cool_until else None


def _reason_for(err: BaseException) -> str:
    from ..models import errors

    if isinstance(err, _Late):
        return "the embedding model was too slow"
    if isinstance(err, errors.NoEligibleEndpoint):
        return str(err)
    if isinstance(err, errors.ModelError):
        return "the embedding model isn't answering"
    return "the embedding model failed"


def _embed(space: Any, inputs: list[str]) -> list[Any]:
    from ..models import embed
    import numpy as np

    result = embed(SPACE, inputs, data_class=DATA_CLASS)
    return [np.asarray(v, dtype=np.float32) for v in result.vectors]


# --- search ------------------------------------------------------------------------------------

def similarities(text: str, memories: list[dict[str, Any]]) -> tuple[dict[str, float] | None, str]:
    """(memory id -> cosine similarity to `text`, why not).

    A dict (possibly covering only the memories that already have a valid vector) when a vector
    search was done; `None` plus a plain reason when it was not, and the caller uses keywords. Never
    raises. May start the background indexing of memories that have no valid vector yet.
    """
    import numpy as np

    if len(text.split()) < MIN_QUERY_WORDS:
        return None, "the message is too short to match by meaning"
    cooling = _cooling()
    if cooling:
        return None, cooling
    try:
        space = _space(make=True)
    except Exception:  # noqa: BLE001
        logger.exception("could not read the embedding space")
        return None, "no embedding model is set up"
    if space is None:
        return None, _why_no_space()

    version = _version_of(space)
    have = store.vector_rows(SPACE, version, space.dimension)
    usable: dict[str, Any] = {}
    missing = 0
    for memory in memories:
        row = have.get(memory["id"])
        if row is not None and row[0] == text_hash(memory) and len(row[1]) == space.dimension * 4:
            usable[memory["id"]] = np.frombuffer(row[1], dtype=np.float32)
        else:
            missing += 1
    if missing:
        sync_in_background()
    if not usable:
        return None, "still indexing your memories"

    key = (SPACE, version, text)
    with _lock:
        query = _cache.get(key)
        if query is not None:
            _cache.move_to_end(key)
    if query is None:
        try:
            query = _within(QUERY_TIMEOUT_S, lambda: _embed(space, [text])[0])
        except Exception as err:  # noqa: BLE001 — every failure is just "use keywords"
            reason = _reason_for(err)
            logger.info("[memory] searching by meaning skipped: %s", reason)
            _failed(reason)
            return None, reason
        with _lock:
            _cache[key] = query
            while len(_cache) > CACHE_ENTRIES:
                _cache.popitem(last=False)

    norm = float(np.linalg.norm(query))
    if norm == 0.0:
        return None, "the message had no meaning to match"
    out: dict[str, float] = {}
    for memory_id, vector in usable.items():
        denominator = norm * float(np.linalg.norm(vector))
        out[memory_id] = float(np.dot(query, vector) / denominator) if denominator else 0.0
    return out, ""


# --- indexing ----------------------------------------------------------------------------------

def sync() -> int:
    """Embed every memory that has no valid vector, in batches. Returns how many were saved. Safe to
    call at any time and from any thread; never raises."""
    import numpy as np

    global _syncing
    with _lock:
        if _syncing or _cooling():
            return 0
        _syncing = True
    saved = 0
    try:
        space = _space(make=True)
        if space is None:
            return 0
        version = _version_of(space)
        have = store.vector_rows(SPACE, version, space.dimension)
        todo = [m for m in store.list_memories(include_expired=False)
                if (have.get(m["id"]) or ("",))[0] != text_hash(m)]
        for start in range(0, len(todo), BATCH):
            batch = todo[start:start + BATCH]
            try:
                vectors = _embed(space, [text_of(m) for m in batch])
            except Exception as err:  # noqa: BLE001
                reason = _reason_for(err)
                logger.info("[memory] indexing memories paused: %s", reason)
                _failed(reason)
                break
            saved += store.save_vectors(
                (m["id"], SPACE, version, space.dimension, text_hash(m),
                 np.asarray(v, dtype=np.float32).tobytes())
                for m, v in zip(batch, vectors))
    except Exception:  # noqa: BLE001 — indexing is a convenience; it must never take anything down
        logger.exception("could not index memories")
    finally:
        with _lock:
            _syncing = False
    return saved


def sync_in_background() -> None:
    from ..background import run_in_background

    with _lock:
        if _syncing or _cooling():
            return
    run_in_background(sync, name="memory-index")


# --- what the person is told -------------------------------------------------------------------

def status(small_limit: int | None = None) -> dict[str, Any]:
    """Whether searching by meaning is on, for the Memory screen. Reads config and the database only:
    it never calls a model, so the screen opening can never be slowed by one. `small_limit` is the
    size up to which every memory goes in unsearched (`orchestrator/context.SMALL_MEMORY`, passed in
    by the caller so this module never imports the turn loop's side)."""
    from ..models import config as model_config
    from ..models import embeddings

    memories = store.list_memories()
    total = len(memories)
    try:
        cfg = model_config.current()
    except model_config.ConfigError:
        return {"state": "off", "model": None, "indexed": 0, "total": total,
                "reason": "Jarvis couldn't read its model settings."}
    space = cfg.embedding_spaces.get(SPACE)
    if space is None:
        usable = [e for e, why in embeddings.candidates(DATA_CLASS, cfg) if not why]
        if usable:
            if small_limit is not None and total <= small_limit:
                reason = (f"Not needed yet: with {total} of up to {small_limit} memories, Jarvis "
                          "reads them all. It switches on by itself after that.")
            else:
                reason = "It switches on by itself with your next message."
            return {"state": "ready", "model": usable[0], "indexed": 0, "total": total,
                    "reason": reason}
        blocked = [why for _, why in embeddings.candidates(DATA_CLASS, cfg) if why]
        return {"state": "off", "model": None, "indexed": 0, "total": total,
                "reason": (blocked[0][0].upper() + blocked[0][1:] + "." if blocked else
                           "Add an embedding model in Model Settings and it switches on by itself.")}
    version = _version_of(space)
    have = store.vector_rows(SPACE, version, space.dimension)
    indexed = sum(1 for m in memories
                  if (have.get(m["id"]) or ("",))[0] == text_hash(m))
    cooling = _cooling()
    state = "on" if indexed >= total else "building"
    return {"state": state, "model": space.primary, "indexed": indexed, "total": total,
            "reason": cooling or ("" if state == "on" else "Indexing your memories in the background.")}

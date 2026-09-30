"""One module per wire protocol. Each exposes the same few names and nothing more:

    NAME                    the driver name config refers to
    DEFAULT_CAPABILITIES    what this protocol generally offers (declared, lowest rank)
    stream(conn, prepared)  -> Iterator[DriverEvent]      raises errors.ModelError
    discover(conn)          -> list[Discovered]           raises errors.ModelError
    translate_schema(schema, quirks) -> schema            raises prepared.Unexpressible
    embed(conn, model_id, inputs) -> list[list[float]]    (only where the protocol has one)

A driver translates; it never routes, retries, falls back or applies policy. This
dictionary is the whole of how config's `driver:` finds one.
"""

from __future__ import annotations

from types import ModuleType

from . import fake

DRIVERS: dict[str, ModuleType] = {m.NAME: m for m in (fake,)}


def get(name: str) -> ModuleType:
    try:
        return DRIVERS[name]
    except KeyError:
        raise ValueError(f"There's no driver called “{name}”. "
                         f"Known drivers: {', '.join(sorted(DRIVERS))}.") from None

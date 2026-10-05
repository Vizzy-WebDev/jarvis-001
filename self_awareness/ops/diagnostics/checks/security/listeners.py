"""Is anything listening on a network interface this app never asked for?

Jarvis binds 127.0.0.1 and nothing else — that is a property of the app, not of
the user's firewall. A listener owned by THIS process on any other address means
that property has stopped holding, which is exactly the kind of thing nobody
notices until it matters.

Scoped to this process's own sockets on purpose. Every other listener on the
machine belongs to software this build knows nothing about, and reporting those
would produce a warning a person cannot act on, every single time.
"""

from __future__ import annotations

import os
from typing import Any

from ...registry import Check

LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def probe() -> dict[str, Any]:
    import psutil

    try:
        connections = psutil.Process(os.getpid()).net_connections(kind="inet")
    except Exception as err:  # noqa: BLE001 — restricted environments refuse this outright
        return {"ok": True, "detail": f"Could not read this process's sockets ({err})."}

    exposed = []
    for connection in connections:
        if connection.status != psutil.CONN_LISTEN or not connection.laddr:
            continue
        address = connection.laddr.ip
        if address in LOOPBACK:
            continue
        # 0.0.0.0 / :: means every interface, which is the case this exists for.
        exposed.append(f"{address}:{connection.laddr.port}")
    if exposed:
        return {"ok": False,
                "detail": ("Jarvis is listening somewhere other than this computer's own "
                           f"loopback address: {', '.join(sorted(set(exposed)))}. It should "
                           "only ever be reachable from this machine.")}
    return {"ok": True}


CHECK = Check(id="listeners", probe=probe, kind="security")

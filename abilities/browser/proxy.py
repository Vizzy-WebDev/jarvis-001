"""The only way out for the browser: a small proxy that checks every destination.

Ported from OpenMuse's `apps/worker/src/proxy.ts` (MIT licence, CopilotKit), onto
asyncio instead of Node's `http` module.

Chromium is started with this as its proxy and with its usual "loopback needs no
proxy" exception removed, so even an address on this computer goes through here.
For each request the destination is validated (`network.validate_public_url`),
and the connection is then made to the exact address that was validated: the
browser never gets a second, different DNS answer. HTTPS is a CONNECT tunnel,
port 443 only; the bytes inside it are not read.

One request per connection on the plain-HTTP side (`Connection: close` upstream)
keeps this simple and cannot mix up two requests' bodies; HTTPS, which is nearly
everything, is unaffected.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Awaitable, Callable

from .errors import BrowserError
from .network import Resolver, validate_public_url

logger = logging.getLogger(__name__)

HEAD_LIMIT = 64 * 1024
HEAD_TIMEOUT_S = 15.0
IDLE_TIMEOUT_S = 60.0
CONNECT_TIMEOUT_S = 15.0

#: (address, port, family) -> streams. Replaceable so a test can point a "public"
#: address at a fixture server after the real destination checks have run.
Connector = Callable[[str, int, int], Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]]]

_AUTHORITY = re.compile(r"^(?:\[[0-9a-f:]+\]|[a-z0-9.-]+):443$", re.I)
_HOP_BY_HOP = {b"proxy-authorization", b"proxy-connection", b"connection", b"keep-alive"}


async def _default_connect(address: str, port: int, family: int):
    return await asyncio.open_connection(address, port)


class EgressProxy:
    def __init__(self, server: asyncio.AbstractServer, port: int) -> None:
        self._server = server
        self.url = f"http://127.0.0.1:{port}"
        self._writers: set[asyncio.StreamWriter] = set()

    def track(self, writer: asyncio.StreamWriter) -> None:
        self._writers.add(writer)

    def untrack(self, writer: asyncio.StreamWriter) -> None:
        self._writers.discard(writer)

    async def close(self) -> None:
        self._server.close()
        for writer in list(self._writers):
            writer.close()
        try:
            await asyncio.wait_for(self._server.wait_closed(), 5)
        except Exception:  # noqa: BLE001 - closing must always finish
            pass


async def start_egress_proxy(connect: Connector | None = None,
                             resolve: Resolver | None = None) -> EgressProxy:
    connector = connect or _default_connect
    holder: dict[str, EgressProxy] = {}

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        proxy = holder["proxy"]
        proxy.track(writer)
        upstream_writer: asyncio.StreamWriter | None = None
        try:
            try:
                head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), HEAD_TIMEOUT_S)
            except (asyncio.IncompleteReadError, asyncio.LimitOverrunError,
                    asyncio.TimeoutError, ConnectionError):
                return
            lines = head[:-4].split(b"\r\n")
            parts = lines[0].decode("latin-1").split(" ")
            if len(parts) != 3:
                await _respond(writer, 400, "Bad request")
                return
            method, target, _version = parts
            try:
                if method.upper() == "CONNECT":
                    upstream_writer = await _tunnel(reader, writer, target, connector, resolve,
                                                    proxy)
                else:
                    upstream_writer = await _forward(reader, writer, method, target,
                                                     lines[1:], connector, resolve, proxy)
            except BrowserError:
                await _respond(writer, 403, "Destination blocked")
            except (OSError, asyncio.TimeoutError):
                await _respond(writer, 502, "Could not reach the destination")
        except Exception:  # noqa: BLE001 - one bad connection must never stop the proxy
            logger.debug("egress proxy connection failed", exc_info=True)
        finally:
            for stream in (upstream_writer, writer):
                if stream is not None:
                    stream.close()
            proxy.untrack(writer)

    server = await asyncio.start_server(handle, "127.0.0.1", 0, limit=HEAD_LIMIT)
    port = server.sockets[0].getsockname()[1]
    holder["proxy"] = EgressProxy(server, port)
    return holder["proxy"]


async def _respond(writer: asyncio.StreamWriter, status: int, text: str) -> None:
    body = text.encode()
    try:
        writer.write(f"HTTP/1.1 {status} {text}\r\nContent-Length: {len(body)}\r\n"
                     "Connection: close\r\n\r\n".encode() + body)
        await writer.drain()
    except (ConnectionError, OSError):
        pass


async def _open(connector: Connector, address: str, port: int, family: int):
    return await asyncio.wait_for(connector(address, port, family), CONNECT_TIMEOUT_S)


async def _tunnel(reader, writer, authority: str, connector: Connector,
                  resolve: Resolver | None, proxy: EgressProxy):
    if not _AUTHORITY.match(authority):
        raise BrowserError("BLOCKED_URL", "Invalid tunnel.")
    target = await validate_public_url(f"https://{authority}", resolve)
    upstream_reader, upstream_writer = await _open(connector, target.address, 443, target.family)
    proxy.track(upstream_writer)
    writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
    await writer.drain()
    await _both_ways(reader, writer, upstream_reader, upstream_writer)
    proxy.untrack(upstream_writer)
    return upstream_writer


async def _forward(reader, writer, method: str, target: str, header_lines: list[bytes],
                   connector: Connector, resolve: Resolver | None, proxy: EgressProxy):
    destination = await validate_public_url(target, resolve)
    if destination.url.scheme != "http":
        raise BrowserError("BLOCKED_URL", "An HTTP proxy request needs an http address.")
    path = (destination.url.path or "/") + (f"?{destination.url.query}"
                                            if destination.url.query else "")
    out = [f"{method} {path} HTTP/1.1".encode("latin-1"),
           b"Host: " + destination.url.netloc.rsplit("@", 1)[-1].encode("idna")]
    for line in header_lines:
        name = line.split(b":", 1)[0].strip().lower()
        if name in _HOP_BY_HOP or name == b"host":
            continue
        out.append(line)
    out.append(b"Connection: close")
    port = destination.url.port or 80
    upstream_reader, upstream_writer = await _open(connector, destination.address, port,
                                                   destination.family)
    proxy.track(upstream_writer)
    upstream_writer.write(b"\r\n".join(out) + b"\r\n\r\n")
    await upstream_writer.drain()
    await _both_ways(reader, writer, upstream_reader, upstream_writer)
    proxy.untrack(upstream_writer)
    return upstream_writer


async def _pipe(source: asyncio.StreamReader, sink: asyncio.StreamWriter) -> None:
    try:
        while True:
            chunk = await asyncio.wait_for(source.read(65536), IDLE_TIMEOUT_S)
            if not chunk:
                break
            sink.write(chunk)
            await sink.drain()
    except (asyncio.TimeoutError, ConnectionError, OSError):
        pass


async def _both_ways(client_reader, client_writer, upstream_reader, upstream_writer) -> None:
    """Copy both directions; when either side is done, the whole exchange is."""
    up = asyncio.ensure_future(_pipe(client_reader, upstream_writer))
    down = asyncio.ensure_future(_pipe(upstream_reader, client_writer))
    try:
        await asyncio.wait({up, down}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in (up, down):
            task.cancel()
        await asyncio.gather(up, down, return_exceptions=True)

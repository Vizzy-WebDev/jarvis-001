"""Where Jarvis's browser may go - the guard, and the proxy that enforces it.

The destination checks are ported from OpenMuse's browser tests (MIT licence) and
extended: the proxy is exercised against a real socket, and so is the property the
whole design rests on - that the connection is made to the address that was
CHECKED, never to a second lookup of the name.
"""

from __future__ import annotations

import asyncio

import pytest

from jarvis.browser.errors import BrowserError
from jarvis.browser.network import is_public_ip, validate_public_url
from jarvis.browser.proxy import start_egress_proxy


@pytest.mark.parametrize("address", [
    "93.184.216.34", "8.8.8.8", "1.1.1.1", "2606:2800:220:1:248:1893:25c8:1946",
    "2a00:1450:4001:81b::200e"])
def test_public_addresses_are_allowed(address):
    assert is_public_ip(address)


@pytest.mark.parametrize("address", [
    "127.0.0.1", "127.255.255.254", "10.0.0.1", "172.16.0.1", "172.31.255.255",
    "192.168.1.1", "169.254.169.254", "100.64.0.1", "0.0.0.0", "224.0.0.1",
    "255.255.255.255", "192.0.2.1", "198.51.100.7", "203.0.113.9", "198.18.0.1",
    "::1", "::", "fe80::1", "fc00::1", "fd12:3456::1", "::ffff:127.0.0.1",
    "::ffff:7f00:1", "2001:db8::1", "2002:7f00:1::", "ff02::1", "not an address", "",
    "fe80::1%eth0"])
def test_private_reserved_and_malformed_addresses_are_refused(address):
    assert not is_public_ip(address)


async def _resolver_for(table):
    async def resolve(name):
        if name not in table:
            raise OSError("no such host")
        return table[name]
    return resolve


def _run(coro):
    return asyncio.run(coro)


@pytest.mark.parametrize("url", [
    "http://localhost/", "http://foo.localhost/", "http://printer.local/",
    "http://router.lan/", "http://nas.home/", "http://db.internal/",
    "http://127.0.0.1/", "http://[::1]/", "http://10.1.2.3/", "http://169.254.169.254/latest/",
    "http://192.168.0.1/", "https://example.com:8443/", "http://example.com:22/",
    "ftp://example.com/", "file:///etc/passwd", "javascript:alert(1)", "data:text/html,x",
    "http://user:pass@example.com/", "http://user@example.com/", "not a url", "http://",
    "https://[::ffff:127.0.0.1]/"])
def test_destinations_that_are_not_public_web_pages_are_refused(url):
    resolve = _run(_resolver_for({"example.com": [("93.184.216.34", 4)]}))
    with pytest.raises(BrowserError) as refused:
        _run(validate_public_url(url, resolve))
    assert refused.value.code in {"BLOCKED_URL", "DNS_UNAVAILABLE"}


def test_a_public_name_is_allowed_and_the_checked_address_is_returned():
    resolve = _run(_resolver_for({"example.com": [("93.184.216.34", 4)]}))
    target = _run(validate_public_url("https://example.com/path?q=1", resolve))
    assert (target.address, target.family) == ("93.184.216.34", 4)
    assert _run(validate_public_url("http://example.com:80/", resolve)).address
    assert _run(validate_public_url("https://example.com:443/", resolve)).address


def test_one_private_answer_among_public_ones_refuses_the_name():
    """DNS rebinding's plainest form: a name that is public for one answer and the
    router for the next. All answers must be public, not just the first."""
    resolve = _run(_resolver_for({"mixed.example": [("93.184.216.34", 4), ("10.0.0.5", 4)]}))
    with pytest.raises(BrowserError) as refused:
        _run(validate_public_url("http://mixed.example/", resolve))
    assert refused.value.code == "BLOCKED_URL"


def test_a_name_that_does_not_resolve_says_so_plainly():
    resolve = _run(_resolver_for({}))
    with pytest.raises(BrowserError) as refused:
        _run(validate_public_url("http://nowhere.example/", resolve))
    assert refused.value.code == "DNS_UNAVAILABLE" and refused.value.status == 502


def test_an_ipv4_literal_that_is_public_needs_no_lookup():
    async def never(_name):
        raise AssertionError("a literal address must not be looked up")
    target = _run(validate_public_url("http://93.184.216.34/", never))
    assert target.address == "93.184.216.34"


# --- the proxy, on a real socket --------------------------------------------------

async def _echo_server():
    """An upstream that answers any request with a fixed page, then closes."""
    async def handle(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        body = b"upstream says hello"
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\nConnection: close\r\n\r\n%s"
                     % (len(body), body))
        await writer.drain()
        writer.close()
    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


async def _through_proxy(proxy_url: str, request: bytes) -> bytes:
    port = int(proxy_url.rsplit(":", 1)[1])
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(request)
    await writer.drain()
    data = await asyncio.wait_for(reader.read(), 10)
    writer.close()
    return data


def test_the_proxy_connects_to_the_checked_address_and_never_looks_the_name_up_again():
    async def scenario():
        upstream, upstream_port = await _echo_server()
        lookups: list[str] = []
        connected: list[tuple[str, int]] = []

        async def resolve(name):
            lookups.append(name)
            # A hostile name: public the first time it is asked, the router afterwards.
            return [("93.184.216.34", 4)] if len(lookups) == 1 else [("127.0.0.1", 4)]

        async def connect(address, port, family):
            connected.append((address, port))
            return await asyncio.open_connection("127.0.0.1", upstream_port)

        proxy = await start_egress_proxy(connect=connect, resolve=resolve)
        try:
            reply = await _through_proxy(
                proxy.url, b"GET http://evil.example/page?a=1 HTTP/1.1\r\nHost: evil.example\r\n"
                           b"Proxy-Connection: keep-alive\r\n\r\n")
        finally:
            await proxy.close()
            upstream.close()
        return reply, lookups, connected

    reply, lookups, connected = _run(scenario())
    assert b"200 OK" in reply and b"upstream says hello" in reply
    assert lookups == ["evil.example"], "the name was looked up more than once"
    assert connected == [("93.184.216.34", 80)], "connected to something that was not checked"


def test_the_proxy_refuses_private_destinations_with_a_403():
    async def scenario():
        async def resolve(name):
            return [("10.0.0.5", 4)]

        async def connect(*_):
            raise AssertionError("must never connect to a refused destination")

        proxy = await start_egress_proxy(connect=connect, resolve=resolve)
        try:
            plain = await _through_proxy(
                proxy.url, b"GET http://router.example/ HTTP/1.1\r\nHost: router.example\r\n\r\n")
            literal = await _through_proxy(
                proxy.url, b"GET http://127.0.0.1:3000/api/health HTTP/1.1\r\nHost: x\r\n\r\n")
            tunnel = await _through_proxy(
                proxy.url, b"CONNECT router.example:443 HTTP/1.1\r\nHost: router.example:443\r\n\r\n")
            odd_port = await _through_proxy(
                proxy.url, b"CONNECT example.com:8443 HTTP/1.1\r\nHost: example.com:8443\r\n\r\n")
        finally:
            await proxy.close()
        return plain, literal, tunnel, odd_port

    for answer in _run(scenario()):
        assert b" 403 " in answer.split(b"\r\n", 1)[0], answer[:80]


def test_the_proxy_tunnels_https_to_the_checked_address_on_443_only():
    async def scenario():
        upstream, upstream_port = await _echo_server()
        connected: list[tuple[str, int]] = []

        async def resolve(name):
            return [("93.184.216.34", 4)]

        async def connect(address, port, family):
            connected.append((address, port))
            return await asyncio.open_connection("127.0.0.1", upstream_port)

        proxy = await start_egress_proxy(connect=connect, resolve=resolve)
        try:
            port = int(proxy.url.rsplit(":", 1)[1])
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(b"CONNECT secure.example:443 HTTP/1.1\r\nHost: secure.example:443\r\n\r\n")
            await writer.drain()
            opened = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
            # Inside the tunnel the proxy does not read: it just carries bytes.
            writer.write(b"GET / HTTP/1.1\r\nHost: secure.example\r\n\r\n")
            await writer.drain()
            inner = await asyncio.wait_for(reader.read(), 5)
            writer.close()
        finally:
            await proxy.close()
            upstream.close()
        return opened, inner, connected

    opened, inner, connected = _run(scenario())
    assert b"200 Connection Established" in opened
    assert b"upstream says hello" in inner
    assert connected == [("93.184.216.34", 443)]

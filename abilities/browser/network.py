"""Where the browser may go: public web addresses on ports 80 and 443, nothing else.

Ported from OpenMuse's `apps/worker/src/network.ts` (MIT licence, CopilotKit).

This is not a nicety. Jarvis's own API listens on this computer's loopback
address, and a web page — or a model that has been talked into something by a
page — must not be able to steer the browser at it, at the router, at a printer
or at anything else on the person's home network. So every destination is
resolved HERE, every answer must be a public address, and the egress proxy
(`proxy.py`) connects to exactly the address that was checked, never to a second
lookup an attacker could change in between ("DNS rebinding").

There is deliberately no switch that allows a private destination. Tests inject
a resolver and a connector instead (see `host.py`), as OpenMuse's own do.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from dataclasses import dataclass
from typing import Awaitable, Callable
from urllib.parse import SplitResult, urlsplit

from .errors import BrowserError

#: One resolved answer: (address, family) where family is 4 or 6.
Answer = tuple[str, int]
Resolver = Callable[[str], Awaitable[list[Answer]]]

DNS_TIMEOUT_S = 5.0

_PRIVATE_NAMES = re.compile(r"(^|\.)(localhost|local|internal|home|lan)$")

#: Networks that are never a browser destination. Transition, documentation,
#: private, loopback, multicast, reserved and carrier-grade-NAT ranges.
_BLOCKED_V4 = tuple(ipaddress.ip_network(n) for n in (
    "0.0.0.0/8", "10.0.0.0/8", "127.0.0.0/8", "224.0.0.0/3", "100.64.0.0/10",
    "169.254.0.0/16", "172.16.0.0/12", "192.168.0.0/16", "192.0.0.0/24", "192.0.2.0/24",
    "192.88.99.0/24", "198.18.0.0/15", "198.51.100.0/24", "203.0.113.0/24"))

#: IPv6 is an allow-list instead: global unicast only, minus the parts of it set
#: aside for transition mechanisms and documentation.
_GLOBAL_V6 = ipaddress.ip_network("2000::/3")
_BLOCKED_V6 = tuple(ipaddress.ip_network(n) for n in (
    "2001::/23", "2001:db8::/32", "2002::/16", "3fff::/20"))


def is_public_ip(address: str) -> bool:
    if "%" in address:                       # a scoped (link-local) address
        return False
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if ip.version == 4:
        return not any(ip in net for net in _BLOCKED_V4)
    return ip in _GLOBAL_V6 and not any(ip in net for net in _BLOCKED_V6)


@dataclass(frozen=True)
class Target:
    url: SplitResult
    address: str
    family: int

    @property
    def href(self) -> str:
        return self.url.geturl()


async def system_resolver(hostname: str) -> list[Answer]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
    return [(info[4][0], 4 if info[0] == socket.AF_INET else 6) for info in infos]


def _blocked() -> BrowserError:
    return BrowserError(
        "BLOCKED_URL",
        "I can only open public web pages (http or https, the normal ports). "
        "Addresses on this computer or your own network are off limits.")


async def validate_public_url(value: str, resolve: Resolver | None = None) -> Target:
    """The checked destination of `value`, or a BrowserError saying why not."""
    resolve = resolve or system_resolver
    try:
        url = urlsplit(value)
        port = url.port
        hostname = (url.hostname or "").rstrip(".").lower()
    except ValueError:
        raise _blocked() from None
    if (url.scheme not in ("http", "https") or url.username or url.password
            or (port is not None and port not in (80, 443))
            or not hostname or _PRIVATE_NAMES.search(hostname)):
        raise _blocked()

    literal = _literal_family(hostname)
    if literal:
        answers: list[Answer] = [(hostname, literal)]
    else:
        try:
            answers = await asyncio.wait_for(resolve(hostname), DNS_TIMEOUT_S)
        except (OSError, asyncio.TimeoutError):
            raise BrowserError("DNS_UNAVAILABLE",
                               "That address could not be found. Check the spelling, or "
                               "whether the site is down.", 502) from None
    if not answers or any(not is_public_ip(address) for address, _ in answers):
        raise _blocked()
    address, family = next((a for a in answers if a[1] == 4), answers[0])
    return Target(url=url, address=address, family=family)


def _literal_family(hostname: str) -> int:
    try:
        return ipaddress.ip_address(hostname).version
    except ValueError:
        return 0

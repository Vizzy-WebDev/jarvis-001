"""Every connector's real, current logo — catalogue and custom alike.

**A connector should look like the app it is.** A hand-drawn set of marks goes
stale the moment a service rebrands, which is exactly what happened to the ones
this replaces, so nothing here is drawn by hand: each logo is fetched and cached,
and refreshed on its own after `REFRESH_AFTER_S` so a rebrand catches up with no
release.

**No address a user or a config supplied is ever fetched.** That is worth being
precise about, because "resolve a logo from a connector's own URL" is the obvious
design and it is the one with a real attack surface — a connector's address is
typed by a person or arrives in a config, and following it would let anything on
the machine's own network be reached from here. Instead the host is used only as
a LOOKUP KEY against one fixed, public icon service, and the only other addresses
this file will fetch are constants written below. A host that is not a plausible
hostname is refused before it is even used as a key.

The monogram the interface draws is what shows while nothing has resolved yet,
or when a service genuinely has no logo to find — never a settled answer for a
custom connector.
"""

from __future__ import annotations

import base64
import logging
import re
import time
from typing import Any
from urllib.parse import urlsplit

import httpx

from ..store import read_json, write_json

logger = logging.getLogger(__name__)

FILE = "connector-icons"

#: The one service a host is looked up against. The host only ever appears as a
#: path segment here, never as somewhere to connect to.
LOOKUP = "https://icons.duckduckgo.com/ip3/{host}.ico"

#: Where the generic lookup returns something poor and a better one is known.
#: Fixed constants in this file — never anything a config could point at.
CURATED = {
    "gmail": "https://upload.wikimedia.org/wikipedia/commons/7/7e/Gmail_icon_%282020%29.svg",
    "google-drive":
        "https://upload.wikimedia.org/wikipedia/commons/5/5f/Google_Drive_icon_%282026%29.svg",
}

TIMEOUT_S = 6.0
#: An icon is a small thing; anything larger is not one, and is refused rather
#: than stored in a JSON file that is read on every screen.
MAX_BYTES = 96 * 1024
#: How long a cached logo is trusted before it is fetched again. Two weeks keeps
#: a rebrand from lingering without making this chatty.
REFRESH_AFTER_S = 14 * 24 * 60 * 60
#: A failure is remembered too, briefly, so a service with no logo is not asked
#: for one on every single read.
RETRY_FAILURE_AFTER_S = 24 * 60 * 60

#: Deliberately strict: this becomes part of a URL path, so anything that could
#: change what is being requested — a slash, a dot-segment, a space — is not a
#: host as far as this file is concerned.
HOSTNAME = re.compile(r"^(?=.{1,253}$)[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?"
                      r"(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$")

IMAGE_TYPES = ("image/", "application/octet-stream")


def _is_address(host: str) -> bool:
    """An IP literal rather than a name.

    Refused not because it is dangerous — nothing here connects to it — but
    because looking one up is pointless and would hand a third party the
    address of something on this machine's own network.
    """
    import ipaddress

    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def host_of(url: str) -> str | None:
    """The hostname in a URL, if it is one this will look up."""
    try:
        host = (urlsplit(str(url or "")).hostname or "").strip().lower()
    except ValueError:
        return None
    if not host or _is_address(host) or not HOSTNAME.match(host):
        return None
    return host


def apex_of(host: str) -> str | None:
    """`mcp.notion.com` -> `notion.com`. Many services put their logo only on the
    bare domain, and a subdomain lookup comes back empty."""
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) > 2 else None


def _cache() -> dict[str, Any]:
    data = read_json(FILE, {"icons": {}})
    return data if isinstance(data, dict) and isinstance(data.get("icons"), dict) \
        else {"icons": {}}


def _fetch(url: str) -> str | None:
    """One image as a data URI, or None. Never raises."""
    try:
        with httpx.Client(timeout=TIMEOUT_S, follow_redirects=True) as client:
            response = client.get(url)
        if response.status_code != 200:
            return None
        kind = (response.headers.get("content-type") or "").split(";")[0].strip().lower()
        if not kind.startswith(IMAGE_TYPES):
            return None
        body = response.content
        # Checked after reading rather than trusting a declared length, which is
        # not something the far end is obliged to be honest about.
        if not body or len(body) > MAX_BYTES:
            return None
        if kind == "application/octet-stream":
            kind = "image/x-icon"
        return f"data:{kind};base64,{base64.b64encode(body).decode('ascii')}"
    except (httpx.HTTPError, ValueError, OSError) as err:
        logger.debug("could not fetch an icon from %s: %s", url, err)
        return None


def _resolve_now(host: str, curated_key: str | None) -> str | None:
    if curated_key and curated_key in CURATED:
        found = _fetch(CURATED[curated_key])
        if found:
            return found
    found = _fetch(LOOKUP.format(host=host))
    if found:
        return found
    apex = apex_of(host)
    return _fetch(LOOKUP.format(host=apex)) if apex else None


def icon_for(url_or_host: str, *, curated_key: str | None = None,
             refresh: bool = True) -> str | None:
    """This service's logo as a data URI, fetching it if it is missing or stale.

    `refresh=False` reads the cache only — for anywhere that must not make a
    network call, such as answering a request that is only listing what exists.
    """
    bare = (url_or_host or "").strip().lower()
    host = bare if (HOSTNAME.match(bare) and not _is_address(bare)) else host_of(url_or_host)
    if not host:
        return None

    cache = _cache()
    entry = cache["icons"].get(host) or {}
    age = time.time() - float(entry.get("fetchedAt") or 0)
    fresh_enough = age < (REFRESH_AFTER_S if entry.get("dataUri") else RETRY_FAILURE_AFTER_S)
    if entry and fresh_enough:
        return entry.get("dataUri")
    if not refresh:
        return entry.get("dataUri")

    found = _resolve_now(host, curated_key)
    cache["icons"][host] = {"dataUri": found, "fetchedAt": time.time()}
    write_json(FILE, cache)
    return found


def refresh_all(targets: list[tuple[str, str | None]]) -> int:
    """Re-resolve a batch, returning how many now have a logo. Used at startup so
    a rebrand catches up without anyone asking."""
    found = 0
    for url_or_host, curated_key in targets:
        if icon_for(url_or_host, curated_key=curated_key):
            found += 1
    return found

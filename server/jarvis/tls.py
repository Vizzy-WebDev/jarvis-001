"""One TLS context for the whole process.

Building one costs about a second on some Windows machines (loading the certificate bundle), and
`httpx.Client(...)` / `httpx.post(...)` build a fresh one every time unless given one. Left alone,
every web fetch, connector call and spoken sentence paid that second before a single byte moved.
A context is safe to share across threads and clients.
"""

from __future__ import annotations

import ssl
import threading

import httpx

_lock = threading.Lock()
_context: ssl.SSLContext | None = None


def context() -> ssl.SSLContext:
    global _context
    with _lock:
        if _context is None:
            _context = httpx.create_ssl_context()
        return _context

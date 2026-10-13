"""Jarvis's own browser.

A headless Chromium (the person's own Chrome or Edge), with a profile of its own
that keeps logins between uses, that Jarvis drives through its agent tools and the
person can watch and take over from a live preview. Built from OpenMuse's browser
(MIT licence, CopilotKit: `apps/worker`, `apps/server/src/browser*.ts`), ported to
Python and kept structurally the same:

    network.py   what the browser may reach: public web addresses only
    proxy.py     the one door out, which checks every destination
    manager.py   sessions: profile, queue, idle close, reading, acting
    host.py      the thread that owns it all, and the synchronous door to it
    console.py   the "Take control" page: a live screenshot you click and type into
    locate.py    which Chrome or Edge to drive

The agent tools are `connectors/browser_connector.py`; the HTTP routes are
`routes/browsers.py`; the chat card is `frontend/components/browser/`.
"""

from __future__ import annotations

from .errors import BrowserError
from .host import BrowserHost, get_host, set_host_for_tests, shutdown

__all__ = ["BrowserError", "BrowserHost", "get_host", "set_host_for_tests", "shutdown"]

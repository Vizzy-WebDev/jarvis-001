"""Where the browser lives inside Jarvis, and the plain synchronous door to it.

Playwright's async API wants one event loop that is never blocked, and Jarvis's
callers are ordinary threads (a tool running for the model, a route handler in
FastAPI's thread pool). So the browser gets a thread of its own that runs one
event loop for as long as the browser exists, and everything else talks to it
through `BrowserHost`'s methods, each of which hands one job across and waits
for the answer.

Nothing starts until something asks for it: importing this module, creating the
app, or listing sessions launches no thread, no proxy and no browser. That is why
the browser has no `JARVIS_*` interlock like the clocks in `main.py`: it does not
run on a clock, it runs only while a session is open, and the only timer in it
(closing a window left idle for half an hour) exists only then.

Tests build their own host with a resolver and connector of their own, to point a
"public" address at a fixture server; there is deliberately no setting that lets
the real one reach a private address (see `network.py`).
"""

from __future__ import annotations

import asyncio
import atexit
import concurrent.futures
import importlib.util
import logging
import sys
import threading
from pathlib import Path
from typing import Any, Awaitable, Callable, TypeVar

from ..store import data_dir
from .errors import BrowserError
from .locate import find_browser
from .manager import BrowserManager
from .network import Resolver
from .proxy import EgressProxy

logger = logging.getLogger(__name__)

T = TypeVar("T")

#: How long one job may take before the caller is told so. Opening a page is the
#: slowest thing, and it has its own 20 second limit inside.
DEFAULT_TIMEOUT_S = 60.0
STARTUP_TIMEOUT_S = 40.0
SESSIONS_FOLDER = "browser-sessions"


class BrowserHost:
    def __init__(self, *, directory: Path | None = None,
                 make_proxy: Callable[[], Awaitable[EgressProxy]] | None = None,
                 resolve: Resolver | None = None,
                 find: Callable[[], str | None] = find_browser, **manager_options: Any) -> None:
        self._directory = directory
        self._make_proxy = make_proxy
        self._resolve = resolve
        self._find = find
        self._options = manager_options
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._manager: BrowserManager | None = None

    # --- lifecycle --------------------------------------------------------------------

    def availability(self) -> dict[str, Any]:
        """Whether a browser can be opened here, answered without opening one."""
        if importlib.util.find_spec("playwright") is None:
            return {"available": False,
                    "reason": "Jarvis's browser part isn't installed yet. Close Jarvis and open "
                              "it again with Start Jarvis, and it will finish setting itself up."}
        if not self._find():
            return {"available": False,
                    "reason": "There's no browser installed here for me to open. Jarvis uses "
                              "Microsoft Edge or Google Chrome."}
        return {"available": True, "reason": None}

    @property
    def started(self) -> bool:
        return self._manager is not None

    def _start(self) -> None:
        with self._lock:
            if self._manager is not None:
                return
            ready = threading.Event()
            box: dict[str, Any] = {}

            def runner() -> None:
                # Playwright starts the browser as a subprocess, which on Windows only the
                # proactor loop can do; the default there is already that, but say so.
                loop = asyncio.ProactorEventLoop() if sys.platform == "win32" \
                    else asyncio.new_event_loop()
                box["loop"] = loop
                try:
                    folder = self._directory or (data_dir() / SESSIONS_FOLDER)
                    box["manager"] = loop.run_until_complete(BrowserManager.create(
                        folder, make_proxy=self._make_proxy, resolve=self._resolve,
                        executable=self._find(), **self._options))
                except BaseException as err:  # noqa: BLE001 - reported to the caller below
                    box["error"] = err
                ready.set()
                if "manager" not in box:
                    loop.close()
                    return
                try:
                    loop.run_forever()
                finally:
                    try:
                        loop.run_until_complete(loop.shutdown_asyncgens())
                    finally:
                        loop.close()

            thread = threading.Thread(target=runner, name="jarvis-browser", daemon=True)
            thread.start()
            if not ready.wait(STARTUP_TIMEOUT_S) or "manager" not in box:
                err = box.get("error")
                raise BrowserError("BROWSER_UNAVAILABLE",
                                   "The browser couldn't start. If it keeps happening, restart "
                                   "Jarvis.", 503) from err
            self._thread, self._loop, self._manager = thread, box["loop"], box["manager"]

    def shutdown(self) -> None:
        with self._lock:
            manager, loop, thread = self._manager, self._loop, self._thread
            self._manager = self._loop = self._thread = None
        if manager is None or loop is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(manager.close(), loop).result(30)
        except Exception:  # noqa: BLE001 - shutting down must finish
            logger.debug("browser did not close cleanly", exc_info=True)
        loop.call_soon_threadsafe(loop.stop)
        if thread is not None:
            thread.join(10)

    # --- one job at a time across the thread boundary ---------------------------------

    def run(self, job: Callable[[BrowserManager], Awaitable[T]],
            timeout: float = DEFAULT_TIMEOUT_S) -> T:
        self._start()
        manager, loop = self._manager, self._loop
        assert manager is not None and loop is not None
        future = asyncio.run_coroutine_threadsafe(job(manager), loop)
        try:
            return future.result(timeout)
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise BrowserError("TIMEOUT", "That took too long, so I stopped waiting for it.",
                               504) from None
        except BrowserError:
            raise
        except Exception as err:  # noqa: BLE001 - a Playwright failure is a plain answer here
            logger.info("browser job failed: %s", err)
            raise BrowserError("BROWSER_FAILED",
                               "The browser ran into a problem with that page.", 502) from err

    # --- the operations ---------------------------------------------------------------

    def sessions(self) -> list[dict[str, str]]:
        return self.run(lambda m: _value(m.sessions()))

    def session(self, session_id: str) -> dict[str, str]:
        return self.run(lambda m: _value(m.session(session_id)))

    def is_running(self, session_id: str) -> bool:
        if self._manager is None:
            return False
        return self.run(lambda m: _value(m.is_running(session_id)))

    def open(self, session_id: str, url: str) -> dict[str, str]:
        return self.run(lambda m: m.create_session(session_id, url), 90)

    def navigate(self, session_id: str, url: str) -> dict[str, str]:
        return self.run(lambda m: m.navigate(session_id, url), 90)

    def close_session(self, session_id: str) -> dict[str, str]:
        return self.run(lambda m: m.close_session(session_id))

    def screenshot(self, session_id: str) -> bytes:
        return self.run(lambda m: m.screenshot(session_id), 30)

    def read(self, session_id: str, *, elements: bool = False) -> dict[str, Any]:
        return self.run(lambda m: m.read(session_id, elements=elements))

    def input(self, session_id: str, value: dict[str, Any]) -> dict[str, str]:
        return self.run(lambda m: m.input(session_id, value))

    def click(self, session_id: str, selector: str) -> dict[str, Any]:
        return self.run(lambda m: m.click(session_id, selector), 90)

    def type_into(self, session_id: str, selector: str, text: str,
                  submit: bool = False) -> dict[str, Any]:
        return self.run(lambda m: m.type_into(session_id, selector, text, submit), 90)

    def scroll(self, session_id: str, direction: str) -> dict[str, Any]:
        return self.run(lambda m: m.scroll(session_id, direction))

    def downloads(self, session_id: str) -> dict[str, Any]:
        """PDFs the window has caught and not yet imported, and the recent refusals."""
        return self.run(lambda m: _value(m.downloads(session_id)))

    def download_file(self, session_id: str, download_id: str) -> Path:
        return self.run(lambda m: _value(m.download_file(session_id, download_id)))

    def mark_imported(self, session_id: str, download_id: str, artifact_id: str) -> None:
        self.run(lambda m: _value(m.mark_imported(session_id, download_id, artifact_id)))


async def _value(value: T) -> T:
    return value


# --- the one host the app uses ------------------------------------------------------

_host: BrowserHost | None = None
_host_lock = threading.Lock()


def get_host() -> BrowserHost:
    global _host
    with _host_lock:
        if _host is None:
            _host = BrowserHost()
            atexit.register(shutdown)
        return _host


def set_host_for_tests(host: BrowserHost | None) -> None:
    """Swap the app's host (a test's, with fixtures wired in); closes the one it replaces."""
    global _host
    with _host_lock:
        previous, _host = _host, host
    if previous is not None and previous is not host:
        previous.shutdown()


def shutdown() -> None:
    """Close the browser, if one was ever opened. Safe to call any time, any number of times."""
    with _host_lock:
        host = _host
    if host is not None:
        host.shutdown()

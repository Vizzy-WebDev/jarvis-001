"""A real browser window Jarvis drives, when a page has to be interacted with.

**Its own window, its own profile, never the user's.** Reusing an already-open
Chrome was considered and rejected: a real tab's contents are not reliably
readable from outside, and a task that borrows someone's browser inherits their
logins, their session cookies and their open work. This one starts empty, in its
own profile directory under the data folder, and closing it loses nothing.

**A page is read by running a small script against it, never by looking at a
picture of it.** Clicking a browser blindly with real screen coordinates is the
fragile way to do this; asking the page itself what its text and its elements
are is the accurate one, which is why this is a connector rather than something
the desktop control loop drives with the mouse.

**Separate from `webrender.py`, deliberately, though they share how a browser is
found.** That one renders a page headlessly for a lookup and closes it; this one
is a window someone is watching. A background lookup must never navigate a page
the user is in the middle of, so they never share an instance.

Playwright is an optional import, exactly as it is there: no browser installed
means a plain answer saying so, not a stack trace about a missing module.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from ..store import data_dir
from ..webrender import browser_path

logger = logging.getLogger(__name__)

NAV_TIMEOUT_MS = 20000
#: How much page text is worth returning. A whole page of markup is not an
#: answer, and the model has to read every character of it.
MAX_TEXT_CHARS = 20000

TOOLS: tuple[dict[str, Any], ...] = (
    {
        "name": "browser_navigate",
        "description": ("Open a web page in a real browser window the user can see, and "
                        "return what it says. Use when a page has to be interacted with, "
                        "or when they asked to browse — for a plain lookup, read_web_page "
                        "is invisible and faster."),
        "parameters": {"type": "object",
                       "properties": {"url": {"type": "string",
                                              "description": "The address to open."}},
                       "required": ["url"]},
    },
    {
        "name": "browser_read_page",
        "description": "Read the current page's title, address and visible text.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "browser_click",
        "description": "Click something on the current page, by CSS selector.",
        # Declared rather than inferred. The word-based classifier exists for
        # tools from a server this build has never seen; these declarations are
        # ours, and we know what they do. Clicking an arbitrary page can pay,
        # send or post — and unlike the control loop there is no overlay and no
        # watching user to stop it, so it asks.
        "risk": "risky",
        "parameters": {"type": "object",
                       "properties": {"selector": {"type": "string"}},
                       "required": ["selector"]},
    },
    {
        "name": "browser_type",
        "description": "Type into a field on the current page, by CSS selector.",
        "risk": "risky",
        "parameters": {"type": "object",
                       "properties": {"selector": {"type": "string"},
                                      "text": {"type": "string"},
                                      "submit": {"type": "boolean",
                                                 "description": "Press Enter afterwards."}},
                       "required": ["selector", "text"]},
    },
    {
        "name": "browser_close",
        "description": "Close the browser window Jarvis opened.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
)


def tool_declarations(config: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    return [dict(tool) for tool in TOOLS]


class NoBrowser(RuntimeError):
    pass


class _Session:
    """The one open window, its Playwright handle, and the lock around it.

    A single window on purpose: "the current page" is the whole vocabulary these
    tools use, and it only means something when there is one.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._playwright: Any = None
        self._context: Any = None
        self._page: Any = None

    def profile_dir(self) -> str:
        # Under the data directory, like everything else — a browser profile is
        # exactly the sort of large, machine-owned state that a hardcoded path
        # relative to this file would drop into the real folder during a test.
        path = data_dir() / "browser-profile"
        path.mkdir(parents=True, exist_ok=True)
        return str(path)

    def page(self, *, headless: bool = False) -> Any:
        with self._lock:
            if self._page is not None and not self._page.is_closed():
                return self._page
            executable = browser_path()
            if not executable:
                raise NoBrowser("There's no browser installed here for me to open.")
            try:
                from playwright.sync_api import sync_playwright
            except ImportError as err:  # pragma: no cover - depends on the install
                raise NoBrowser("There's no browser available for me to open.") from err

            self._playwright = sync_playwright().start()
            self._context = self._playwright.chromium.launch_persistent_context(
                self.profile_dir(), headless=headless, executable_path=executable,
                args=["--no-first-run", "--no-default-browser-check"])
            self._page = self._context.pages[0] if self._context.pages \
                else self._context.new_page()
            return self._page

    def close(self) -> bool:
        with self._lock:
            open_before = self._page is not None
            for closing in (self._context, self._playwright):
                try:
                    if closing is not None:
                        (closing.close if hasattr(closing, "close") else closing.stop)()
                except Exception as err:  # noqa: BLE001
                    logger.info("could not close the browser cleanly: %s", err)
            self._page = self._context = self._playwright = None
            return open_before

    @property
    def is_open(self) -> bool:
        return self._page is not None and not self._page.is_closed()


_session = _Session()


def _text_of(page: Any) -> str:
    text = page.evaluate("() => document.body ? document.body.innerText : ''") or ""
    return text[:MAX_TEXT_CHARS]


def dispatch(name: str, args: dict[str, Any], config: dict[str, Any] | None = None, *,
             session: _Session | None = None) -> dict[str, Any]:
    live = session or _session
    config = config or {}
    # Headless only where a real window is impossible — the point of this
    # connector is a window someone can watch, so it is never the default.
    headless = bool(config.get("headless"))

    if name == "browser_close":
        return {"ok": True, "closed": live.close()}

    if name == "browser_navigate":
        url = str(args.get("url") or "").strip()
        if not url:
            return {"ok": False, "error": "No address to open."}
        if not url.startswith(("http://", "https://")):
            url = f"https://{url}"
        page = live.page(headless=headless)
        page.goto(url, timeout=NAV_TIMEOUT_MS, wait_until="domcontentloaded")
        return {"ok": True, "url": page.url, "title": page.title(), "text": _text_of(page)}

    if not live.is_open:
        return {"ok": False,
                "error": "No page is open — use browser_navigate first."}
    page = live.page(headless=headless)

    if name == "browser_read_page":
        return {"ok": True, "url": page.url, "title": page.title(), "text": _text_of(page)}

    if name == "browser_click":
        selector = str(args.get("selector") or "")
        page.click(selector, timeout=NAV_TIMEOUT_MS)
        return {"ok": True, "clicked": selector, "url": page.url,
                "text": _text_of(page)}

    if name == "browser_type":
        selector = str(args.get("selector") or "")
        page.fill(selector, str(args.get("text") or ""), timeout=NAV_TIMEOUT_MS)
        if args.get("submit"):
            page.press(selector, "Enter")
            page.wait_for_load_state("domcontentloaded", timeout=NAV_TIMEOUT_MS)
        return {"ok": True, "url": page.url, "text": _text_of(page)}

    return {"ok": False, "error": f"{name} is not something the browser can do."}


def close_for_tests() -> None:
    _session.close()

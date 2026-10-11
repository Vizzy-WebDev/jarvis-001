"""Browser sessions: one persistent Chromium profile each, driven through Playwright.

Ported from OpenMuse's `apps/worker/src/browser.ts` (MIT licence, CopilotKit) and
kept structurally the same: a session is a UUID, a profile directory and a small
`session.json`; at most three run at once; every operation on one session is
queued behind the one before it; an idle one is closed after half an hour and
reopens, logged in, with the same id; popups and dialogs are dismissed; service
workers and WebSockets are off; every destination is checked, twice (see
`network.py` and `proxy.py`).

Where Jarvis differs from OpenMuse, and why:

* It is a module inside Jarvis, not a separate token-protected service: Jarvis
  is one process on one port. All of this runs on ONE event loop in a dedicated
  thread (`host.py`), which is what Playwright's async API needs.
* The agent can act through the page's own elements (`click`, `type_into`,
  `scroll`) as well as the person through coordinates (`input`), and a read
  lists the page's clickable elements with selectors, because a model cannot
  click what it cannot name.
* The browser is whichever Chrome or Edge the person already has (`locate.py`).

Everything that comes out of a page is untrusted text. This module never
interprets it; the tool layer says so to the model.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from ..childenv import scrubbed_environment
from .errors import BrowserError
from .network import Resolver, validate_public_url
from .proxy import EgressProxy, start_egress_proxy

logger = logging.getLogger(__name__)

SESSION_ID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$", re.I)

VIEWPORT = {"width": 1280, "height": 800}
NAV_TIMEOUT_MS = 20_000
ACTION_TIMEOUT_MS = 10_000
MAX_READ_CHARS = 100_000
MAX_SESSIONS = 3
MAX_PROFILES = 20
IDLE_TIMEOUT_S = 30 * 60
MAX_ELEMENTS = 80
MAX_TYPED_CHARS = 10_000

_KEYS = re.compile(r"^(Enter|Tab|Escape|Backspace|Delete|ArrowUp|ArrowDown|ArrowLeft|ArrowRight|"
                   r"Home|End|PageUp|PageDown|Control\+a|Meta\+a|Shift\+Tab)$")

#: Runs inside the page. A fixed script: callers cannot pass their own.
_READ_SCRIPT = """() => {
  const text = document.body ? document.body.innerText : '';
  return {url: location.href, title: document.title.slice(0, 300),
          text: text.slice(0, %d), truncated: text.length > %d,
          scroll: {y: Math.round(window.scrollY), height: document.documentElement.scrollHeight,
                   viewport: window.innerHeight}};
}""" % (MAX_READ_CHARS, MAX_READ_CHARS)

_ELEMENTS_SCRIPT = """() => {
  const MAX = %d;
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) return false;
    const s = getComputedStyle(el);
    return s.visibility !== 'hidden' && s.display !== 'none';
  };
  const unique = (sel) => { try { return document.querySelectorAll(sel).length === 1; } catch (e) { return false; } };
  const pathOf = (el) => {
    const parts = [];
    while (el && el.nodeType === 1 && el !== document.documentElement) {
      if (el.id && unique('#' + CSS.escape(el.id))) { parts.unshift('#' + CSS.escape(el.id)); break; }
      let part = el.tagName.toLowerCase();
      const parent = el.parentElement;
      if (parent) {
        const same = Array.prototype.filter.call(parent.children, (c) => c.tagName === el.tagName);
        if (same.length > 1) part += ':nth-of-type(' + (same.indexOf(el) + 1) + ')';
      }
      parts.unshift(part);
      el = parent;
    }
    return parts.join(' > ');
  };
  const selectorFor = (el) => {
    if (el.id) { const s = '#' + CSS.escape(el.id); if (unique(s)) return s; }
    for (const attr of ['data-testid', 'name', 'aria-label']) {
      const v = el.getAttribute(attr);
      if (v) { const s = el.tagName.toLowerCase() + '[' + attr + '=' + JSON.stringify(v) + ']'; if (unique(s)) return s; }
    }
    return pathOf(el);
  };
  const found = [];
  const nodes = document.querySelectorAll(
    'a[href],button,input,select,textarea,summary,[role=button],[role=link],[role=tab],' +
    '[role=checkbox],[role=menuitem],[role=option],[onclick]');
  for (const el of nodes) {
    if (found.length >= 400) break;
    if (el.disabled || !visible(el)) continue;
    const tag = el.tagName.toLowerCase();
    const type = tag === 'input' ? (el.getAttribute('type') || 'text').toLowerCase() : undefined;
    if (type === 'hidden') continue;
    // What a person would call it. A field's label never comes from its VALUE: that is what
    // was typed (or a password), and a value is reported separately and only when safe.
    const isField = tag === 'input' || tag === 'textarea' || tag === 'select';
    const isButtonInput = tag === 'input' && ['submit', 'button', 'reset'].indexOf(type) >= 0;
    let label = el.getAttribute('aria-label') || '';
    if (!label && el.id) { const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]'); if (l) label = l.innerText; }
    if (!label && isField) { const wrap = el.closest('label'); if (wrap) label = wrap.innerText; }
    if (!label) label = isField && !isButtonInput ? (el.placeholder || el.title || el.name || '')
                                                  : (el.innerText || (isButtonInput ? el.value : '') || el.title || el.alt || '');
    label = label.trim().replace(/\\s+/g, ' ').slice(0, 80);
    const rect = el.getBoundingClientRect();
    const item = {selector: selectorFor(el), tag: tag, label: label,
                  _inView: rect.bottom > 0 && rect.top < window.innerHeight};
    if (type) item.type = type;
    if (tag === 'a') item.href = (el.href || '').slice(0, 200);
    if (type === 'checkbox' || type === 'radio') item.checked = !!el.checked;
    else if ((tag === 'input' || tag === 'textarea') && type !== 'password' && el.value &&
             type !== 'button' && type !== 'submit') item.value = String(el.value).slice(0, 60);
    if (tag === 'select') item.options = Array.prototype.slice.call(el.options, 0, 12).map((o) => o.text.trim().slice(0, 40));
    found.push(item);
  }
  // When there are too many, keep what is on screen first, then the rest, in page order.
  let chosen = found;
  if (found.length > MAX) {
    const onScreen = found.filter((i) => i._inView);
    const rest = found.filter((i) => !i._inView);
    const keep = new Set(onScreen.concat(rest).slice(0, MAX));
    chosen = found.filter((i) => keep.has(i));
  }
  return chosen.map((i) => { const o = Object.assign({}, i); delete o._inView; return o; });
}""" % MAX_ELEMENTS


def validate_session_id(value: Any) -> str:
    if not isinstance(value, str) or not SESSION_ID.match(value):
        raise BrowserError("INVALID_SESSION", "A valid UUID session ID is required.")
    return value.lower()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass
class Session:
    id: str
    title: str
    url: str
    status: str  # "active" | "closed" | "error"
    updated_at: str

    def as_dict(self) -> dict[str, str]:
        return {"id": self.id, "title": self.title, "url": self.url, "status": self.status,
                "updatedAt": self.updated_at}

    @classmethod
    def from_dict(cls, raw: dict[str, Any], id: str) -> "Session":
        return cls(id=id, title=str(raw.get("title") or "")[:300], url=str(raw.get("url") or ""),
                   status="closed", updated_at=str(raw.get("updatedAt") or _now()))


@dataclass
class _Running:
    context: Any
    page: Any
    touched: float
    temp_dir: Path | None = None
    download_error: bool = False


ProxyFactory = Callable[[], Awaitable[EgressProxy]]


class BrowserManager:
    def __init__(self, data_dir: Path, proxy: EgressProxy, *, executable: str | None,
                 resolve: Resolver | None = None, max_sessions: int = MAX_SESSIONS,
                 idle_timeout_s: float = IDLE_TIMEOUT_S, headless: bool = True) -> None:
        self.data_dir = data_dir
        self._proxy = proxy
        self._executable = executable
        self._resolve = resolve
        self._max_sessions = max_sessions
        self._idle_timeout_s = idle_timeout_s
        self._headless = headless
        self._sessions: dict[str, Session] = {}
        self._running: dict[str, _Running] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._create_lock = asyncio.Lock()
        self._playwright: Any = None
        self._sweeper: asyncio.Task | None = None
        self._download_hooks: list[Callable[..., Any]] = []

    # --- construction -----------------------------------------------------------------

    @classmethod
    async def create(cls, data_dir: Path, *, make_proxy: ProxyFactory | None = None,
                     resolve: Resolver | None = None, **options: Any) -> "BrowserManager":
        data_dir.mkdir(parents=True, exist_ok=True)
        proxy = await (make_proxy() if make_proxy else start_egress_proxy(resolve=resolve))
        manager = cls(data_dir, proxy, resolve=resolve, **options)
        manager._restore()
        manager._sweeper = asyncio.ensure_future(manager._sweep())
        return manager

    def _restore(self) -> None:
        for entry in self.data_dir.iterdir():
            if not (entry.is_dir() and SESSION_ID.match(entry.name)):
                continue
            try:
                raw = json.loads((entry / "session.json").read_text("utf-8"))
            except (OSError, ValueError):
                continue                       # an incomplete first launch has nothing to restore
            if isinstance(raw, dict):
                self._sessions[entry.name] = Session.from_dict(raw, entry.name)

    # --- small helpers ----------------------------------------------------------------

    def directory(self, session_id: str) -> Path:
        return self.data_dir / validate_session_id(session_id)

    def _lock(self, session_id: str) -> asyncio.Lock:
        return self._locks.setdefault(session_id, asyncio.Lock())

    def _persist(self, session: Session) -> None:
        folder = self.directory(session.id)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / "session.json"
        temp = path.with_suffix(".json.tmp")
        temp.write_text(json.dumps(session.as_dict()), "utf-8")
        os.replace(temp, path)

    def _active(self, session_id: str) -> _Running:
        running = self._running.get(session_id)
        if running is None or running.page.is_closed():
            raise BrowserError("SESSION_CLOSED",
                               "That browser window is closed. Open it again to carry on.", 409)
        running.touched = time.monotonic()
        return running

    def is_running(self, session_id: str) -> bool:
        running = self._running.get(session_id)
        return running is not None and not running.page.is_closed()

    def sessions(self) -> list[dict[str, str]]:
        return [s.as_dict() for s in self._sessions.values()]

    def session(self, session_id: str) -> dict[str, str]:
        found = self._sessions.get(validate_session_id(session_id))
        if found is None:
            raise BrowserError("SESSION_NOT_FOUND", "Browser session not found.", 404)
        return found.as_dict()

    async def _validate(self, url: str):
        return await validate_public_url(url, self._resolve)

    async def _refresh(self, session_id: str) -> Session:
        running = self._active(session_id)
        if running.page.url != "about:blank":
            await self._validate(running.page.url)
        session = Session(id=session_id, title=(await running.page.title())[:300],
                          url=running.page.url, status="active", updated_at=_now())
        self._sessions[session_id] = session
        self._persist(session)
        return session

    # --- opening, navigating, closing -------------------------------------------------

    async def _playwright_handle(self) -> Any:
        if self._playwright is None:
            try:
                from playwright.async_api import async_playwright
            except ImportError as err:
                raise BrowserError(
                    "BROWSER_UNAVAILABLE",
                    "Jarvis's browser part isn't installed yet. Close Jarvis and open it "
                    "again with Start Jarvis, and it will finish setting itself up.",
                    503) from err
            self._playwright = await async_playwright().start()
        return self._playwright

    async def create_session(self, session_id: str, url: str) -> dict[str, str]:
        session_id = validate_session_id(session_id)
        async with self._create_lock:
            async with self._lock(session_id):
                return (await self._create(session_id, url)).as_dict()

    async def _create(self, session_id: str, url: str) -> Session:
        await self._validate(url)
        if self.is_running(session_id):
            return await self._navigate(session_id, url)
        if sum(1 for r in self._running.values() if not r.page.is_closed()) >= self._max_sessions:
            raise BrowserError("SESSION_LIMIT",
                               f"Close a browser window before opening another "
                               f"(the limit is {self._max_sessions}).", 409)
        if session_id not in self._sessions and len(self._sessions) >= MAX_PROFILES:
            raise BrowserError("PROFILE_LIMIT",
                               f"The browser has reached its limit of {MAX_PROFILES} saved "
                               "profiles.", 409)
        if not self._executable:
            raise BrowserError("BROWSER_UNAVAILABLE",
                               "There's no browser installed here for me to open. Jarvis uses "
                               "Microsoft Edge or Google Chrome.", 503)
        previous = self._sessions.get(session_id)
        folder = self.directory(session_id)
        profile = folder / "profile"
        temp_dir = Path(tempfile.gettempdir()) / f"jarvis-browser-downloads-{session_id}"
        profile.mkdir(parents=True, exist_ok=True)
        temp_dir.mkdir(parents=True, exist_ok=True)
        playwright = await self._playwright_handle()
        try:
            context = await playwright.chromium.launch_persistent_context(
                str(profile), executable_path=self._executable, headless=self._headless,
                viewport=VIEWPORT, proxy={"server": self._proxy.url, "bypass": "<-loopback>"},
                service_workers="block", accept_downloads=True, downloads_path=str(temp_dir),
                timeout=25_000, env=scrubbed_environment(),
                args=["--disable-quic", "--disable-extensions", "--no-first-run",
                      "--no-default-browser-check",
                      "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
                      "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1"])
        except Exception as err:  # noqa: BLE001 - any launch failure reads the same to a person
            logger.info("browser did not start: %s", err)
            if previous is None:
                shutil.rmtree(folder, ignore_errors=True)
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise BrowserError("BROWSER_UNAVAILABLE",
                               "The browser couldn't start. If it keeps happening, restart "
                               "Jarvis.", 503) from err
        try:
            await self._restore_cookies(context, folder)
            await context.route("**/*", self._guard_request)
            await context.route_web_socket("**/*", self._close_socket)
            for old in list(context.pages):
                await old.close()
            page = await context.new_page()
            page.set_default_timeout(ACTION_TIMEOUT_MS)
            await self._normalise_user_agent(context, page)
            running = _Running(context=context, page=page, touched=time.monotonic(),
                               temp_dir=temp_dir)
            self._running[session_id] = running
            context.on("page", lambda popup: asyncio.ensure_future(self._close_quietly(popup)))
            page.on("dialog", lambda dialog: asyncio.ensure_future(self._dismiss(dialog)))
            for hook in self._download_hooks:
                hook(session_id, page, running)
            initial = Session(id=session_id, title=previous.title if previous else "New session",
                              url=url, status="active", updated_at=_now())
            self._sessions[session_id] = initial
            self._persist(initial)
            return await self._navigate(session_id, url)
        except BaseException:
            await self._discard(context)
            self._running.pop(session_id, None)
            if previous is not None:
                failed = Session(id=session_id, title=previous.title, url=url, status="error",
                                 updated_at=_now())
                self._sessions[session_id] = failed
                self._persist(failed)
            else:
                self._sessions.pop(session_id, None)
                shutil.rmtree(folder, ignore_errors=True)
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise

    async def _restore_cookies(self, context: Any, folder: Path) -> None:
        try:
            state = json.loads((folder / "storage.json").read_text("utf-8"))
        except (OSError, ValueError):
            return
        cookies = state.get("cookies") if isinstance(state, dict) else None
        if cookies:
            try:
                await context.add_cookies(cookies)
            except Exception:  # noqa: BLE001 - the profile itself still holds them
                logger.info("saved cookies could not be restored", exc_info=True)

    async def _guard_request(self, route: Any) -> None:
        try:
            await self._validate(route.request.url)
            await route.continue_()
        except Exception:  # noqa: BLE001 - anything unchecked is refused
            try:
                await route.abort("blockedbyclient")
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    async def _close_socket(socket: Any) -> None:
        try:
            await socket.close()
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    async def _close_quietly(page: Any) -> None:
        try:
            await page.close()
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    async def _dismiss(dialog: Any) -> None:
        try:
            await dialog.dismiss()
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    async def _normalise_user_agent(context: Any, page: Any) -> None:
        """A headless browser announces itself as "HeadlessChrome", which plenty of
        sites answer with a block page. Say what it really is: Chrome."""
        try:
            agent = await page.evaluate("navigator.userAgent")
            if "HeadlessChrome" in agent:
                cdp = await context.new_cdp_session(page)
                await cdp.send("Network.setUserAgentOverride",
                               {"userAgent": agent.replace("HeadlessChrome", "Chrome")})
        except Exception:  # noqa: BLE001 - cosmetic; browsing works without it
            logger.debug("could not normalise the user agent", exc_info=True)

    @staticmethod
    async def _discard(context: Any) -> None:
        try:
            await context.close()
        except Exception:  # noqa: BLE001
            pass

    async def navigate(self, session_id: str, url: str) -> dict[str, str]:
        session_id = validate_session_id(session_id)
        async with self._lock(session_id):
            return (await self._navigate(session_id, url)).as_dict()

    async def _navigate(self, session_id: str, url: str) -> Session:
        target = await self._validate(url)
        page = self._active(session_id).page
        try:
            await page.goto(target.href, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            # A redirect can land somewhere the proxy refused; its 403 is still an HTTP
            # answer, so check where the page really ended up before calling this a success.
            await self._validate(page.url)
        except BrowserError as err:
            if err.code == "BLOCKED_URL":
                await self._blank(page)
            raise
        except Exception as err:  # noqa: BLE001
            # A file download aborts the navigation on purpose; the page stays where it was.
            if "Download is starting" not in str(err):
                raise BrowserError("NAVIGATION_FAILED", _why_navigation_failed(err), 502) from err
        return await self._refresh(session_id)

    @staticmethod
    async def _blank(page: Any) -> None:
        try:
            await page.goto("about:blank", timeout=5_000)
        except Exception:  # noqa: BLE001
            pass

    async def close_session(self, session_id: str) -> dict[str, str]:
        session_id = validate_session_id(session_id)
        async with self._lock(session_id):
            return (await self._close(session_id)).as_dict()

    async def _close(self, session_id: str) -> Session:
        stored = self._sessions.get(session_id)
        if stored is None:
            raise BrowserError("SESSION_NOT_FOUND", "Browser session not found.", 404)
        running = self._running.get(session_id)
        if running is not None:
            try:
                await running.context.storage_state(
                    path=str(self.directory(session_id) / "storage.json"))
            except Exception:  # noqa: BLE001 - saving cookies is best effort during teardown
                pass
            await self._discard(running.context)
            self._running.pop(session_id, None)
            if running.temp_dir:
                shutil.rmtree(running.temp_dir, ignore_errors=True)
        result = Session(id=stored.id, title=stored.title, url=stored.url, status="closed",
                         updated_at=_now())
        self._sessions[session_id] = result
        self._persist(result)
        return result

    async def _sweep(self) -> None:
        while True:
            await asyncio.sleep(60)
            for session_id, running in list(self._running.items()):
                if time.monotonic() - running.touched > self._idle_timeout_s:
                    try:
                        await self.close_session(session_id)
                    except Exception:  # noqa: BLE001 - the next sweep tries again
                        logger.debug("idle close failed", exc_info=True)

    # --- looking ----------------------------------------------------------------------

    async def screenshot(self, session_id: str) -> bytes:
        session_id = validate_session_id(session_id)
        async with self._lock(session_id):
            return await self._active(session_id).page.screenshot(type="png", timeout=10_000)

    async def read(self, session_id: str, *, elements: bool = False) -> dict[str, Any]:
        session_id = validate_session_id(session_id)
        async with self._lock(session_id):
            return await self._read(session_id, elements=elements)

    async def _read(self, session_id: str, *, elements: bool = False) -> dict[str, Any]:
        page = self._active(session_id).page
        if page.url != "about:blank":
            await self._validate(page.url)
        # The script is fixed by this module; nothing a caller sends is ever evaluated.
        result = await page.evaluate(_READ_SCRIPT)
        if result["url"] != "about:blank":
            await self._validate(result["url"])
        if elements:
            try:
                result["elements"] = await page.evaluate(_ELEMENTS_SCRIPT)
            except Exception:  # noqa: BLE001 - a page that will not list itself is still readable
                result["elements"] = []
        session = Session(id=session_id, title=result["title"], url=result["url"],
                          status="active", updated_at=_now())
        self._sessions[session_id] = session
        self._persist(session)
        return result

    # --- acting: the person, by coordinates -------------------------------------------

    async def input(self, session_id: str, value: dict[str, Any]) -> dict[str, str]:
        session_id = validate_session_id(session_id)
        async with self._lock(session_id):
            page = self._active(session_id).page
            kind = value.get("type")
            x, y, key = value.get("x"), value.get("y"), value.get("key")
            text, delta = value.get("text"), value.get("deltaY")
            if (kind == "click" and _is_number(x) and _is_number(y)
                    and 0 <= x < VIEWPORT["width"] and 0 <= y < VIEWPORT["height"]):
                await page.mouse.click(x, y)
            elif kind == "text" and isinstance(text, str) and len(text) <= MAX_TYPED_CHARS:
                await page.keyboard.insert_text(text)
            elif kind == "key" and isinstance(key, str) and _KEYS.match(key):
                await page.keyboard.press(key)
            elif kind == "scroll" and _is_number(delta) and abs(delta) <= 5000:
                await page.mouse.wheel(0, delta)
            else:
                raise BrowserError("INVALID_INPUT", "Unsupported browser input or coordinates.")
            return (await self._refresh(session_id)).as_dict()

    # --- acting: the agent, through the page's own elements ---------------------------

    async def click(self, session_id: str, selector: str) -> dict[str, Any]:
        session_id = validate_session_id(session_id)
        async with self._lock(session_id):
            page = self._active(session_id).page
            try:
                # A link that wants a new tab would open a popup, which is dismissed;
                # make it open in this one instead.
                await page.locator(selector).first.evaluate(
                    "el => { const a = el.closest('a'); "
                    "if (a && a.target && a.target !== '_self') a.target = '_self'; }",
                    timeout=ACTION_TIMEOUT_MS)
                await page.click(selector, timeout=ACTION_TIMEOUT_MS)
            except BrowserError:
                raise
            except Exception as err:  # noqa: BLE001
                raise BrowserError("ACTION_FAILED", _why_action_failed(err, selector), 422) from err
            await self._settle(page)
            await self._refresh(session_id)
            return await self._read(session_id, elements=True)

    async def type_into(self, session_id: str, selector: str, text: str,
                        submit: bool = False) -> dict[str, Any]:
        session_id = validate_session_id(session_id)
        if len(text) > MAX_TYPED_CHARS:
            raise BrowserError("INVALID_INPUT", "That is too much text to type at once.")
        async with self._lock(session_id):
            page = self._active(session_id).page
            try:
                tag = await page.locator(selector).first.evaluate(
                    "el => el.tagName", timeout=ACTION_TIMEOUT_MS)
                if tag == "SELECT":
                    try:
                        await page.select_option(selector, label=text, timeout=ACTION_TIMEOUT_MS)
                    except Exception:  # noqa: BLE001 - not a label; try it as the option's value
                        await page.select_option(selector, value=text, timeout=ACTION_TIMEOUT_MS)
                else:
                    await page.fill(selector, text, timeout=ACTION_TIMEOUT_MS)
                    if submit:
                        await page.press(selector, "Enter", timeout=ACTION_TIMEOUT_MS)
            except Exception as err:  # noqa: BLE001
                raise BrowserError("ACTION_FAILED", _why_action_failed(err, selector), 422) from err
            await self._settle(page)
            await self._refresh(session_id)
            return await self._read(session_id, elements=True)

    async def scroll(self, session_id: str, direction: str) -> dict[str, Any]:
        session_id = validate_session_id(session_id)
        async with self._lock(session_id):
            page = self._active(session_id).page
            script = {"down": "window.scrollBy(0, Math.round(window.innerHeight * 0.85))",
                      "up": "window.scrollBy(0, -Math.round(window.innerHeight * 0.85))",
                      "top": "window.scrollTo(0, 0)",
                      "bottom": "window.scrollTo(0, document.documentElement.scrollHeight)"
                      }.get(direction)
            if script is None:
                raise BrowserError("INVALID_INPUT", "Scroll down, up, to the top or the bottom.")
            await page.evaluate(script)
            await asyncio.sleep(0.4)               # lazy content loads as it comes into view
            await self._refresh(session_id)
            return await self._read(session_id, elements=True)

    @staticmethod
    async def _settle(page: Any) -> None:
        """Give the page a moment to react to what was just done."""
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=5_000)
        except Exception:  # noqa: BLE001 - nothing was loading
            pass
        await asyncio.sleep(0.4)

    # --- shutting down ----------------------------------------------------------------

    async def close(self) -> None:
        if self._sweeper:
            self._sweeper.cancel()
        for session_id in list(self._running):
            try:
                await self.close_session(session_id)
            except Exception:  # noqa: BLE001
                logger.debug("could not close %s cleanly", session_id, exc_info=True)
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception:  # noqa: BLE001
                pass
            self._playwright = None
        await self._proxy.close()


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value == value \
        and abs(value) != float("inf")


def _why_navigation_failed(err: Exception) -> str:
    text = str(err)
    if "ERR_NAME_NOT_RESOLVED" in text or "ERR_NAME_RESOLUTION_FAILED" in text:
        return "That address could not be found. Check the spelling, or whether the site is down."
    if "Timeout" in text:
        return "The page took too long to load."
    if "ERR_TUNNEL_CONNECTION_FAILED" in text or "ERR_PROXY" in text:
        return "That page could not be reached, or it sends the browser somewhere that is off limits."
    return "The page could not be loaded. It may be unreachable or contain a blocked destination."


def _why_action_failed(err: Exception, selector: str) -> str:
    text = str(err)
    if "Timeout" in text:
        return (f"I couldn't find anything on the page matching {selector!r} that I could use. "
                "Read the page again to see what is there now.")
    if "not a valid selector" in text or "Unexpected token" in text or "SyntaxError" in text:
        return f"{selector!r} isn't a selector the page understands. Use one from the page's list."
    return f"That didn't work on the page ({text.splitlines()[0][:200] if text else 'unknown'})."

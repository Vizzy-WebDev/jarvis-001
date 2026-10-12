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
import contextlib
import json
import logging
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from ..childenv import scrubbed_environment
from . import downloads as downloads_store
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
STATE_SAVE_EVERY_S = 3.0
#: After an action, how long to wait for a page load it started before reporting. Longer than
#: this and the result says the page is still loading, which is true and useful, rather than
#: holding up the whole task for a slow server.
SETTLE_MAX_S = 6.0
#: Reading a page whose next page is still arriving can hang until that page does.
READ_TIMEOUT_S = 6.0
LOADING_READ_S = 1.5
#: How long Close and the person's own input wait behind an action the agent is in the middle of.
HOLD_PATIENCE_S = 3.0
#: A picture older than this is not shown in place of a fresh one.
STALE_SHOT_S = 45.0
MAX_TYPED_CHARS = 10_000

_KEYS = re.compile(r"^(Enter|Tab|Escape|Backspace|Delete|ArrowUp|ArrowDown|ArrowLeft|ArrowRight|"
                   r"Home|End|PageUp|PageDown|Control\+a|Meta\+a|Shift\+Tab)$")

#: Runs inside the page. A fixed script: callers cannot pass their own.
_READ_SCRIPT = """() => {
  const text = document.body ? document.body.innerText : '';
  return {url: location.href, title: document.title.slice(0, 300), ready: document.readyState,
          text: text.slice(0, %d), truncated: text.length > %d,
          scroll: {y: Math.round(window.scrollY), height: document.documentElement.scrollHeight,
                   viewport: window.innerHeight},
          frames: Array.prototype.filter.call(document.querySelectorAll('iframe'), function (f) {
            const r = f.getBoundingClientRect(); return r.width > 40 && r.height > 40;
          }).slice(0, 5).map(function (f) { return (f.src || f.name || 'a frame on this page').slice(0, 120); })};
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
    #: When the sign-ins were last written down (monotonic seconds).
    state_saved: float = 0.0
    #: Page loads the window has started and not finished (main-frame navigations in flight).
    navigating: dict = field(default_factory=dict)
    #: When the last navigation turned out to be a file download, and to what address.
    download_started: tuple | None = None
    #: The last picture taken of the page, and when: shown when a fresh one cannot be had because
    #: the page is busy arriving.
    last_shot: tuple | None = None
    #: Things the window did on its own that the next report should mention: a dialog it
    #: dismissed, a pop-up it closed. Told once, then forgotten.
    events: list = field(default_factory=list)
    #: Downloads still arriving; awaited before an agent's step reports, and at close.
    pending: set = field(default_factory=set)


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
                try:
                    # A transfer the last run was in the middle of becomes a recorded failure.
                    downloads_store.read_failures(entry, recover_interrupted=True)
                except (OSError, ValueError):
                    logger.debug("could not recover downloads for %s", entry.name, exc_info=True)

    # --- small helpers ----------------------------------------------------------------

    def directory(self, session_id: str) -> Path:
        return self.data_dir / validate_session_id(session_id)

    def _lock(self, session_id: str) -> asyncio.Lock:
        return self._locks.setdefault(session_id, asyncio.Lock())

    @contextlib.asynccontextmanager
    async def _hold(self, session_id: str, patience: float = HOLD_PATIENCE_S):
        """Take a window's turn, but not forever: closing it, and the person's own clicks and
        typing, must not sit behind an agent action that is waiting on a slow site. After
        `patience` they go ahead regardless (the action in progress then fails, which is what
        was asked for)."""
        lock = self._lock(session_id)
        held = False
        try:
            await asyncio.wait_for(lock.acquire(), patience)
            held = True
        except asyncio.TimeoutError:
            logger.info("window %s is busy; going ahead without its turn", session_id)
        try:
            yield
        finally:
            if held:
                lock.release()

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
        known = self._sessions.get(session_id)
        title = await self._title(running.page, known.title if known else "")
        session = Session(id=session_id, title=title, url=running.page.url, status="active",
                          updated_at=_now())
        self._sessions[session_id] = session
        self._persist(session)
        await self._save_state(session_id, running)
        return session

    @staticmethod
    async def _title(page: Any, fallback: str = "") -> str:
        """The page's title - or what was known, when the page is busy arriving and will not say."""
        try:
            return (await asyncio.wait_for(page.title(), READ_TIMEOUT_S))[:300]
        except Exception:  # noqa: BLE001
            return fallback[:300]

    async def _save_state(self, session_id: str, running: _Running, *, force: bool = False) -> None:
        """Write the window's sign-ins down, so they outlive a hard exit.

        A browser keeps session cookies - what a login with no "remember me" is made of - only
        in memory, so a Jarvis that is killed (a closed console window) would forget every one.
        They are written after what the window just did, at most every few seconds; closing the
        window writes them too. Best effort: it never fails the step that caused it."""
        now = time.monotonic()
        if not force and now - running.state_saved < STATE_SAVE_EVERY_S:
            return
        running.state_saved = now
        try:
            path = self.directory(session_id) / "storage.json"
            temp = path.with_suffix(".json.tmp")
            await running.context.storage_state(path=str(temp))
            os.replace(temp, path)
        except Exception:  # noqa: BLE001 - saving cookies is never worth failing a step over
            logger.debug("could not save sign-ins for %s", session_id, exc_info=True)

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

    async def create_session(self, session_id: str, url: str, *,
                             keep_on_failure: bool = False) -> dict[str, str]:
        """Open (or reopen) a window at `url`.

        If the window starts but the FIRST page will not load, `keep_on_failure` decides what
        is left: a window the agent works in stays open on a blank page (it was just asked to
        go somewhere that failed, and its next step is to try something else - a window that
        vanished made that step answer "no page is open"); a brand new one nobody has an id
        for yet is removed, as OpenMuse does, so it cannot pile up out of sight."""
        session_id = validate_session_id(session_id)
        async with self._create_lock:
            async with self._lock(session_id):
                return (await self._create(session_id, url, keep_on_failure)).as_dict()

    async def _create(self, session_id: str, url: str, keep_on_failure: bool = False) -> Session:
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
        await self._clear_dead(session_id)
        previous = self._sessions.get(session_id)
        folder = self.directory(session_id)
        profile = folder / "profile"
        temp_dir = Path(tempfile.gettempdir()) / f"jarvis-browser-downloads-{session_id}"
        profile.mkdir(parents=True, exist_ok=True)
        temp_dir.mkdir(parents=True, exist_ok=True)
        playwright = await self._playwright_handle()
        _prime_profile(profile)
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
            # The new tab BEFORE the old one goes: closing a browser's last tab ends its session
            # and drops every session cookie - exactly what an ordinary sign-in is made of -
            # including the ones just restored above.
            page = await context.new_page()
            for old in list(context.pages):
                if old is not page:
                    await old.close()
            page.set_default_timeout(ACTION_TIMEOUT_MS)
            await self._normalise_user_agent(context, page)
            running = _Running(context=context, page=page, touched=time.monotonic(),
                               temp_dir=temp_dir)
            self._running[session_id] = running
            context.on("page", lambda popup: asyncio.ensure_future(
                self._block_popup(running, popup)))
            page.on("dialog", lambda dialog: asyncio.ensure_future(
                self._dismiss(running, dialog)))
            page.on("download", lambda download: self._on_download(session_id, running, download))
            self._track_navigation(page, running)
            initial = Session(id=session_id, title=previous.title if previous else "New session",
                              url=url, status="active", updated_at=_now())
            self._sessions[session_id] = initial
            self._persist(initial)
            try:
                return await self._navigate(session_id, url)
            except BrowserError as err:
                if keep_on_failure:
                    await self._refresh_quietly(session_id)
                    raise _Kept(err) from None
                raise _Abandon(err) from None
        except _Kept as kept:
            raise kept.reason from None             # the window stays; only the page failed
        except _Abandon as abandon:
            await self._abandon(session_id, context, previous, folder, temp_dir, url)
            raise abandon.reason from None
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

    async def _clear_dead(self, session_id: str) -> None:
        """Let go of a window whose tab has died, before a new one is opened on its profile.

        A tab can die under a window (a crash, a browser killed under memory pressure) and
        leave the browser process holding the profile; a second launch on it is refused, and
        the window could not be opened again until Jarvis was restarted. Found by making a
        tab die and opening the window again."""
        dead = self._running.pop(session_id, None)
        if dead is None:
            return
        try:
            await asyncio.wait_for(self._discard(dead.context), 15)
        except Exception:  # noqa: BLE001 - a browser that will not close is abandoned
            logger.info("a dead browser window would not close cleanly", exc_info=True)
        if dead.temp_dir:
            shutil.rmtree(dead.temp_dir, ignore_errors=True)

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
    def _note(running: "_Running", event: dict[str, str]) -> None:
        running.events.append(event)
        del running.events[:-10]                    # a page that spams dialogs is not a log

    async def _block_popup(self, running: "_Running", popup: Any) -> None:
        """Close a window a page opened, and remember that it did, and where it was going.

        One window is all there is to work in (see `connectors/browser_connector.py`), so a
        pop-up cannot be used; but a page that "opened a pop-up" that is not there is something
        the person and the model both need to be told, not to discover."""
        address = ""
        try:
            await popup.wait_for_url("**/*", wait_until="commit", timeout=1_500)
            address = popup.url
        except Exception:  # noqa: BLE001 - it may never navigate; the address is a courtesy
            address = popup.url if popup.url != "about:blank" else ""
        self._note(running, {"kind": "popup", "address": address[:200]})
        try:
            await popup.close()
        except Exception:  # noqa: BLE001
            pass

    async def _dismiss(self, running: "_Running", dialog: Any) -> None:
        """Answer a dialog the only safe way - no - and remember what it said.

        An alert is simply closed; a confirm is answered No and a prompt cancelled, so nothing
        a page asks "are you sure?" about is ever agreed to on the person's behalf."""
        self._note(running, {"kind": "dialog", "type": dialog.type,
                             "message": (dialog.message or "")[:200]})
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
        running = self._active(session_id)
        page = running.page
        before = page.url
        running.download_started = None
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
            if "Download is starting" in str(err):
                # A file download aborts the navigation on purpose; the page stays where it was.
                running.download_started = (target.href, time.monotonic())
            elif _is_timeout(err) and page.url not in (before, "about:blank", ""):
                # The server answered and the page began to arrive, but not all of it did in time.
                # A person would see a half-drawn page and carry on; so does this (the result
                # says it is still loading), instead of throwing away a page that is there.
                await self._validate(page.url)
            else:
                raise BrowserError("NAVIGATION_FAILED", _why_navigation_failed(err), 502) from err
        return await self._refresh(session_id)

    async def _refresh_quietly(self, session_id: str) -> None:
        try:
            await self._refresh(session_id)
        except Exception:  # noqa: BLE001 - recording where a failed window is must not hide the failure
            logger.debug("could not refresh a window after a failed first load", exc_info=True)

    async def _abandon(self, session_id: str, context: Any, previous: "Session | None",
                       folder: Path, temp_dir: Path, url: str) -> None:
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

    @staticmethod
    def _track_navigation(page: Any, running: "_Running") -> None:
        """Remember which page loads are still under way, so a result can say "still loading"
        and an action can tell a slow page from a click that did nothing."""
        def started(request: Any) -> None:
            try:
                if request.is_navigation_request() and request.frame == page.main_frame:
                    running.navigating[request] = time.monotonic()
            except Exception:  # noqa: BLE001 - a request with no frame (a worker's) is not a page load
                pass

        def ended(request: Any) -> None:
            running.navigating.pop(request, None)

        page.on("request", started)
        page.on("requestfinished", ended)
        page.on("requestfailed", ended)

    @staticmethod
    def _loading(running: "_Running") -> bool:
        cutoff = time.monotonic() - 90          # a load nobody ever finished is not still loading
        for request, since in list(running.navigating.items()):
            if since < cutoff:
                running.navigating.pop(request, None)
        return bool(running.navigating)

    @staticmethod
    async def _blank(page: Any) -> None:
        try:
            await page.goto("about:blank", timeout=5_000)
        except Exception:  # noqa: BLE001
            pass

    async def close_session(self, session_id: str) -> dict[str, str]:
        session_id = validate_session_id(session_id)
        async with self._hold(session_id):
            return (await self._close(session_id)).as_dict()

    async def _close(self, session_id: str) -> Session:
        stored = self._sessions.get(session_id)
        if stored is None:
            raise BrowserError("SESSION_NOT_FOUND", "Browser session not found.", 404)
        running = self._running.get(session_id)
        if running is not None:
            # Each step is given a few seconds, not forever: a window being closed because the
            # page it is waiting on never arrives must still close.
            try:
                await asyncio.wait_for(self._save_state(session_id, running, force=True), 3)
            except Exception:  # noqa: BLE001 - what was written at the last step is on disk
                logger.info("could not save sign-ins while closing %s", session_id)
            await self._finish_downloads(running, 10)
            try:
                await asyncio.wait_for(self._discard(running.context), 8)
            except Exception:  # noqa: BLE001 - a browser that will not close is let go of
                logger.info("the browser for %s did not close in time", session_id)
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
        """A picture of the window as it is now.

        Deliberately NOT queued behind what the agent is doing. It used to be, and while the
        agent waited on a slow page (tens of seconds is ordinary) the person's live view
        stalled, gave up and showed "Disconnected", and the requests piled up to arrive in a
        burst. Taking a picture does not disturb a page that is loading. If the page is too busy
        to be photographed this moment, the last picture is shown instead of nothing."""
        session_id = validate_session_id(session_id)
        running = self._active(session_id)
        try:
            shot = await asyncio.wait_for(running.page.screenshot(type="png", timeout=2_500), 3.5)
            running.last_shot = (shot, time.monotonic())
            return shot
        except Exception:  # noqa: BLE001 - busy, mid-navigation, or closing
            if running.last_shot and time.monotonic() - running.last_shot[1] < STALE_SHOT_S:
                return running.last_shot[0]
            raise BrowserError("BUSY", "The page is busy loading; try the picture again in a "
                                       "moment.", 503) from None

    async def read(self, session_id: str, *, elements: bool = False) -> dict[str, Any]:
        session_id = validate_session_id(session_id)
        async with self._lock(session_id):
            return await self._read(session_id, elements=elements)

    async def _read(self, session_id: str, *, elements: bool = False) -> dict[str, Any]:
        running = self._active(session_id)
        page = running.page
        if elements:
            # An agent's step reports what it caused, and a download is something a click
            # can cause: give a transfer that has started a moment to land.
            await self._finish_downloads(running, 10)
        if page.url != "about:blank":
            await self._validate(page.url)
        # The script is fixed by this module; nothing a caller sends is ever evaluated.
        unreadable = False
        # While the next page is still being fetched the browser holds a read until it arrives,
        # so a read during a load is only given a moment: "still loading" is the honest answer.
        patience = LOADING_READ_S if self._loading(running) else READ_TIMEOUT_S
        try:
            result = await asyncio.wait_for(self._evaluate_settled(page, _READ_SCRIPT), patience)
        except asyncio.TimeoutError:
            # The page cannot be read while its replacement is on the way. Say where it is and
            # that it is still loading; the next look will have the page.
            known = self._sessions.get(session_id)
            result = {"url": page.url, "title": known.title if known else "", "text": "",
                      "truncated": False, "scroll": {"y": 0, "height": 0, "viewport": 0}}
            unreadable = True
        if result["url"] != "about:blank":
            await self._validate(result["url"])
        # `readyState` is the page's own word for it: a page whose head has arrived and whose
        # body has not reads as empty, and an empty page that is merely unfinished must not be
        # mistaken for a page that has nothing on it.
        result["loading"] = unreadable or self._loading(running) or result.get("ready") == "loading"
        result.pop("ready", None)
        if running.events:
            result["events"], running.events[:] = list(running.events), []
        if running.download_started and time.monotonic() - running.download_started[1] < 60:
            result["startedDownload"] = running.download_started[0]
        if elements:
            try:
                result["elements"] = [] if unreadable else await asyncio.wait_for(
                    self._evaluate_settled(page, _ELEMENTS_SCRIPT), patience)
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
        async with self._hold(session_id):
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
            running = self._active(session_id)
            page = running.page
            started = time.monotonic()
            try:
                # A link that wants a new tab would open a popup, which is dismissed;
                # make it open in this one instead.
                await page.locator(selector).first.evaluate(
                    "el => { const a = el.closest('a'); "
                    "if (a && a.target && a.target !== '_self') a.target = '_self'; }",
                    timeout=ACTION_TIMEOUT_MS)
                await self._perform(running, page.click(selector, timeout=ACTION_TIMEOUT_MS),
                                    started)
            except BrowserError:
                raise
            except Exception as err:  # noqa: BLE001
                # The click itself can be done and the PAGE it started still be arriving: the
                # wait for that is what timed out. Reporting "couldn't find it" for a click that
                # worked sent a real task round in circles. Only a failure BEFORE the click is a
                # failure.
                if not _action_was_performed(err):
                    raise BrowserError("ACTION_FAILED", _why_action_failed(err, selector),
                                       422) from err
            await self._settle(page, running, started)
            await self._refresh(session_id)
            return await self._read(session_id, elements=True)

    async def type_into(self, session_id: str, selector: str, text: str,
                        submit: bool = False) -> dict[str, Any]:
        session_id = validate_session_id(session_id)
        if len(text) > MAX_TYPED_CHARS:
            raise BrowserError("INVALID_INPUT", "That is too much text to type at once.")
        async with self._lock(session_id):
            running = self._active(session_id)
            page = running.page
            started = time.monotonic()
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
                        await self._perform(running, page.press(selector, "Enter",
                                                                timeout=ACTION_TIMEOUT_MS), started)
            except Exception as err:  # noqa: BLE001
                if not _action_was_performed(err):
                    raise BrowserError("ACTION_FAILED", _why_action_failed(err, selector),
                                       422) from err
            await self._settle(page, running, started)
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

    async def _perform(self, running: "_Running", action: Any, started: float) -> None:
        """Run one Playwright action, without being held hostage by the page it starts.

        A click or key press is not finished, to Playwright, until the page load it began
        has finished too - so a slow site made the action "time out" after it had worked, and
        its option to stop waiting is deprecated and ignored. This waits for the action for a
        fair while; if it is still going after that, and a page load is in flight, the action
        was evidently performed (that load is what it is waiting for): report on. Anything else
        still failing is a real failure, and is raised."""
        task = asyncio.ensure_future(action)
        patience = started + SETTLE_MAX_S
        while not task.done():
            await asyncio.wait({task}, timeout=0.25)
            if not task.done() and time.monotonic() > patience and self._loading(running):
                task.add_done_callback(lambda finished: finished.cancelled() or finished.exception())
                return
        task.result()

    async def _settle(self, page: Any, running: "_Running", started: float) -> None:
        """Give the page a moment to react to what was just done - and, if the action started a
        page load, a fair wait for it, but not an unlimited one. `started` is when the action
        began: the wait for the action itself and this one share ONE budget, so a slow site
        costs the task `SETTLE_MAX_S`, not twice that."""
        await asyncio.sleep(0.4)                 # a load the action started registers just after
        deadline = started + SETTLE_MAX_S
        while self._loading(running) and time.monotonic() < deadline:
            await asyncio.sleep(0.25)
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=3_000)
        except Exception:  # noqa: BLE001 - nothing was loading, or it is still going
            pass

    @staticmethod
    async def _evaluate_settled(page: Any, script: str) -> Any:
        """Run a fixed script, again if the page changed under it.

        A page that is navigating destroys the context a script runs in, and the failure reads
        as "the browser ran into a problem". The second look, after the new page has begun,
        is nearly always fine."""
        last: Exception | None = None
        for _ in range(3):
            try:
                return await page.evaluate(script)
            except Exception as err:  # noqa: BLE001
                last = err
                if not _is_page_changing(err):
                    raise
                try:
                    await page.wait_for_load_state("domcontentloaded", timeout=4_000)
                except Exception:  # noqa: BLE001
                    await asyncio.sleep(0.5)
        raise last  # type: ignore[misc]

    # --- downloads --------------------------------------------------------------------

    def _on_download(self, session_id: str, running: _Running, download: Any) -> None:
        directory = self.directory(session_id)
        held = len(downloads_store.list_downloads(directory)) + len(running.pending)
        task = asyncio.ensure_future(downloads_store.capture_pdf_download(
            directory=directory, temp_dir=running.temp_dir or Path(tempfile.gettempdir()),
            download=download, limit_reached=held >= downloads_store.MAX_PER_SESSION))
        running.pending.add(task)
        task.add_done_callback(running.pending.discard)

    @staticmethod
    async def _finish_downloads(running: _Running, timeout: float) -> None:
        if running.pending:
            await asyncio.wait(set(running.pending), timeout=timeout)

    def downloads(self, session_id: str) -> dict[str, Any]:
        """The PDFs this window has caught, and the recent ones it refused."""
        session_id = validate_session_id(session_id)
        if session_id not in self._sessions:
            raise BrowserError("SESSION_NOT_FOUND", "Browser session not found.", 404)
        directory = self.directory(session_id)
        imported = self._imported(directory)
        return {"downloads": [d for d in downloads_store.list_downloads(directory)
                              if d["id"] not in imported],
                "imported": imported,
                "failures": downloads_store.read_failures(directory)}

    def download_file(self, session_id: str, download_id: str) -> Path:
        session_id = validate_session_id(session_id)
        path = downloads_store.download_path(self.directory(session_id), download_id)
        if path is None:
            raise BrowserError("DOWNLOAD_NOT_FOUND", "That download is no longer there.", 404)
        return path

    @staticmethod
    def _imported(directory: Path) -> dict[str, str]:
        try:
            found = json.loads((directory / "imported.json").read_text("utf-8"))
        except (OSError, ValueError):
            return {}
        return found if isinstance(found, dict) else {}

    def mark_imported(self, session_id: str, download_id: str, artifact_id: str) -> None:
        """Remember that a download became an artifact, so asking again returns the same one."""
        directory = self.directory(session_id)
        known = self._imported(directory)
        known[download_id] = artifact_id
        temp = directory / "imported.json.tmp"
        temp.write_text(json.dumps(known), "utf-8")
        os.replace(temp, directory / "imported.json")

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
            # The driver's pipes close on the loop's next turns; stopping the loop at once
            # leaves them to be collected after it is gone (a noisy, harmless warning).
            await asyncio.sleep(0.25)
        await self._proxy.close()


def _prime_profile(profile: Path) -> None:
    """Make a PDF a site serves inline arrive as a download instead.

    Chrome and Edge show a PDF in a built-in viewer, which a page read sees as an empty
    document and which leaves nothing to keep - and most real invoice and report links
    are served that way. This setting, which both browsers honour, makes every PDF a
    download, so it takes the same road as one a site marked as an attachment
    (`downloads.py`). Written into the profile before the browser starts; anything else
    already in it is left alone.
    """
    path = profile / "Default" / "Preferences"
    try:
        found = json.loads(path.read_text("utf-8")) if path.exists() else {}
        if not isinstance(found, dict):
            found = {}
        plugins = found.setdefault("plugins", {})
        if plugins.get("always_open_pdf_externally") is True:
            return
        plugins["always_open_pdf_externally"] = True
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name("Preferences.tmp")
        temp.write_text(json.dumps(found), "utf-8")
        os.replace(temp, path)
    except (OSError, ValueError):
        logger.info("could not set the PDF preference in the profile", exc_info=True)


class _Kept(Exception):
    """Internal: the first page failed, but the window it failed in is to be kept."""

    def __init__(self, reason: BrowserError) -> None:
        super().__init__(reason.message)
        self.reason = reason


class _Abandon(Exception):
    """Internal: the first page of a window nobody can name yet would not load; remove it."""

    def __init__(self, reason: BrowserError) -> None:
        super().__init__(reason.message)
        self.reason = reason


def _is_timeout(err: Exception) -> bool:
    return type(err).__name__ == "TimeoutError" or "Timeout" in str(err)[:80]


def _is_page_changing(err: Exception) -> bool:
    text = str(err)
    return "Execution context was destroyed" in text or "navigation" in text.lower()


def _action_was_performed(err: Exception) -> bool:
    """Whether a failed click or key press had already happened when it failed.

    Playwright's own log says so: the actionability checks pass, the action is performed
    ("click action done"), and what is left to time out is the wait for any page load the
    action began. That is a slow page, not a failed action."""
    text = str(err)
    return _is_timeout(err) and ("click action done" in text
                                 or "waiting for scheduled navigations" in text
                                 or "navigations have finished" in text)


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

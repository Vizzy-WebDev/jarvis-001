"""Rendering a page that a plain fetch cannot read.

Some pages ship almost no HTML and build themselves in the browser. A plain
fetch of one returns a shell — technically a success, with nothing in it — and
the honest options are to say so or to actually run the page.

**Headless, always.** A visible window is a different thing entirely: it belongs
to the desktop work, where a person can watch it, and it should never open just
because a lookup was slightly harder than usual. Reading a page is invisible
work, and it stays that way.

Optional by design: if no browser is installed this reports that plainly rather
than failing in a way that reads like the page being broken.
"""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass

logger = logging.getLogger(__name__)

RENDER_TIMEOUT_MS = 20000
#: Long enough for a client-rendered page to draw, short enough not to hang a
#: turn on one that never settles.
SETTLE_TIMEOUT_MS = 5000


@dataclass(frozen=True)
class Rendered:
    ok: bool
    html: str = ""
    error: str | None = None


def browser_path() -> str | None:
    """Where a headless browser lives, or None.

    Checks the environment's own pre-installed location before looking on PATH:
    a packaged Chromium is the normal case here and is not on PATH.
    """
    packaged = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if packaged:
        candidate = os.path.join(packaged, "chromium")
        if os.path.exists(candidate):
            return candidate
    for name in ("chromium", "chromium-browser", "google-chrome", "msedge"):
        found = shutil.which(name)
        if found:
            return found
    return None


def is_available() -> bool:
    try:
        import playwright  # noqa: F401
    except ImportError:
        return False
    return browser_path() is not None


def render(url: str, *, timeout_ms: int = RENDER_TIMEOUT_MS) -> Rendered:
    """The page's HTML after its own scripts have run."""
    if not is_available():
        return Rendered(ok=False,
                        error="There's no browser installed here to render that page with.")

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return Rendered(ok=False, error="There's no browser available to render that page.")

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True,
                                                 executable_path=browser_path())
            try:
                page = browser.new_page()
                page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
                try:
                    page.wait_for_load_state("networkidle", timeout=SETTLE_TIMEOUT_MS)
                except Exception:  # noqa: BLE001 — a page that never settles still has content
                    logger.debug("%s never went idle; reading it as it stands", url)
                return Rendered(ok=True, html=page.content())
            finally:
                browser.close()
    except Exception as err:  # noqa: BLE001
        logger.info("could not render %s: %s", url, err)
        return Rendered(ok=False, error=f"That page couldn't be rendered: {err}")

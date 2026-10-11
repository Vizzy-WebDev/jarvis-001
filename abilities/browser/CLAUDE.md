# Jarvis's own browser (`abilities/browser/` = `jarvis.browser`)

A headless Chromium — the Chrome or Edge the person already has — with a profile of its own
under `data/browser-sessions/<uuid>/`, that Jarvis drives through his agent tools and the person
can watch and take over. **Ported from OpenMuse's browser (MIT, CopilotKit; see `NOTICE`)** and
kept structurally the same: `network.py` ↔ `apps/worker/src/network.ts`, `proxy.py` ↔
`proxy.ts`, `manager.py` ↔ `browser.ts`, `downloads.py` ↔ `downloads.ts`, `console.py` ↔
`apps/server/src/browser-console.ts`, `routes/browsers.py` ↔ the `/api/browsers/*` routes, and
`frontend/components/browser/BrowserCard.tsx` ↔ OpenMuse's browser card. When OpenMuse's side
changes something worth having, compare against those files.

What differs from OpenMuse, and why: it is a module in this process, not a token-protected
service (Jarvis is one process on one port, and `request_guard.py` is what the signed URLs were
for); it is Python, because running Jarvis needs only Python; the agent acts through the page's
own elements (selectors) as well as the person through coordinates; PDFs that a site serves
inline are turned into downloads.

## The rules that do not bend

- **Only public web addresses, ports 80/443, and no switch to allow anything else.** Jarvis's own
  API is on this computer's loopback, and a page — or a model a page has talked into something —
  must not reach it, the router, or the home network. Every destination is validated twice
  (`network.validate_public_url`, in the request route AND in the egress proxy), every DNS answer
  must be public, and the proxy connects to the exact address that was checked, never a second
  lookup (`test_browser_network.py` proves it with a name that answers differently the second
  time). **Tests inject a resolver and a connector (`tests/browser_site.py`); they never relax the
  check.** Do not add an environment variable that does.
- **Nothing a page says is an instruction.** Page text and element labels are the website's.
  Every agent result says so (`UNTRUSTED`), and a password field's value is never listed — nor
  used as its label (a real bug a test caught).
- **The browser gets `childenv.scrubbed_environment()`**, never Jarvis's own environment, which
  holds every API key.
- **Outside content is only ever served as a download.** The page's picture
  (`GET /api/browsers/{id}/preview`) has `Content-Disposition: attachment`, `nosniff` and a
  sandboxing CSP, and is fetched and drawn by the console and the chat card rather than linked.
  The console (`console.py`) is a fixed template; the remote page's title and address go in with
  `textContent` only.
- **Only a PDF is kept from a download**, by its first five bytes whatever it was called, within
  10 MiB and 20 per window. Anything else is *reported* (`downloadProblems`), never dropped silently.

## How it is put together

- **One event loop in a dedicated thread** (`host.py`) owns Playwright's async API, the egress proxy
  and every session. Callers — a tool running for the model, a route in FastAPI's thread pool —
  use `BrowserHost`'s synchronous methods, each of which hands one job across and waits.
  Nothing starts until something asks: importing, creating the app, listing sessions and
  `availability()` launch no thread, no proxy and no browser, which is why there is no `JARVIS_*`
  interlock (the only timer is the 30-minute idle close, and it exists only while a window does).
  `atexit` closes it. On Windows the loop is explicitly a `ProactorEventLoop` (subprocesses).
- **Sessions** (`manager.py`): UUID, profile directory, `session.json`, at most 3 running and 20
  saved, one lock per session so operations queue, idle windows close after 30 minutes and reopen
  signed in. Popups are closed and dialogs dismissed; service workers and WebSockets are off.
  A link that wants a new tab is opened in this one by the agent's `click`.
- **The agent's window** is one fixed session, `browser_connector.AGENT_SESSION_ID`, so its
  sign-ins survive restarts and every chat shows the same one. The person's "Take control" opens
  the same window.
- **Browser discovery** (`locate.py`): `JARVIS_BROWSER_PATH`, then the older `webrender.browser_path`
  (PATH only — it found nothing on Windows), then Chrome, Edge, Chromium in their usual places,
  then Playwright's own cache. **Jarvis never downloads a browser.** `playwright` is a normal
  dependency (`pyproject.toml`), used as a driver only; if it is missing or there is no browser,
  `availability()` says so in plain words.
- **Reading a page** returns its text (cut for the model: 20,000 characters on open/read, 6,000
  after an action), a capped list of clickable/typeable elements each with a selector, and the scroll
  position (`moreBelow`). Selectors are `#id`, `[name=…]`, or a short path, checked to be unique.
- **PDFs**: `_prime_profile` sets `plugins.always_open_pdf_externally` in the profile before launch.
  Without it Chrome shows an inline PDF in its viewer — an empty page to read, nothing to keep —
  and most real report and invoice links are served inline. Captured PDFs sit in the session's
  folder; `imports.py` is the one place they become artifacts (`keep(check=False)`: the store's
  own PDF check is strict about Jarvis's own writers and would throw away an ordinary download).
  Importing the same download twice returns the same artifact (`imported.json`).
- **A tool result names its window** with `ui_action: {type: "attachment", kind: "browser", …,
  sessionId, pageUrl}` (`orchestrator/pipeline.py::_one_attachment` carries those two fields). The
  chat shows one live card per window per reply (`Message.tsx` de-duplicates by session).

## Testing it

`tests/browser_site.py` is a small website on loopback plus a host that reaches it as
`http://site.test`: the real destination checks run against a resolver that says it is public, and
only the proxy's last connect is redirected to the fixture. Real Chrome or Edge, real proxy.

- `test_browser_network.py` (guard + proxy, no browser), `test_browser_manager.py`,
  `test_browser_connector.py` (the agent's side, incl. a whole multi-step task),
  `test_browser_downloads.py`, `test_routes_browsers.py`, `test_browser_e2e.py` (the built front
  end in a real browser; set `JARVIS_FRONTEND_DIR` to a preview build).
- An e2e test must reset the tool registry per test (`assembly.reset_for_tests()` before the
  server starts): it remembers which connector record each tool belongs to, and that record was in
  the previous test's database.
- The console page has a strict CSP without `unsafe-eval`, which Playwright's
  `wait_for_function` needs — poll with `page.evaluate` instead.
- "open …" in a chat message can be taken by the router's fast path as an app-launch intent and
  never reach the model; phrase e2e prompts otherwise.
- What cannot be verified here: a real site's behaviour (logins, CAPTCHAs, consent walls, sites that
  need WebSockets or service workers, which are off). Real HTTPS through the CONNECT tunnel was
  checked against `example.com`, but is not in the suite, which has no internet dependency.

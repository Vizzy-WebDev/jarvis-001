# App Control connectors (`server/connectors/*.js`)

**This whole area was fully rebuilt** (not patched), including the MCP transport itself
in a second pass after the first left it as untouched legacy code — see "Standing
permission vs. runtime confirmation" below for why and what changed. Everything in this
section describes the current, rebuilt state.

A connector is one saved way to reach something beyond raw desktop control:
`data/connectors.json`, `{id, type, label, enabled, config, status}`. `browser`
and `files` are **singletons** (`getOrCreateSingleton()` — there's one "your
browser settings", not a list of them) — Jarvis's own built-in abilities, not
user-added connectors, no App Control card. `mcp`, `api`, and `cli` are the
three **peer, user-facing mechanisms** — the ONLY kinds a user ever adds
themselves, either from the **Official Connectors** directory (bundled,
`connectors/catalog.json` + `get-catalog.js` — every entry already knows
which mechanism it needs, so browsing never asks) or as a **Custom
Connector** (the one place a user picks a mechanism directly — App
Control's "Add custom connector" form asks MCP server URL / API base
address + key / CLI command, matching whichever they chose). None of the
three is privileged as "the real one" with the others bolted on:
`connectors/index.js`'s `rawToolsFor()` reads each mechanism's own client
module through the same interface, and every tool — regardless of
mechanism — goes through the exact same standing-permission filter and
`classifyToolRisk()` risk check before it ever reaches a model.

Every enabled connector's tools merge into `skills/index.js`'s existing
`getToolDeclarations()`/`listSkills()`/`runSkill()` as a **third** source alongside
built-in code skills and playbooks (`connectors/index.js`'s `getToolDeclarations()`,
wrapped by `skills/index.js`'s `connectorSkills()`) — a connector tool is
indistinguishable from any other skill to the model.

- **`files.js`** — read/write/list/move, every path resolved to absolute and checked
  against the allowlist BEFORE any `fs` call (never trust a relative path or a `..`
  segment). Tool names are NOT prefixed (`list_files`, `read_file`, ... — a singleton
  connector can't collide with another one of the same type).
- **`browser.js`** — drives a real Chrome/Edge window over CDP via a plain WebSocket
  (the already-installed `ws`), its own dedicated profile dir + fixed debug port 9333,
  entirely separate from the user's normal browser. Click/type/read all go through
  `Runtime.evaluate` running a small script in the page (see the Gotchas section below
  for why the Input domain's raw event dispatch isn't used) rather than
  driving it with the desktop mouse — reading a page's real text/DOM beats a screenshot
  guess. Tool names also unprefixed (`browser_navigate`, `browser_click`, ...).
- **`mcp-client.js`** — the **local/stdio** MCP transport: hand-written JSON-RPC 2.0
  over a spawned process's stdin/stdout (not the `@modelcontextprotocol/sdk` — see the
  no-new-dependencies rule), one persistent process per connector (same
  request/response id-matching shape as `control/ps-bridge.js`). `initialize` →
  `notifications/initialized` → `tools/list` → `tools/call`. **Hidden internal
  plumbing only, never a user-facing choice** — Jarvis runs locally, so a locally-
  spawned server is a real capability an Official Connector's `connectFlow` can use
  internally (`{kind: 'stdio', command, args, env}`), but the user only ever sees a
  name and a Connect button, never "this one happens to run as a local process."
- **`mcp-remote-client.js`** — the **remote** MCP transport real hosted servers speak
  (Notion, GitHub, Slack, Stripe, ...): Streamable HTTP, every JSON-RPC call a POST to
  one URL carrying a bearer token and an `Mcp-Session-Id` once assigned. A response may
  come back as plain `application/json` OR `text/event-stream` (SSE) — a server MAY
  upgrade to streaming even for a single request/response, so both are parsed. Plain
  `fetch()`, no new dependency.
- **`api-client.js`** — the **API key** mechanism. `discoverFromSpec(specUrl)` parses a
  real OpenAPI/Swagger JSON document's `paths` into one proposed operation per
  method+path, for the user to tick before anything saves (same "propose, user
  confirms" shape as CLI discovery below); `dispatch()` runs a saved operation with
  plain `fetch()`, injecting the key server-side (`config.js`'s `getSecret()`) so it
  never reaches the model. See the two real bugs this surfaced in the gotchas below —
  both found by testing against real, live public APIs, not by reasoning about the code.
- **`cli-client.js`** — the **CLI** mechanism. `discoverCommands(command)` runs the
  real `<command> --help` and asks a model to propose a subcommand list
  (`{name, description, argv, args}`) for the user to review/tick — the model never
  invents an executable command, only proposes data. `dispatch()` substitutes each
  `{placeholder}` in a saved `argv` template with exactly the matching supplied value,
  one argv entry each, and runs via `spawn(command, argv, {shell: false})` — **never a
  raw shell string, anywhere** — same discipline as `skills/open_app.js`'s allowlist;
  nothing about a call can inject an extra flag or chain a second command.
- **`oauth.js`** — OAuth 2.1 + PKCE client, the auth mechanism behind every remote `mcp`
  connector (official or custom). First runs `probeAuthorization()` — a real
  unauthenticated `initialize` + `tools/list` against the server itself — since some
  servers (Google's Gmail/Drive MCP servers, confirmed live) answer the handshake
  anonymously and only gate real tool calls; a server that needs nothing at all is saved
  as `connectFlow.kind: 'none'` and connects with no browser tab. Otherwise discovers the
  server's OAuth setup (RFC 9728 Protected Resource Metadata → RFC 8414 Authorization
  Server Metadata, both path-aware before falling back to a bare-origin guess — see the
  RFC 9728/8414 compliance bug below), then obtains a client in the MCP spec's own
  priority order (`obtainClientCredentials()`): **1.** a pre-registered or previously
  manually-typed Client ID; **2.** a Client ID Metadata Document (`client-identity.js`;
  needs a real publicly-reachable address for this Jarvis, via `setPublicBaseUrl()` —
  absent that, this step is skipped entirely, not a regression); **3.** RFC 7591 Dynamic
  Client Registration when the server supports it (`token_endpoint_auth_method: 'none'`
  — a public client authenticated only by PKCE, no pre-shared secret needed at all —
  this is why Notion/GitHub/Slack/Stripe-style "Connect" buttons need nothing from the
  user); **4.** none of the above worked — returns `{needsManualClient: true, reason,
  message}` (`manualClientHint()`) rather than throwing, so a real, per-connector,
  service-worded explanation is persisted on `connectFlow.manualClient` and the connector
  is never left stuck. **As of the user's own final decision, nothing in the front-end
  ever shows a Client ID/Secret entry point at all — `manualClient`'s message is shown
  as plain status text only.** See "No Client ID/Secret UI anywhere" below for the full
  account (this regressed toward showing some form of it unconditionally three separate
  times before landing here). The redirect lands on Jarvis's OWN
  already-running server (`GET /api/connectors/oauth/callback`) — no separate temporary
  listener needed. Token sets are one `JSON.stringify`'d value under the connector's
  `config.secretRef`, through the existing `config.js` `saveSecret()`/`getSecret()` — no
  new secret store. `getAccessToken()` silently refreshes an expiring token; never a
  second Connect prompt for a still-valid connection.
- **`client-identity.js`** — the single source of truth for what Jarvis calls itself as
  an OAuth client: `redirectUri()`, the Client ID Metadata Document body
  (`clientMetadataDocument()`), and the DCR registration body derived from the same
  object (`clientRegistrationBody()`) — so the document an authorization server fetches
  and the body DCR posts can never drift apart. `cimdUrl()` returns null (CIMD silently
  skipped, DCR still runs) until `setPublicBaseUrl()` is configured — Jarvis binds to
  `127.0.0.1` only, so a remote authorization server can never fetch a document Jarvis
  serves locally without a real, publicly-reachable address in front of it.
- **`catalog-credentials.js`** — one Client ID/Secret **per catalog entry**, and
  shared by every connector that catalog entry ever creates for this install. **No UI
  calls this any more** (see "No Client ID/Secret UI anywhere" below) — the two modals
  that originally posted to its `register-client` route were deleted outright. The
  route stays reachable server-side, meant to be called directly (e.g. by Claude,
  given real credentials outside the app) if the user ever wants Gmail/Drive/GitHub/
  Slack actually connected. This exists specifically because Google/GitHub/Slack's
  real OAuth servers don't support automatic registration (verified live — no
  `registration_endpoint`), unlike Notion/Composio: SOME registration has to happen
  somewhere, and this is what makes it happen once per service instead of once per
  connector. It's the single-user-install equivalent of what a real product with a
  backend does — Claude's own Gmail/Drive connectors never ask because Anthropic
  registered one OAuth client centrally, invisibly, shared by every Claude user forever;
  a Jarvis install has no such backend, so the user plays that role for their own
  install, exactly once, instead of never. `server.js`'s `POST
  /api/connectors/catalog/:catalogId/ensure` checks this BEFORE creating a brand-new
  connector record and, if a credential is already registered, seeds the new
  connector's `connectFlow.clientId` and its own `connclient_<id>` secret at creation
  time — so `oauth.js` needs no awareness this happened at all; `existingFlow.clientId`
  in `startConnect()` is just already populated, and the connector goes straight to a
  real `authUrl` on its very first Connect click, same as Notion's DCR path. The Client
  ID lives in `data/catalog-credentials.json` (git-ignored, like everything under
  `data/`) — never in the bundled, hand-verified `catalog.json`, which stays read-only
  at runtime on purpose (see `get-catalog.js`'s own doc comment). The secret never
  touches that JSON file either — same `saveSecret()`/`getSecret()` every other
  credential in this codebase uses, under `catalogclient_<catalogId>`.
- **`get-catalog.js`** — the bundled **Official Connectors** directory. Every entry is
  `{id, label, icon, description, connectFlow}`; clicking Connect on any of them runs
  the exact same `oauth.js` flow a Custom Connector's own URL would, just pre-filled —
  **official connectors are just curated + pre-filled custom connectors**, same
  mechanism underneath, never a different code path. **Catalog honesty rule**: an entry
  is only added once its `connectFlow` has been tested end to end against the real
  service — never on the strength of "the transport code should work in theory."
  Notion, GitHub, Slack, Google Drive, and Gmail are the entries today for exactly that
  reason — **Figma was deliberately NOT added**: verified live (its own real docs page,
  fetched directly) that Figma's remote MCP server only accepts pre-approved catalog
  clients (VS Code, Cursor, Claude Code) — dynamic client registration returns 403 for
  anyone else, personal access tokens are rejected at the MCP endpoint, and the only way
  in for a new client is a waitlist. No guided-setup form can work around a server that
  refuses the client itself; revisit only if Figma's own policy changes.

`connectors/index.js` prefixes an `mcp`/`api`/`cli` connector's tool names as
`{sanitized-label}__{name}` to avoid collisions across multiple connectors (files and
browser skip this since they're singletons), and routes a call by reading
`connector.type`: `'api'` → `api-client.js`, `'cli'` → `cli-client.js`, `'mcp'` →
`connector.config.connectFlow.kind` (`'stdio'` → `mcp-client.js`, `'oauth_dcr'` /
`'oauth_guided'` → `mcp-remote-client.js`). `connectFlow`/mechanism is internal
bookkeeping the model never sees as a "type" — the UI shows it only once, as the App
Control tab a connector lives in and the one question "Add custom connector" asks.

### Standing permission vs. runtime confirmation — how the current design was reached

This area was fully rebuilt (not patched, three rounds in one session) after an original
3-way Always/Ask/Never permission dropdown was found to conflate two genuinely separate
requirements: **standing permission** ("is Jarvis allowed to use this tool at all") and
**runtime confirmation** ("does it pause to ask right now, regardless of standing
permission"). Along the way, a real risk-classifier bug surfaced: `classifyActionRisk()`
was scanning a tool's entire raw MCP description (real ones run to a couple thousand
characters with usage examples) with plain substring matching — "SharePoint" matched
"share", "in sidebar order" matched "order". Fixed with word-tokenized matching
(`guard.js`) plus truncating to the first sentence before it ever reaches the classifier
(`shortDescription()`). **Two more real bugs in this same area, found later, both in
`server/control/CLAUDE.md`'s Gotchas**: `classifyActionRisk()` was lowercasing an
identifier before ever checking its camelCase boundary (silently undoing the
word-tokenization fix above for any tool name shaped like `COMPOSIO_MULTI_EXECUTE_TOOL`
— it never actually tokenized into `[..., "execute", ...]`, yet was still landing on
'risky' regardless, via a different path); and `classifyToolRisk()` (below) now trusts a
tool's real description over its bare name once one exists, specifically because a
generic gateway/dispatcher tool (Composio's own `COMPOSIO_MULTI_EXECUTE_TOOL` is the
confirmed live example) can have a risky-sounding word baked into its own name with no
relation to what any given call actually does — the description is the more honest
signal when there's a real one to read. The rebuild produced `api-client.js`/`cli-client.js` as real,
working mechanisms (not just permission-model theory) and the standing permission
(Allowed/Not-allowed toggle, see "Connector detail pages" below) fully separated from
runtime confirmation (automatic, `classifyToolRisk()`, no per-tool override at all). Full
three-round account: handoff-archive.md's "connector/permissions full rebuild" session entry.
`order` was tried as a risky keyword and dropped — it matched "in sidebar order"
(sequence) as often as "place an order" (purchase); `purchase`/`buy`/`checkout`
already cover the money-spending case without that ambiguity, and once something is
risky it can no longer be silenced with the standing-permission toggle, so a bad
keyword here is a permanent annoyance, not a one-time one. Re-verified against a real
27-tool Notion connector: 4 tools classify risky (`update-page`, `move-pages`,
`update-data-source`, `update-view`) — all genuine "changes something" actions.

### Desktop control / Browser / Files have no settings screen

`public/screens/app-control.js` (nav label: **App Control** — renamed from
"Connectors" partway through the App Control/Skills/Monitoring/Sandbox upgrade,
once MCP became one of three mechanisms rather than the only one) shows only the
Connectors card now — no Desktop control, Browser, or Files cards. These three are Jarvis's own
built-in abilities, not things the user "adds" or configures, the same reasoning
already applied to the Skills screen (see the root `CLAUDE.md`'s "Jarvis's built-in
abilities... must never appear in the Skills screen") — it just took a direct question
from the user ("shouldn't these be Jarvis's own capability, not something on the
interface?") to notice it hadn't been applied here too. What used to live on those
cards:

- **The safety blocklist, autonomy, and screenshot retention** (`control/safety.js`'s
  `DEFAULTS`) are now **fixed, non-editable defaults** — no screen, no API write path
  (`GET`/`POST /api/safety` were removed; `getSafetyConfig()` is still called
  in-process by `session.js`/`screenshot-store.js`/`guard.js`, only its HTTP exposure
  is gone). The user's explicit choice: these are "sensible hardcoded defaults," not
  something worth a settings UI.
- **The Files folder allowlist** has no form either — it starts empty and grows only
  through conversation. `server/skills/allow_folder.js` is the only way it changes:
  `confirm: 'always'` (reusing the existing read-back-and-confirm gate in
  `skills/index.js`, the same mechanism `remember_about_me` uses, rather than trusting
  the model's own judgment about what counts as a real "yes"), with its own small
  hardcoded denylist (`C:\Windows`, `C:\Program Files`, `C:\Program Files (x86)`) that
  can never be granted regardless of what's asked — the same "sensible hardcoded floor"
  idea as the blocklist above, applied to the filesystem. `prompt.js` tells the model to
  ask the user by name for the specific folder it needs before ever calling this. Meta
  skill (`meta: true`) — granting new access only makes sense in a live conversation,
  never during an unattended scheduled task.
- **Browser** loses its on/off toggle and Test button — it's simply always available,
  same as Desktop control already was (that card never had an enable/disable control
  either, only settings).

**A real startup-ordering bug this surfaced**: `getOrCreateSingleton('files', ...)` /
`getOrCreateSingleton('browser', ...)` used to only ever run as a side effect of the
now-removed cards' `GET /api/connectors/files` / `GET /api/connectors/browser` fetching
them on page load. A fresh install that never visited that page would never have either
connector record, and `connectors/index.js`'s `getToolDeclarations()` would silently
never find one to register `list_files`/`browser_navigate`/etc. from — losing both
abilities entirely for a new user, invisibly. Fixed by seeding both singleton records
once at server startup (`server.js`, right after `startScheduler()`), unconditionally,
not lazily from a UI visit that no longer exists.

### Connector detail pages, per-tool permissions, and the guided-setup flow

Two separate surfaces, kept deliberately apart after an implementation slip got
corrected mid-build: the **Connectors** card on the main page lists only what's
already added (connected or mid-setup); **"Browse connectors"** (one of "+ Add"'s
two options, alongside "Add custom connector") is the full Official Connectors
directory with its own search — looking through everything never clutters the main
list. Clicking any row, from either surface, opens the same connector detail page
(`public/screens/_connector-detail.js`) — not a hash route; `app-control.js` swaps
its own container's content internally between its list view(s) and this view,
same "wipe and rebuild" pattern every screen uses, just at a finer grain within one
section.

**Per-tool permissions — two genuinely separate things, not one dropdown.**
Rebuilt after the user re-read their own original spec and pointed out it
described two different paragraphs ("Connector/tool permissions" vs.
"Permission and safety... regardless of what standing permissions I've
configured") that an earlier version had conflated into a single 3-way
Always/Ask/Never control:

- **Standing permission** (`config.toolPermissions: {[toolName]: 'blocked'}`,
  set from the detail page's per-tool **Allowed/Not allowed toggle**) — is
  Jarvis allowed to use this tool AT ALL. A key's absence means allowed (the
  default for every newly-discovered tool). `'blocked'` filters the tool out
  of `getToolDeclarations()`'s model-facing list completely — though it still
  shows on the detail page, toggle included, so the user can change their
  mind. A legacy `'never'` value reads the same as `'blocked'`; legacy
  `'always'`/`'ask'` values simply read as allowed — that distinction no
  longer exists as a concept, and there was no real saved data to migrate
  (checked the user's actual `data/connectors.json` before removing it).
- **Runtime confirmation** — whether using an *allowed* tool pauses to
  confirm right now. Purely automatic, from `guard.js`'s
  `classifyActionRisk()` via `connectors/index.js`'s exported
  `classifyToolRisk(name, description)` — **no per-tool setting anywhere
  changes this**, matching "regardless of what standing permissions I've
  configured" literally. The detail page shows a small read-only "Always
  confirms" badge (`server.js`'s `publicConnector()` attaches a `risk` field
  to each `mcpTools` entry using the very same `classifyToolRisk()`, so the
  badge can never drift out of sync with what actually happens at runtime)
  but there is nothing there to click — confirmation timing isn't a dial.

**Tool grouping is a zero-cost, zero-model-call heuristic**, not a claim of real
information architecture: MCP tool lists carry no server-declared category, so
`_connector-detail.js`'s `groupFor()` splits a tool's name into words (handling
underscores, hyphens, AND camelCase boundaries) and checks every word against a
small verb list (search/list/find/query → "Search & browse", read/get/fetch →
"Read", create/write/update/edit/set/upload → "Create & edit", delete/remove/move →
"Delete & move", else "Other"). **Checking every word, not just a leading one, was
a real fix mid-build**: the first pass only matched a verb anchored to the very
start of the tool name (`search_x`), which silently put every one of Notion's real
tools — `notion-search`, `notion-fetch`, `notion-update-page`, `notion-delete-page`
— into "Other", since Notion's own naming convention leads with the service name,
not the verb. Caught by actually connecting a real service in testing, not by
reading the code.

**The guided-setup flow** (`connectFlow.kind: 'oauth_guided'`) is what an Official
Connector already known in advance to need a manually-created app/Client ID (GitHub,
Slack, Google Drive — see the "known-service shortcuts" verification table below)
falls back to: that catalog entry's own `connectFlow.guide.steps` as a numbered
list, a redirect-URI box (computed client-side as `window.location.origin +
'/api/connectors/oauth/callback'`, with a Copy button — the most common failure
mode in a manual flow like this is a mistyped redirect URI) with a copy button,
then Client ID/Secret fields and Connect — **historical, kept for accuracy about
what `guide.steps`/`guide.note` are for; this is no longer what actually renders**,
see "No Client ID/Secret UI anywhere" just below for the current, final state.
Server-side, three small routes replace what was one merged "official connect"
route: `POST /api/connectors/catalog/:catalogId/ensure` (find-or-create the
connector record — no OAuth attempt), `POST /api/connectors/custom` (create-only,
same reasoning), and the one generic `POST /api/connectors/:id/connect` (starts
OAuth for ANY existing mcp connector, official or custom, optionally with
`{clientId, clientSecret}` — still accepted server-side, just never sent by any
UI any more) — separating "create the record" from "start connecting" is what
lets the SAME detail page own the whole connect experience regardless of how the
user got there.

**No Client ID/Secret UI anywhere — the user's own final, explicit decision, and
the end state of a rule that regressed three times before landing here. Do not
re-add ANY form of it without being asked again.** The history, because it's the
reason the current shape is what it is:
1. Inline, unconditional Client ID/Secret fields on every connector's detail page.
2. Inline, but only shown once a failure or `guide` justified it.
3. Removed from the page; a "Have a Client ID for this service?" link + a
   guided-setup modal, both gated correctly on a real, per-connector recorded
   failure (`connectFlow.manualClient`) — genuinely correct, live-verified, no
   longer "unconditional" in any sense.
4. **The user, shown (3) working exactly as designed against their own real Google
   Drive connector, said they didn't want to see any of it, ever, even
   correctly-gated — "I did not ask you to remove any connector but I want you to
   remove that advance setting that is there."** This is the current state: no
   Client ID/Secret field, link, or modal exists anywhere in `public/` any more —
   not on the connector detail page, not on the Add-custom-connector form (its own
   "▸ Advanced settings" toggle, modeled directly on Claude's real UI, is gone
   too), not in Browse Connectors. `buildGuidedSetupModal()`/`buildManualClientModal()`
   were deleted outright, not just unlinked — dead UI code for a form the user
   explicitly doesn't want is still a way for it to reappear by accident.

**What this trades away, stated plainly so it isn't rediscovered by surprise
later:** Gmail/Google Drive/GitHub/Slack stay in the catalog and stay clickable —
removing the connectors themselves was explicitly ruled out. But their real OAuth
servers don't support automatic registration (verified live, repeatedly — no
`registration_endpoint`), so with no UI able to ever collect a Client ID, these
four simply cannot be connected through the app by the user alone. Clicking
Connect tries the automatic chain, it fails exactly as it always has, and the
real reason (`manualClientHint()`'s message) shows as plain, non-interactive
status text — informative, not actionable. **The only way any of the four ever
gets connected is `catalog-credentials.js`'s `register-client` route, called
directly (e.g. by Claude, given real credentials outside the app's own UI) —
that route was deliberately NOT removed, only unlinked from every screen.** If a
future session is asked to make Gmail "just work," the fix is calling that route
with a real Client ID/Secret, never adding a form back.

**Known-service shortcuts — verified live, not from docs/blog posts alone.** Every
catalog entry's real endpoint AND its Dynamic Client Registration support were
checked directly (`curl` against the real OAuth discovery documents), not assumed
from search results:

| Service | Real endpoint | DCR (one-click)? |
|---|---|---|
| Notion | `https://mcp.notion.com/mcp` | Yes — `oauth_dcr` |
| GitHub | `https://api.githubcopilot.com/mcp/` | No — `oauth_guided` |
| Slack | `https://mcp.slack.com/mcp` | No — `oauth_guided`; **also carries a real, unresolved risk**: Slack's OAuth docs require an HTTPS redirect URI and Jarvis's is plain HTTP — only a real click-through can confirm whether Slack's authorize step accepts it |
| Google Drive | `https://drivemcp.googleapis.com/mcp/v1` | No — `oauth_guided` |

**A real RFC 9728/8414 compliance bug this surfaced, in `oauth.js`'s `discover()`**:
the original discovery logic assumed a protected resource always sits at its
origin's root, guessing `{origin}/.well-known/oauth-protected-resource` and, for
its authorization server, `{authServerBase}/.well-known/oauth-authorization-server`
— both APPENDING the well-known segment. This works for Notion (whose resource
and issuer both happen to sit at their origin root) but silently failed for
GitHub: its MCP resource is at `/mcp/`, not the origin root, and per RFC 9728/8414
the well-known document then lives at
`{origin}/.well-known/oauth-<kind><resource-or-issuer-path>` — the well-known
segment INSERTED BEFORE the path. Found by hitting GitHub's real endpoint directly:
a plain `curl` to the guessed path 404'd, but the resulting 401's own
`WWW-Authenticate` header carried the real path in its `resource_metadata` hint
(`https://api.githubcopilot.com/.well-known/oauth-protected-resource/mcp/`) —
worth remembering as a diagnostic technique for any future OAuth discovery
mismatch. Fixed by trying both the path-aware and the plain-append forms, in that
order, so simpler servers (Notion, and Slack's — whose issuer IS its origin root)
keep working unchanged. Re-verified live against all three (GitHub, Slack, Notion)
after the fix.

## Gotchas

- **A freshly cold-started Chrome's CDP endpoint can respond over HTTP before its
  internal engine is fully warmed up** — `waitForDevtools` succeeding (the
  `/json/version` HTTP endpoint is up) does NOT mean a CDP command sent over a
  freshly-opened WebSocket to a page target will get answered. A command sent within
  roughly the first second of Chrome's own process life can go completely unanswered —
  no error, no close event, the socket just never responds — while the identical
  command a second or two later on a fresh connection works instantly. `ensureBrowser()`
  adds a flat ~1.2s pause after `waitForDevtools()` succeeds and before ever opening the
  WebSocket/sending anything; the retry loop around the first two commands
  (`Page.enable`/`Runtime.enable`) is defense in depth, not sufficient alone — only the
  upfront delay fixes it.
- **`Runtime.evaluate` + a page script beats the CDP Input domain for ordinary
  click/type** — dispatching raw mouse/keyboard events through
  `Input.dispatchMouseEvent` etc. works but is materially more code and more fragile
  across sites; running `element.click()` / setting `.value` via the native property
  setter + a real `input` event (needed for React/Vue-controlled inputs, which override
  the plain value setter) inside `Runtime.evaluate` is simpler and reliable against real
  sites. Reach for the Input domain only if a site is found to reject script-driven
  interaction.
- **`new URL(path, baseUrl)` is not "append path to base" when `path` starts with
  `/`** — a leading-slash relative reference resolves against the base's ORIGIN in the
  WHATWG URL spec, discarding the base's own path entirely
  (`new URL('/pet/findByStatus', 'https://petstore3.swagger.io/api/v3')` silently drops
  `/api/v3`). `api-client.js`'s `dispatch()` does plain string concatenation
  (`baseUrl.replace(/\/$/, '') + path`) then parses the result — this only shows up when
  the base URL itself has a non-empty path, which a naive test against
  `https://example.com` would never catch.
- **Two real, still-live OpenAPI/Swagger spec shapes exist, and only testing against
  both catches it.** OpenAPI 3.x declares its base address as `servers[0].url` (often
  relative, resolve against the spec document's own origin); Swagger 2.0 has no
  `servers` array at all, using `host` + `basePath` + `schemes` instead
  (`petstore.swagger.io/v2` is a real, still-live example). `discoverFromSpec()`
  normalizes both to one absolute `baseUrl`.
- **Lowercasing an identifier before checking camelCase boundaries destroys the
  boundary — a safety bug, not just a display one, when the result feeds the risk
  scan.** `sanitizeName()` lowercased first, so `"updatePet"` collapsed to the single
  token `"updatepet"` — cosmetic for the tools list (everything landed in "Other"), but
  `guard.js`'s word-tokenized risk scan (see "Standing permission" above) can then never
  match `"updatepet"` against the keyword `"update"`, since a word-boundary match
  requires `"update"` as its own token. A genuinely risky operation silently stopped
  being classified as risky. Fixed the same way `groupFor()` (above) and `guard.js`'s
  own tokenizer already do it: split camelCase boundaries into their own separator
  BEFORE lowercasing, never after.
- **A module that hardcodes a path relative to its OWN source file bypasses the
  `JARVIS_DATA_DIR` test-isolation convention entirely** (see the root `CLAUDE.md`'s
  testing section) — `browser.js` computed its Chrome profile directory from
  `__dirname`, not `JARVIS_DATA_DIR`, so a scratch test run once wrote a 145MB browser
  profile straight into the real project's `data/` folder. Fixed by using `store.js`'s
  `dataDir()` export, same as any other module needing its own subdirectory under
  `data/`.

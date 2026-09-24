# Connectors (`jarvis/connectors/`)

A connector is one saved way to reach something beyond raw desktop control: a record in
`data/connectors.json`, `{id, type, label, enabled, config, status}` (`store.py`, a leaf: the JSON
store and the clock).

Two groups of type:
- **Singletons** — `files` and `browser` (`SINGLETON_TYPES`). Jarvis's own built-in abilities: there
  is one set of "your browser settings", not a list. `get_or_create_singleton()` creates them on
  demand; the user never adds one and they have no App Control card. Their tool names are NOT
  prefixed (`UNPREFIXED_TYPES`).
- **The three peer, user-facing mechanisms** — `mcp`, `api`, `cli` (`USER_TYPES`). A user adds them
  either from the **Official Connectors** directory (`catalog.json`, listed by `catalog.py`) or as a
  **Custom Connector**, the one place a user picks a mechanism directly. None is "the real one" with
  the others bolted on: `capabilities.py` reads each mechanism's client module through the same
  interface, and every tool from any of them goes through the same permission filter and the same
  risk check before it reaches a model.

## `capabilities.py` — every connector's tools, merged into what the model can call

`connector_specs(connector)` turns each tool into a `CapabilitySpec` of `CapabilityKind.CONNECTOR`;
`sync(registry)` registers every enabled connector's tools and unregisters what is gone;
`refresh_tools(connector_id)` is the ONLY place that reaches out to ask a connector what it can do
(for MCP it caches the tool list in the connector's config, for an API connector it re-reads its
OpenAPI spec), so building a declaration list never starts a process or makes a request.
`tool_names_for(connector_id)` resolves a connector's current capability names on demand — a
scheduled task or briefing saves connector IDS and asks here at every run, because a tool list
changes when a connector reconnects. Non-singleton tool names are prefixed `<label>__<tool>`.

**Standing permission and runtime confirmation are two different things, deliberately not merged.**
- A standing PERMISSION (`store.tool_permission()`: `allow` | `ask` | `deny`, set per tool by the
  user) answers "may Jarvis use this at all". **A tool never set is `ask`** for mcp/api/cli — so every
  newly discovered tool asks first — and `allow` for the files/browser singletons. A `deny` tool is not
  declared at all, so the model never has to be refused. The lookup uses the PREFIXED name, since that
  is the only name the frontend, and therefore any saved permission, knows. Looking it up by the raw
  server name was a real bug: it always missed, so a tool set to "Blocked" stayed reachable.
  `_dispatch` re-checks at call time and re-reads the connector, since a permission or key may have
  changed since the declaration.
- **`ask` is `Risk.HIGH`, so it asks everywhere** — chat, specialists, scheduled tasks, briefings,
  jobs. The policy (`policy/decide.py`) lets MEDIUM through inside a pre-consented task; HIGH is the one
  level no autonomy and no blanket grant waves through, so the person's "ask" needs no second rule.
  **For mcp/api/cli the person's permission is FINAL** (`_risk_for()`): "allow" is `Risk.LOW`, so it
  runs without asking everywhere, whatever the classifier thinks. The user decided this after the
  classifier was found overriding "Always allow" for 26 of their 197 tools — harmless reads like a
  `get_message` or `query_database` included — in chat, while letting the same tools run unasked in a
  scheduled task. Do not reintroduce a risk layer above the user's choice for these types.
- `tool_rows()` is what the connector screen reads (`GET /api/connectors/{id}`): EVERY tool, blocked
  ones included, with each tool's `permission`. Only `connector_specs()` decides what
  the model sees. A blocked tool vanishing from the screen made blocking a one-way door — a real bug.
- **Jarvis is told what is set up**: `prompt.connected_apps_section()` puts every mcp/api/cli
  connector — connected, added-but-not-connected, switched off, tools not read yet, how many tools and
  their name prefix — into the VOLATILE half of Jarvis's own instruction each turn, read from this
  store and `tool_rows()`. Without it, asked "how many apps are connected", Jarvis searched its tools,
  found one app's OWN `list_connectors` tool (Lovable's workspace integrations) and answered from that.
  It never reaches the network, so it is safe per turn.
- `refresh_tools()` runs automatically after an OAuth connect (or a server needing no sign-in), on a
  background thread (`routes/connectors.py`'s `_discover_in_background`); the `/refresh` route runs it
  off the event loop, because `mcp_client` drives its async client from sync code and refuses to start
  inside a running loop.

Errors from a connector are passed through `redact.py`'s `redact_text()` before they go back to the
model, the saved conversation and the next provider's request, since a service that quotes a request
back could otherwise send a key straight through.

## `risk.py` — how dangerous is a tool nobody here declared?

**It decides only for Jarvis's own connectors (files, browser)**, which have no permission screen, and
feeds `control/guard.py`. It never overrides a user-added connector's per-tool permission (above).
Risk is inferred, bluntly and word-based, because the cost is asymmetric (a false "risky" costs one
confirmation, a false "safe" sends the email). Pure: no I/O, no state.
- **Whole words, never substrings.** Substring matching found "share" in "SharePoint" and "order" in
  "in sidebar order".
- **camelCase is split in a tool's NAME but not in its description.** A name is an identifier where
  `updatePet` really means update; a description is prose where SharePoint is a proper noun, and
  splitting it re-creates the false positive. Lowercasing an identifier BEFORE splitting destroys the
  boundaries — keep the real casing into `words()` for identifiers only.
- Only Jarvis's OWN connectors may declare a risk; anything from an outside server is always
  classified and cannot declare itself safe.
- `control/guard.py` imports these word lists rather than copying them, so there is one vocabulary.
- **Structural blind spot:** a generic "run whatever tool was found" dispatcher (a gateway-style MCP
  server such as Composio's) hides its real danger in its arguments, which a static per-declaration
  classifier never sees.

## The mechanisms

- **`files_connector.py`** (`files`) — read, write, list and move, inside an allowlist that starts
  EMPTY and grows only through conversation: an operation outside it fails with a plain error, the
  model asks, and only after an explicit yes does `tools/allow_folder.py` remember the folder. Every
  path is resolved and checked BEFORE any filesystem call (a relative path and `..` look like they stay
  put and do not; a symlink is only caught by resolving it).
- **`browser_connector.py`** (`browser`) — a real, VISIBLE browser window Jarvis drives when a page
  has to be interacted with (`browser_navigate`, `browser_read_page`, `browser_click`, ...), through
  Playwright as an optional import: no browser installed means a plain answer, not a stack trace. **Its
  own window and profile under the data folder, never the user's** — a task that borrows someone's
  browser inherits their logins and open work. A page is read by running a script against it (real
  text and elements), never by looking at a picture and guessing coordinates. It is separate from
  `webrender.py`, which renders a page headlessly for a background lookup and closes it; a background
  lookup must never navigate a page the user is watching. `NAV_TIMEOUT_MS` and `MAX_TEXT_CHARS` bound it.
- **`mcp_client.py`** (`mcp`) — an MCP server over stdio or HTTP, using the official MCP client library
  (already a dependency). **It connects per call, not once and kept**: a held-open subprocess is a
  lifecycle to get wrong, so a call is slower than against a warm process, and that trade is stated
  rather than hidden. The tool LIST is cached in the connector's config, so listing declarations never
  starts a server. Tokens live behind the connector's `secretRef` (see `oauth.py`). **A URL target
  must go through `streamable_http_client(url, http_client=create_mcp_http_client(headers=...))`** —
  `Client(url)` alone has no way to carry a header, and the bearer token was once built and silently
  never sent (every signed-in server answered 401; the tests only checked the token was looked up).
- **`api_client.py`** (`api`) — an HTTP API as tools. Operations come from an OpenAPI document
  (`discover_from_spec()`) or are added by hand. **The key never reaches the model**: it is a secret
  reference read at call time, and a declaration carries only name, description and parameters.
- **`cli_client.py`** (`cli`) — a local program as tools. Every command is a SAVED TEMPLATE: a fixed
  program and argv list where only marked placeholders are filled from the model's arguments, so the
  model chooses VALUES and never the command or a flag. No shell — the program is executed directly with
  an argument list, so quoting, globbing and `;` mean nothing — and the child gets
  `jarvis/childenv.py`'s scrubbed environment, so a program in a template cannot read the user's model
  API keys.

## `oauth.py` — OAuth 2.1 + PKCE for remote MCP connectors

The mechanism behind both a catalogue entry and a custom MCP connector (an official entry is a
curated, pre-filled custom one). **Generic by construction: no code branches on a service's name or
domain; every decision is driven by what a server's own metadata says.** It follows the MCP
Authorization spec (RFC 9728 protected-resource metadata, RFC 8414 authorization-server metadata,
OAuth 2.1 + PKCE, RFC 9207 `iss` mix-up mitigation, RFC 8707 resource indicators).

Flow: probe (one real unauthenticated request; a non-401 means no OAuth is needed, and a 401's own
`WWW-Authenticate` is the authoritative place to look next, never a guess) → discover the protected
resource and the authorization server, verifying PKCE support → obtain client credentials
(pre-registered or manually supplied first, else Dynamic Client Registration) → `start_connect()`
builds the authorize URL, or reports `noAuthNeeded` with no browser tab at all → `handle_callback()`
validates `iss` and redeems the code on Jarvis's own already-running server (pending flows are stored
in `connector-oauth-pending` for `PENDING_FLOW_TTL_S`) → `get_access_token()` returns a valid token,
silently refreshing an expiring one.

- It is built ON the MCP SDK's OAuth building blocks (PKCE, discovery URL orderings, the RFC 7591
  registration request, `WWW-Authenticate` parsing) but NOT the SDK's `OAuthClientProvider`, which
  blocks on one in-process redirect/callback pair and cannot span two separate HTTP requests with a
  browser round trip between them.
- The SDK's registration helper returns a request object of a transitive dependency's type; use it only
  to resolve the endpoint and build the body, then send it through this project's own `httpx`.
- **CIMD (Client ID Metadata Document) is deliberately not implemented**: it only pays off with a
  publicly reachable https address, which this project has none of.
- **A manual Client ID/Secret is optional and collapsed by default** ("Advanced settings" on the MCP
  connect card in `AppControlScreen.tsx`). It is sent on Connect only when filled in; left blank the
  request is unchanged and the automatic chain (discovery → dynamic registration) still runs first. A
  failed automatic attempt returns a `manualClient` hint explaining what to do; the section is never
  auto-expanded.
- `catalog_credentials.py` stores ONE Client ID/Secret per catalogue entry, registered once by the user
  and shared by every connector that entry creates (the closest a single-user install with no backend
  gets to a product registering one client centrally), because some real providers do not support
  dynamic registration. It is a leaf over `store.py`'s JSON helpers; the secret goes through
  `config.save_secret()`.

## Catalogue, icons, routes

- `catalog.json` / `catalog.py` — the bundled directory. **An entry is only listed once its real
  endpoint has been verified live**, not recalled from documentation; that is why it is short. Each
  `connectFlow` drops straight into a connector's config, so connecting from the catalogue runs the same
  flow a custom connector does. There is no "ready" versus "needs setup" badge.
- `icons.py` — real, current logos, fetched and cached (`data/connector-icons.json`), refreshed every
  `REFRESH_AFTER_S` (14 days), with a background resolver gated by `JARVIS_CONNECTOR_ICONS`. **No
  address a user or config supplied is ever fetched**: a connector's host is only a LOOKUP KEY against
  one fixed public icon service, and any other address fetched is a constant in the file. Following a
  connector's own URL would let anything on the machine's network be reached from here. A host that is
  not a plausible hostname is refused before it is used as a key. The interface's monogram shows while
  nothing has resolved.
- `routes/connectors.py` — list, catalogue, `POST /{id}/connect`, `/disconnect`, `/refresh`, the OAuth
  callback and redirect-uri routes, and catalogue client registration.

## Gotchas

- **Never assume a connector's tool list is current.** Read it through `tool_names_for()` /
  `refresh_tools()`; do not store names.
- **A connector's own server metadata is authoritative for OAuth.** If a new service does not work,
  fix the generic flow, not by branching on the service.
- **`connector_specs()` runs on every declaration build**, so it must never start a process, make a
  network call or block. Anything that does belongs in `refresh_tools()`.

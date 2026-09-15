"""OAuth 2.1 + PKCE for remote MCP connectors — the mechanism behind both a
catalogue entry and a custom MCP connector, since an official entry is just a
curated, pre-filled custom one.

**Generic by construction.** No code anywhere in this file branches on a
service's name or domain; every decision is driven by what a given server's own
metadata actually says. Follows the current MCP Authorization spec: RFC 9728
(Protected Resource Metadata), RFC 8414 (Authorization Server Metadata), OAuth
2.1 + PKCE, RFC 9207 (mix-up-attack mitigation via the `iss` callback
parameter), RFC 8707 (Resource Indicators).

Flow: `probe_authorization()` — one real, unauthenticated request; a non-401
response means no OAuth is needed at all, and a 401's own `WWW-Authenticate`
header is the authoritative place to look next, never a guess. ->
`discover_auth_server()` — protected-resource metadata, then the issuer's own
metadata, with PKCE support verified rather than assumed. ->
`obtain_client_credentials()` — pre-registered/manually-supplied credentials
first, else Dynamic Client Registration. -> `start_connect()` builds the
authorize URL, or reports `noAuthNeeded` with no browser tab at all when step
one found none is required. -> `handle_callback()` validates `iss` and redeems
the code on Jarvis's own already-running server. -> `get_access_token()`
returns a valid token, silently refreshing an expiring one first.

**Genuinely built on the MCP SDK's own OAuth module** (`mcp.client.auth.oauth2`,
already a declared dependency via `mcp`) rather than hand-rolled — PKCE
generation, the RFC 9728/8414 discovery URL orderings (which turn out to cover
more real cases than a hand-written version: OIDC discovery fallbacks the spec
allows and a hand-written version here did not have), the RFC 7591 registration
request, and `WWW-Authenticate` parsing are all delegated to it. **What is NOT
reused is the SDK's `OAuthClientProvider`** — that class is built for one
interactive, in-process connection (it blocks on a `redirect_handler`/
`callback_handler` pair), not a flow that has to span two separate HTTP
requests through this app's own routes with a real browser round trip in
between, which is what a web app needs. So this file writes its own
`start_connect()`/`handle_callback()` orchestration, the same shape as a
correct, already spec-verified hand-written version, built ON the SDK's
building blocks rather than reimplementing them.

**`create_client_registration_request()` returns an `httpx2.Request`** — a
separate package from this project's own declared `httpx` dependency, present
only because the installed `mcp` package happens to pull it in transitively,
not something this file should rely on directly (a future `mcp` release is free
to change it). Its request is used for exactly what it is good for — resolving
the real registration endpoint and building the correct, RFC-7591-aliased JSON
body from the typed model — and then sent through this project's own `httpx`
client instead of passing an undeclared dependency's object to it.

**CIMD (Client ID Metadata Document) is deliberately not implemented here.**
It only pays off once Jarvis has a real, publicly-reachable https address
(a tunnel) — this project has none, and Dynamic Client Registration plus the
manual-Client-ID fallback covers every verified catalogue entry. A disclosed
scope cut, not an oversight.
"""

from __future__ import annotations

import base64
import json
import logging
import secrets
import time
from typing import Any
from urllib.parse import urlencode, urlparse

import httpx

from ..config import delete_secret, get_secret, save_secret
from ..store import read_json, write_json
from . import store

logger = logging.getLogger(__name__)

PENDING_FILE = "connector-oauth-pending"
PENDING_FLOW_TTL_S = 30 * 60
TOKEN_REFRESH_SLACK_S = 30
TIMEOUT_S = 15.0
MCP_PROBE_HEADERS = {"Content-Type": "application/json",
                     "Accept": "application/json, text/event-stream"}


# --- Jarvis's own client identity ------------------------------------------------

def redirect_uri() -> str:
    """The one redirect URI every flow uses — pre-registered/manual credentials
    included, so a stored client's registered URI can never drift from what a
    later callback actually arrives at."""
    import os

    port = int(os.environ.get("PORT", "3000"))
    return f"http://127.0.0.1:{port}/api/connectors/oauth/callback"


def client_registration_body() -> dict[str, Any]:
    """The identity handed to a server that supports Dynamic Client
    Registration — a public client (`token_endpoint_auth_method: 'none'`),
    correct for something running locally with no way to keep a secret truly
    secret."""
    return {
        "client_name": "Jarvis",
        "redirect_uris": [redirect_uri()],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }


# --- the pending-flow store (state -> in-progress connect attempt) --------------

def _load_pending() -> dict[str, Any]:
    data = read_json(PENDING_FILE, {"flows": {}})
    flows = data.get("flows") if isinstance(data, dict) else None
    if not isinstance(flows, dict):
        return {"flows": {}}
    now = time.time()
    pruned = {state: flow for state, flow in flows.items()
              if isinstance(flow, dict) and float(flow.get("expiresAt") or 0) > now}
    return {"flows": pruned}


def _save_pending_flow(state: str, flow: dict[str, Any]) -> None:
    data = _load_pending()
    data["flows"][state] = flow
    write_json(PENDING_FILE, data)


def _get_pending_flow(state: str) -> dict[str, Any] | None:
    return _load_pending()["flows"].get(state)


def _delete_pending_flow(state: str) -> None:
    data = _load_pending()
    data["flows"].pop(state, None)
    write_json(PENDING_FILE, data)


def _pkce_pair() -> tuple[str, str]:
    from mcp.client.auth.oauth2 import PKCEParameters

    params = PKCEParameters.generate()
    return params.code_verifier, params.code_challenge


def _pkce_pair_native() -> tuple[str, str]:
    """Fallback if the SDK's own generator is ever unavailable — kept tiny and
    spec-literal (RFC 7636 §4.1/§4.2) so it is easy to trust on sight."""
    import hashlib

    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    return verifier, challenge


# --- discovery -------------------------------------------------------------------

async def _try_fetch_json(client: httpx.AsyncClient, url: str) -> dict[str, Any] | None:
    try:
        response = await client.get(url)
        if response.status_code != 200:
            return None
        return response.json()
    except (httpx.HTTPError, ValueError):
        return None


async def _probe_authorization(client: httpx.AsyncClient, mcp_url: str) -> dict[str, Any]:
    """The spec's own first move: a real, unauthenticated request against the
    MCP server itself, and let ITS response decide what happens next — rather
    than assuming OAuth is required and going straight to discovery, which can
    wrongly enter a setup flow a server never needed. Checks both `initialize`
    and `tools/list` unauthenticated: some real servers accept an anonymous
    handshake and only gate actual tool access.

    Returns `{authRequired: True|False|None}` — `None` means genuinely
    ambiguous (a network failure, or some other status entirely), and the
    caller falls back to well-known-URL guessing rather than guessing which way
    this resolves.
    """
    from mcp.client.auth.oauth2 import (
        extract_resource_metadata_from_www_auth, extract_scope_from_www_auth,
    )

    def _auth_required_from(response: httpx.Response) -> dict[str, Any]:
        return {"authRequired": True,
                "resourceMetadataUrl": extract_resource_metadata_from_www_auth(response),
                "scope": extract_scope_from_www_auth(response)}

    try:
        init = await client.post(mcp_url, headers=MCP_PROBE_HEADERS, json={
            "jsonrpc": "2.0", "id": 0, "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                       "clientInfo": {"name": "jarvis", "version": "1.0.0"}}})
        if init.status_code == 401:
            return _auth_required_from(init)
        if init.status_code >= 400:
            return {"authRequired": None}

        headers = dict(MCP_PROBE_HEADERS)
        session_id = init.headers.get("Mcp-Session-Id")
        if session_id:
            headers["Mcp-Session-Id"] = session_id
        listed = await client.post(mcp_url, headers=headers, json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
        if listed.status_code == 401:
            return _auth_required_from(listed)
        if listed.status_code < 400:
            return {"authRequired": False}
        return {"authRequired": None}
    except httpx.HTTPError:
        return {"authRequired": None}


def _is_protected_resource_doc(doc: dict[str, Any]) -> bool:
    return (isinstance(doc.get("resource"), str) or isinstance(doc.get("authorization_servers"), list)
            or isinstance(doc.get("scopes_supported"), list))


def _is_auth_server_doc(doc: dict[str, Any]) -> bool:
    return isinstance(doc.get("issuer"), str) or (
        isinstance(doc.get("authorization_endpoint"), str) and isinstance(doc.get("token_endpoint"), str))


def _canonical(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}".rstrip("/")


def _resource_field_matches(meta: dict[str, Any], mcp_url: str) -> bool:
    """RFC 9728 §3.3: a metadata document's own `resource` field, when
    present, MUST match the MCP URL actually requested — guards against a
    document meant for a different resource being trusted. Lenient when the
    field is absent, and compares origin+path only (ignoring query/fragment,
    since a saved connector URL often carries one the canonical resource
    identifier does not)."""
    resource = meta.get("resource")
    if not isinstance(resource, str):
        return True
    return _canonical(resource) == _canonical(mcp_url)


async def _discover_protected_resource(client: httpx.AsyncClient, mcp_url: str) -> dict[str, Any]:
    """RFC 9728: whether this resource requires authorization, and if so which
    authorization server issues its tokens."""
    from mcp.client.auth.oauth2 import build_protected_resource_metadata_discovery_urls

    probe = await _probe_authorization(client, mcp_url)

    if probe.get("authRequired") is True and probe.get("resourceMetadataUrl"):
        meta = await _try_fetch_json(client, probe["resourceMetadataUrl"])
        if meta and _is_protected_resource_doc(meta) and _resource_field_matches(meta, mcp_url):
            servers = meta.get("authorization_servers") or []
            return {"authRequired": True, "issuer": servers[0] if servers else None,
                    "resourceId": meta.get("resource") or mcp_url,
                    "resourceScopes": meta.get("scopes_supported"),
                    "challengeScope": probe.get("scope")}
        # Present but unusable (malformed, or a resource mismatch — possible
        # impersonation per RFC 9728 §3.3) — fall through to well-known lookup.

    # Runs even when the probe saw no 401: a server that PUBLISHES protected-
    # resource metadata is a protected resource by its own declaration,
    # regardless of what an unauthenticated request happened to answer (a
    # stateless server can gate only actual tool calls, not the handshake).
    for url in build_protected_resource_metadata_discovery_urls(None, mcp_url):
        meta = await _try_fetch_json(client, url)
        if meta and _is_protected_resource_doc(meta):
            break
    else:
        meta = None

    if not meta and probe.get("authRequired") is False:
        return {"authRequired": False}

    servers = (meta or {}).get("authorization_servers") or []
    return {"authRequired": True, "issuer": servers[0] if servers else None,
            "resourceId": (meta or {}).get("resource") or mcp_url,
            "resourceScopes": (meta or {}).get("scopes_supported"),
            "challengeScope": probe.get("scope") if probe.get("authRequired") is True else None}


async def _discover_authorization_server(client: httpx.AsyncClient, issuer_url: str) -> dict[str, Any] | None:
    """RFC 8414: the issuer's real authorize/token/registration endpoints.
    Returns None (not a raise) on a missing/malformed document so the caller
    can try another candidate. Enforces RFC 8414 §3.3: a returned `issuer`
    field must match the URL actually asked — without this a bare-origin guess
    can land on a different, unrelated authorization server that just happens
    to answer with valid-looking metadata at that origin."""
    from mcp.client.auth.oauth2 import build_oauth_authorization_server_metadata_discovery_urls

    for url in build_oauth_authorization_server_metadata_discovery_urls(None, issuer_url):
        meta = await _try_fetch_json(client, url)
        if not meta or not _is_auth_server_doc(meta):
            continue
        if not meta.get("authorization_endpoint") or not meta.get("token_endpoint"):
            continue
        declared = meta.get("issuer")
        if declared and _canonical(str(declared)) != _canonical(issuer_url):
            continue
        return meta
    return None


async def _discover_auth_server(client: httpx.AsyncClient, mcp_url: str) -> dict[str, Any]:
    """Tries, in order: the issuer the resource explicitly named; then the
    resource's own URL, path included (a path-scoped MCP server often hosts
    its OAuth server at that same path); only then the resource's bare origin.
    Jumping straight to the bare origin can silently resolve to a different,
    unrelated authorization server rather than either finding the right one
    or failing honestly."""
    discovery = await _discover_protected_resource(client, mcp_url)
    if not discovery.get("authRequired"):
        return {"authRequired": False}

    issuer = discovery.get("issuer")
    candidates = [issuer] if issuer else [mcp_url, f"{urlparse(mcp_url).scheme}://{urlparse(mcp_url).netloc}"]
    for candidate in candidates:
        as_meta = await _discover_authorization_server(client, candidate)
        if as_meta:
            return {"authRequired": True, "asMeta": as_meta,
                    "resourceId": discovery.get("resourceId") or mcp_url,
                    "resourceScopes": discovery.get("resourceScopes"),
                    "challengeScope": discovery.get("challengeScope")}
    host = urlparse(mcp_url).netloc or "This service"
    raise RuntimeError(
        f"{host} didn't return the OAuth details Jarvis needs — this is sometimes temporary. "
        "Check the server address, then click Connect to try again.")


# --- obtaining a client (pre-registered, manual, or DCR) -------------------------

async def _post_registration(client: httpx.AsyncClient, endpoint: str,
                             body: dict[str, Any]) -> dict[str, Any]:
    try:
        response = await client.post(endpoint, json=body,
                                     headers={"Content-Type": "application/json"})
    except httpx.HTTPError as err:
        return {"ok": False, "reason": "network", "detail": str(err) or "could not reach the server"}
    if response.status_code >= 400:
        detail = f"status {response.status_code}"
        try:
            err_body = response.json()
            detail = err_body.get("error_description") or err_body.get("error") or detail
        except ValueError:
            pass
        return {"ok": False, "reason": "rejected", "detail": detail}
    try:
        ok_body = response.json()
    except ValueError:
        return {"ok": False, "reason": "rejected",
                "detail": "the server accepted the request but sent back something unreadable"}
    return {"ok": True, "clientId": ok_body.get("client_id"), "clientSecret": ok_body.get("client_secret")}


async def _try_dynamic_client_registration(client: httpx.AsyncClient,
                                           as_meta: dict[str, Any]) -> dict[str, Any]:
    """RFC 7591 — spec-deprecated but still what most real servers offer.
    `application_type: 'native'` matters: Jarvis is reached over 127.0.0.1, not
    a remote web origin, and omitting it defaults to "web" under OIDC, which
    can reject a loopback redirect URI. A server that rejects an unrecognised
    member rather than ignoring it looks identical to a real refusal without a
    retry, so the plain body is tried once more before giving up."""
    from mcp.client.auth.oauth2 import create_client_registration_request
    from mcp.shared.auth import OAuthClientMetadata, OAuthMetadata

    registration_endpoint = as_meta.get("registration_endpoint")
    if not registration_endpoint:
        return {"ok": False, "reason": "unsupported"}

    metadata = OAuthClientMetadata(**{**client_registration_body(), "application_type": "native"})
    request = create_client_registration_request(
        OAuthMetadata.model_validate(as_meta), metadata, registration_endpoint)
    body = json.loads(request.content or b"{}")

    first = await _post_registration(client, str(request.url), body)
    if first["ok"] or first.get("reason") == "network":
        return first
    body.pop("application_type", None)
    second = await _post_registration(client, str(request.url), body)
    return second if second["ok"] else first  # first carries the more informative refusal text


async def _obtain_client_credentials(client: httpx.AsyncClient, as_meta: dict[str, Any],
                                     manual_client_id: str | None,
                                     manual_client_secret: str | None) -> dict[str, Any]:
    """Priority order: pre-registered/manually-supplied credentials first, then
    Dynamic Client Registration. (No CIMD step — see this file's header.)"""
    if manual_client_id:
        return {"ok": True, "clientId": manual_client_id, "clientSecret": manual_client_secret,
                "via": "preregistered"}
    dcr = await _try_dynamic_client_registration(client, as_meta)
    return {**dcr, "via": "dcr"} if dcr["ok"] else dcr


def _build_authorize_url(as_meta: dict[str, Any], client_id: str, state: str, challenge: str,
                         scopes: list[str] | None, resource: str | None) -> str:
    params = {
        "response_type": "code", "client_id": client_id, "redirect_uri": redirect_uri(),
        "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
    }
    # Prefer the protected resource's own declared scopes over the
    # authorization server's full advertised list, which can be far wider
    # (everything it issues tokens for, not just this one resource) — a fresh
    # client is far more likely to actually be granted what the resource
    # itself says it needs.
    scope_list = scopes or as_meta.get("scopes_supported")
    if scope_list:
        params["scope"] = " ".join(scope_list)
    if resource:  # RFC 8707
        params["resource"] = resource
    return f"{as_meta['authorization_endpoint']}?{urlencode(params)}"


def _manual_client_hint(failure: dict[str, Any], mcp_url: str) -> dict[str, Any]:
    """Turns a classified credential failure into a plain-language,
    service-specific explanation — never a generic "needs a Client ID"
    sentence regardless of why. Reads only the failure's own classification and
    the server's own response; no service name is ever branched on."""
    host = urlparse(mcp_url).netloc or "This service"
    reason = failure.get("reason")
    if reason == "unsupported":
        message = (f"{host} doesn't hand out app credentials automatically — it needs a "
                  "Client ID registered with it directly.")
    elif reason == "network":
        detail = f" ({failure['detail']})" if failure.get("detail") else ""
        message = f"Jarvis couldn't reach {host} to register automatically{detail}. Click Connect to try again."
    else:
        detail = f": {failure['detail']}" if failure.get("detail") else "."
        message = f"{host} declined to register Jarvis automatically{detail}"
    return {"reason": reason, "detail": failure.get("detail"), "message": message}


# --- the two halves a browser round trip splits this into -----------------------

async def start_connect(connector_id: str, mcp_url: str, *, manual_client_id: str | None = None,
                        manual_client_secret: str | None = None) -> dict[str, Any]:
    """Discovers the server's OAuth setup, obtains client credentials, and
    returns the URL the browser should open.

    When no authorization is needed at all, the connector is marked usable
    immediately and `{noAuthNeeded: True}` comes back — no browser tab, no
    credentials, nothing else. Obtaining a client automatically can fail
    several ways (no registration endpoint, a real refusal, a network
    failure), and none of them raise or permanently block the connector — every
    failure returns `{needsManualClient: True, reason, detail, message}`, and a
    past failure never forecloses trying again: the very next Connect click
    re-attempts the full chain from scratch.
    """
    async with httpx.AsyncClient(timeout=TIMEOUT_S, follow_redirects=True) as client:
        discovery = await _discover_auth_server(client, mcp_url)
        connector = store.get_connector(connector_id)
        existing_flow = ((connector or {}).get("config") or {}).get("connectFlow") or {}

        if not discovery.get("authRequired"):
            store.update_connector(connector_id, {
                "config": {"connectFlow": {**existing_flow, "kind": "none"}},
                "status": {"state": "working", "checkedAt": None, "detail": None}})
            return {"noAuthNeeded": True}

        as_meta = discovery["asMeta"]
        if not as_meta.get("code_challenge_methods_supported"):
            # PKCE support must be VERIFIED, not assumed — sending a
            # code_challenge a server can't be sure it will honour would
            # silently downgrade a security control this whole flow depends on.
            raise RuntimeError(
                "This server's OAuth setup doesn't support PKCE, which Jarvis requires "
                "for a secure connection — it can't be set up automatically.")

        credentials = await _obtain_client_credentials(
            client, as_meta, manual_client_id, manual_client_secret)
        if not credentials["ok"]:
            hint = _manual_client_hint(credentials, mcp_url)
            store.update_connector(connector_id, {
                "config": {"connectFlow": {**existing_flow, "manualClient": hint}}})
            return {"needsManualClient": True, **hint}

        store.update_connector(connector_id, {
            "config": {"connectFlow": {**existing_flow, "manualClient": None,
                                       "credentialSource": credentials["via"]}}})

        try:
            verifier, challenge = _pkce_pair()
        except ImportError:
            verifier, challenge = _pkce_pair_native()
        state = secrets.token_urlsafe(16)
        scope_list = (discovery.get("challengeScope") or "").split() or discovery.get("resourceScopes")
        resource = discovery.get("resourceId") or mcp_url

        _save_pending_flow(state, {
            "connectorId": connector_id, "verifier": verifier,
            "clientId": credentials["clientId"], "clientSecret": credentials.get("clientSecret"),
            "tokenEndpoint": as_meta["token_endpoint"], "resource": resource,
            # RFC 9207 mix-up-attack protection — recorded now, while the
            # discovery that produced them is still trusted.
            "issuer": as_meta.get("issuer"),
            "issSupported": bool(as_meta.get("authorization_response_iss_parameter_supported")),
            "expiresAt": time.time() + PENDING_FLOW_TTL_S,
        })

        return {"authUrl": _build_authorize_url(
            as_meta, credentials["clientId"], state, challenge, scope_list, resource)}


async def _exchange_code_for_tokens(client: httpx.AsyncClient, flow: dict[str, Any],
                                    code: str) -> dict[str, Any]:
    body = {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri(),
            "client_id": flow["clientId"], "code_verifier": flow["verifier"]}
    if flow.get("clientSecret"):
        body["client_secret"] = flow["clientSecret"]
    if flow.get("resource"):
        body["resource"] = flow["resource"]  # RFC 8707

    response = await client.post(flow["tokenEndpoint"], data=body)
    if response.status_code >= 400:
        raise RuntimeError(f"The service would not complete the connection ({response.status_code}).")
    return response.json()


def _account_hint_from(tokens: dict[str, Any]) -> str | None:
    """A plain-language account hint from a standard OIDC `id_token`, when the
    token response happens to include one — honestly best-effort. Decoded only,
    NEVER signature-verified: used exclusively as a display label (telling two
    connectors to the same service apart), never for any authorization
    decision, so an unverified claim here could at most mislabel a connector.
    The token endpoint that issued it was already reached over TLS."""
    id_token = tokens.get("id_token")
    if not isinstance(id_token, str) or id_token.count(".") < 2:
        return None
    try:
        segment = id_token.split(".")[1]
        segment += "=" * (-len(segment) % 4)
        payload = json.loads(base64.urlsafe_b64decode(segment))
    except (ValueError, TypeError):
        return None
    return payload.get("email") or payload.get("preferred_username") or payload.get("name") \
        or payload.get("sub")


def _token_set_from(tokens: dict[str, Any], flow: dict[str, Any]) -> dict[str, Any]:
    from mcp.client.auth.oauth2 import calculate_token_expiry

    return {
        "accessToken": tokens["access_token"], "refreshToken": tokens.get("refresh_token"),
        "expiresAt": calculate_token_expiry(tokens.get("expires_in")),
        "tokenEndpoint": flow["tokenEndpoint"], "clientId": flow["clientId"],
        "clientSecret": flow.get("clientSecret"), "resource": flow.get("resource"),
    }


async def handle_callback(query: dict[str, Any]) -> dict[str, Any]:
    """Handles the redirect back from the provider. Exchanges the code for
    tokens and saves them. Never raises — returns `{ok, error?, connectorId?}`
    so the route can show a plain page either way."""
    code, state = query.get("code"), query.get("state")
    error, error_description = query.get("error"), query.get("error_description")
    iss = query.get("iss")
    flow = _get_pending_flow(state) if state else None

    if error:
        if flow and state:
            _delete_pending_flow(state)
        return {"ok": False, "error": error_description or f"The service refused to connect: {error}",
                "connectorId": (flow or {}).get("connectorId")}

    if not flow:
        return {"ok": False, "error": "This sign-in link is invalid or was already used.",
                "connectorId": None}
    _delete_pending_flow(state)  # one-time use either way

    if time.time() > flow["expiresAt"]:
        return {"ok": False, "error": "This sign-in link expired — go back and click Connect again.",
                "connectorId": flow["connectorId"]}

    # RFC 9207: if the authorization server told us ahead of time it always
    # includes `iss`, a response missing it is rejected outright; whenever
    # `iss` IS present, it must match — plain comparison, no normalisation —
    # the issuer this specific flow was actually started against.
    if flow.get("issSupported") and not iss:
        return {"ok": False,
                "error": "This sign-in response is missing required security information — "
                        "go back and click Connect again.",
                "connectorId": flow["connectorId"]}
    if iss and flow.get("issuer") and iss != flow["issuer"]:
        return {"ok": False,
                "error": "This sign-in response does not match the service Jarvis was "
                        "connecting to — go back and click Connect again.",
                "connectorId": flow["connectorId"]}
    if not code:
        return {"ok": False,
                "error": "This sign-in response is missing the required code — "
                        "go back and click Connect again.",
                "connectorId": flow["connectorId"]}

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
            tokens = await _exchange_code_for_tokens(client, flow, code)
    except (httpx.HTTPError, RuntimeError, ValueError) as err:
        return {"ok": False, "error": str(err) or "Could not finish connecting to the service.",
                "connectorId": flow["connectorId"]}

    secret_ref = f"connoauth_{flow['connectorId']}"
    save_secret(secret_ref, json.dumps(_token_set_from(tokens, flow)))

    try:
        store.update_connector(flow["connectorId"], {
            "config": {"secretRef": secret_ref},
            "status": {"state": "working", "checkedAt": None, "detail": None},
            # Plain, non-secret display metadata — never inside the secret blob
            # above. Set from THIS response every time, so it always reflects
            # who is actually signed in right now.
            "accountHint": _account_hint_from(tokens),
        })
    except Exception as err:  # noqa: BLE001 — the token DID save; say so honestly
        return {"ok": False, "error": f"Connected, but could not save it: {err}",
                "connectorId": flow["connectorId"]}

    return {"ok": True, "connectorId": flow["connectorId"]}


# --- using and maintaining a connection ------------------------------------------

async def _refresh_token_set(token_set: dict[str, Any]) -> dict[str, Any]:
    if not token_set.get("refreshToken"):
        raise RuntimeError("This connection expired and has no way to refresh itself — reconnect it.")
    body = {"grant_type": "refresh_token", "refresh_token": token_set["refreshToken"],
            "client_id": token_set["clientId"]}
    if token_set.get("clientSecret"):
        body["client_secret"] = token_set["clientSecret"]
    if token_set.get("resource"):
        body["resource"] = token_set["resource"]

    async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
        response = await client.post(token_set["tokenEndpoint"], data=body)
    if response.status_code >= 400:
        raise RuntimeError("This connection expired and could not be refreshed — reconnect it.")
    fresh = response.json()

    from mcp.client.auth.oauth2 import calculate_token_expiry

    return {**token_set, "accessToken": fresh["access_token"],
            "refreshToken": fresh.get("refresh_token") or token_set.get("refreshToken"),
            "expiresAt": calculate_token_expiry(fresh.get("expires_in"))}


def is_oauth_flow(config: dict[str, Any]) -> bool:
    """Whether an MCP connector's config was set up through THIS module, as
    opposed to a static bearer token pasted in by hand. `mcp_client.py` reads
    this to decide whether `secretRef` holds a plain token or a JSON token set
    that needs to go through `get_access_token()` (refreshing first, if due)."""
    return ((config or {}).get("connectFlow") or {}).get("kind") in ("oauth_dcr", "oauth_guided")


async def get_access_token(connector_id: str) -> str | None:
    """A valid access token, refreshing first if it is expired or about to be
    (within `TOKEN_REFRESH_SLACK_S`). Returns None for a connector verified at
    connect time to need no authorization at all — an expected, non-error
    state, not something with a token to fetch."""
    connector = store.get_connector(connector_id)
    config = (connector or {}).get("config") or {}
    if (config.get("connectFlow") or {}).get("kind") == "none":
        return None
    ref = config.get("secretRef")
    raw = get_secret(ref) if ref else None
    if not raw:
        raise RuntimeError("This connector is not connected yet.")

    try:
        token_set = json.loads(raw)
    except ValueError as err:
        raise RuntimeError("This connection is in a bad state — reconnect it.") from err

    expires_at = token_set.get("expiresAt")
    # `is not None`, not a truthy check: a real expiry is always a huge Unix
    # timestamp, but writing this as a bare `if expires_at` is the exact
    # falsy-vs-absent confusion that has bitten this codebase before.
    if expires_at is not None and time.time() > expires_at - TOKEN_REFRESH_SLACK_S:
        token_set = await _refresh_token_set(token_set)
        save_secret(ref, json.dumps(token_set))

    return token_set["accessToken"]


def disconnect(connector_id: str) -> None:
    """Deletes a connector's stored token set. The connector record itself is
    untouched — the caller decides whether to also remove it entirely."""
    connector = store.get_connector(connector_id)
    ref = ((connector or {}).get("config") or {}).get("secretRef")
    if ref:
        delete_secret(ref)


async def recheck_no_auth_connectors() -> None:
    """Re-checks every connector currently saved as needing no authorization: if
    its server actually publishes protected-resource metadata, the original
    conclusion was wrong (a real, verified case: some servers answer both
    `initialize` and `tools/list` anonymously yet still publish real metadata,
    gating only actual tool calls) — demoted back to needing a real connect,
    rather than sitting as "working" with a tools list that stays empty
    forever with no path back to Connect. Best-effort, never blocks startup."""
    async with httpx.AsyncClient(timeout=TIMEOUT_S, follow_redirects=True) as client:
        for connector in store.list_connectors("mcp"):
            flow = (connector.get("config") or {}).get("connectFlow") or {}
            if flow.get("kind") != "none" or not flow.get("url"):
                continue
            try:
                discovery = await _discover_protected_resource(client, flow["url"])
                if not discovery.get("authRequired"):
                    continue
                store.update_connector(connector["id"], {
                    "config": {"connectFlow": {**flow, "kind": "oauth_dcr"}},
                    "status": {"state": "error", "checkedAt": None,
                              "detail": "This service needs you to sign in — click Connect."}})
            except Exception:  # noqa: BLE001 — best-effort; leave this one as it was
                continue

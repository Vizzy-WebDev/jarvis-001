// OAuth 2.1 + PKCE client for remote MCP connectors — the mechanism behind
// both "Official Connectors" and "Custom Connector" MCP entries (an official
// one is just a curated, pre-filled custom one). Generic by construction —
// no code anywhere in this file branches on a specific provider's name; every
// decision is driven entirely by what a given server's own metadata says.
//
// Rebuilt against the CURRENT MCP Authorization specification (2026-07-28 —
// this supersedes 2025-06-18, which some earlier work in this file was
// informed by; see CLAUDE.md's "Fix the shared MCP connection/authorization
// architecture" for the full live-verified diff between the two) after a
// real architectural gap surfaced: every earlier version of this file always
// assumed OAuth was required and went straight to discovery, with no code
// path that ever asked the server itself whether authorization was needed at
// all, and discovery itself only ever guessed well-known URLs rather than
// reading what a real 401 response actually says. Both are fixed below.
//
// Flow, per the current spec (RFC 9728 Protected Resource Metadata, RFC 8414
// Authorization Server Metadata, the MCP Client Registration page covering
// Client ID Metadata Documents / Dynamic Client Registration / pre-
// registration, OAuth 2.1 + PKCE, RFC 9207 mix-up-attack mitigation, RFC 8707
// Resource Indicators):
//   1. probeAuthorization()      one real, unauthenticated MCP request —
//                                a non-401 response means no OAuth is needed
//                                at all; a 401's own WWW-Authenticate header
//                                is the authoritative source for where to
//                                look next, not a guess
//   2. discoverAuthServer()      protected-resource metadata (from the
//                                probe's hint, or well-known guessing as the
//                                documented fallback when that's absent) ->
//                                issuer -> its authorize/token/registration
//                                endpoints, with PKCE-support verified
//   3. obtainClientCredentials() pre-registered/manually-supplied credentials
//                                first, else DCR if the server supports it
//   4. startConnect()            builds the PKCE challenge + authorize URL,
//                                or marks the connector usable immediately
//                                with no browser tab at all when step 1 found
//                                no authorization is required
//   5. handleCallback()          validates `iss` (mix-up-attack protection)
//                                and redeems the code for tokens on Jarvis's
//                                own already-running server — no separate
//                                temporary listener needed
//   6. getAccessToken()          returns a valid token (or null for a
//                                connector that needs none), silently
//                                refreshing an expiring one first
//
// Token sets are one JSON-stringified value under config.js's existing
// saveSecret()/getSecret() — no new secret store, matching every other
// secret in the app.

import crypto from 'node:crypto';
import { saveSecret, getSecret, deleteSecret } from '../config.js';
import { getConnector, updateConnector, listConnectors, recordStatus } from './store.js';
import { readJson, writeJson } from '../store.js';
import { redirectUri as clientRedirectUri, cimdUrl, clientMetadataDocument, clientRegistrationBody } from './client-identity.js';

// 30 minutes, not 10 — long enough to actually leave and come back to finish
// signing in. Raised as part of the reconnection-glitch fix (see CLAUDE.md):
// the bigger problem was that this state lived only in memory at all (a
// server restart lost it regardless of TTL), fixed below by persisting it.
const PENDING_FLOW_TTL_MS = 30 * 60 * 1000;
const TOKEN_REFRESH_SLACK_MS = 30 * 1000;
const PENDING_FLOWS_FILE = 'oauth-pending';

// Persisted to data/oauth-pending.json (not just kept in a module-level Map)
// specifically so an in-flight sign-in survives the user coming back later
// or a server restart — the original in-memory-only Map lost every pending
// flow on either. Keyed by the OAuth `state` value; value is
// { connectorId, verifier, clientId, clientSecret, tokenEndpoint, expiresAt }.
// Expired entries are pruned on every read/write rather than left to leak
// for the process lifetime, which the old Map never did either.
function loadPendingFlowsFile() {
  return readJson(PENDING_FLOWS_FILE, { flows: {} });
}

function pruneExpired(data) {
  const now = Date.now();
  let changed = false;
  for (const [state, flow] of Object.entries(data.flows)) {
    if (!flow.expiresAt || flow.expiresAt < now) {
      delete data.flows[state];
      changed = true;
    }
  }
  return changed;
}

function getPendingFlow(state) {
  const data = loadPendingFlowsFile();
  if (pruneExpired(data)) writeJson(PENDING_FLOWS_FILE, data);
  return data.flows[state] || null;
}

function savePendingFlow(state, flow) {
  const data = loadPendingFlowsFile();
  pruneExpired(data);
  data.flows[state] = flow;
  writeJson(PENDING_FLOWS_FILE, data);
}

function deletePendingFlow(state) {
  const data = loadPendingFlowsFile();
  delete data.flows[state];
  writeJson(PENDING_FLOWS_FILE, data);
}

// Re-exported from client-identity.js (the new single source of truth for
// Jarvis's OAuth client identity, shared by CIMD/DCR/manual entry alike) so
// every existing caller of oauth.redirectUri() — server.js's
// GET /api/connectors/oauth/redirect-uri route included — keeps working
// unchanged. See client-identity.js's header for why this moved.
export const redirectUri = clientRedirectUri;

function base64url(buffer) {
  return buffer.toString('base64').replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

function newPkcePair() {
  const verifier = base64url(crypto.randomBytes(32));
  const challenge = base64url(crypto.createHash('sha256').update(verifier).digest());
  return { verifier, challenge };
}

async function tryFetchJson(url) {
  try {
    const res = await fetch(url);
    if (!res.ok) return null;
    return await res.json();
  } catch {
    return null;
  }
}

/**
 * Parses a WWW-Authenticate header's auth-params (RFC 9110 §11.6.1, the
 * `resource_metadata`/`scope` params specifically from RFC 9728 §5.1 and RFC
 * 6750 §3) into a plain object — e.g. `Bearer resource_metadata="...",
 * scope="a b"` -> `{ scheme: 'Bearer', resource_metadata: '...', scope: 'a b' }`.
 * Handles both quoted and bare token values; this is deliberately narrow —
 * good enough for the small, well-defined set of params these RFCs actually
 * define, not a general HTTP structured-field parser.
 */
function parseWwwAuthenticate(header) {
  if (!header) return null;
  const schemeMatch = header.match(/^(\S+)\s*(.*)$/s);
  if (!schemeMatch) return null;
  const [, scheme, rest] = schemeMatch;
  const params = { scheme };
  const paramRe = /([a-zA-Z_][a-zA-Z0-9_]*)=(?:"([^"]*)"|([^\s,]+))/g;
  let m;
  while ((m = paramRe.exec(rest))) {
    params[m[1]] = m[2] !== undefined ? m[2] : m[3];
  }
  return params;
}

function authRequiredFrom(res) {
  const auth = parseWwwAuthenticate(res.headers.get('WWW-Authenticate'));
  return { authRequired: true, resourceMetadataUrl: auth?.resource_metadata || null, scope: auth?.scope || null };
}

const MCP_PROBE_HEADERS = { 'Content-Type': 'application/json', Accept: 'application/json, text/event-stream' };

/**
 * The actual first move the current MCP spec describes: attempt a real,
 * unauthenticated request against the MCP server itself, and let ITS
 * response decide what happens next, rather than assuming OAuth is required
 * and going straight to discovery (the gap that let some servers wrongly
 * enter an OAuth setup flow when they didn't need one at all — see
 * CLAUDE.md). Checks BOTH `initialize` and `tools/list` unauthenticated, not
 * just the handshake — some real servers accept an anonymous `initialize`
 * (it's just capability negotiation) and only gate actual tool access,
 * which a single-call probe would misread as "no auth needed" and then
 * break on the very next real request. Deliberately self-contained here
 * rather than reusing mcp-remote-client.js's rpcCall(): that module already
 * imports getAccessToken from this file, so importing back from it would be
 * circular — this small amount of duplication is the cheaper trade.
 *
 * Returns:
 *   { authRequired: false }                                  — connect directly, no OAuth at all
 *   { authRequired: true, resourceMetadataUrl, scope }        — a real 401; resourceMetadataUrl (from
 *                                                                WWW-Authenticate, when the server sent one)
 *                                                                is the authoritative next place to look
 *   { authRequired: null }                                    — genuinely ambiguous (network failure, or
 *                                                                some other status entirely) — the caller
 *                                                                falls back to well-known guessing rather
 *                                                                than guessing which way THIS resolves
 */
async function probeAuthorization(mcpUrl) {
  try {
    const initRes = await fetch(mcpUrl, {
      method: 'POST',
      headers: MCP_PROBE_HEADERS,
      body: JSON.stringify({
        jsonrpc: '2.0',
        id: 0,
        method: 'initialize',
        params: { protocolVersion: '2024-11-05', capabilities: {}, clientInfo: { name: 'jarvis', version: '1.0.0' } },
      }),
    });
    if (initRes.status === 401) return authRequiredFrom(initRes);
    if (!initRes.ok) return { authRequired: null };

    const sessionId = initRes.headers.get('Mcp-Session-Id');
    const listHeaders = sessionId ? { ...MCP_PROBE_HEADERS, 'Mcp-Session-Id': sessionId } : MCP_PROBE_HEADERS;
    const listRes = await fetch(mcpUrl, {
      method: 'POST',
      headers: listHeaders,
      body: JSON.stringify({ jsonrpc: '2.0', id: 1, method: 'tools/list', params: {} }),
    });
    if (listRes.status === 401) return authRequiredFrom(listRes);
    if (listRes.ok) return { authRequired: false };
    return { authRequired: null };
  } catch {
    return { authRequired: null };
  }
}

/**
 * First candidate URL that returns JSON actually matching `isValid`,
 * path-aware form first. `isValid` matters, not just "did it return JSON":
 * confirmed live against a real host that a catch-all route can answer a
 * WRONG well-known path with `200` and a parseable (but unrelated) JSON
 * body — without a shape check, that would satisfy the `||` chain below and
 * permanently block ever trying the correct URL, rather than being
 * correctly treated as a miss. See CLAUDE.md.
 */
async function fetchWellKnown(baseUrlObj, wellKnownName, isValid) {
  const path = baseUrlObj.pathname === '/' ? '' : baseUrlObj.pathname;
  const pathAware = `${baseUrlObj.origin}/.well-known/${wellKnownName}${path}`;
  const plainAppend = `${baseUrlObj.origin}/.well-known/${wellKnownName}`;
  for (const url of [pathAware, plainAppend]) {
    const doc = await tryFetchJson(url);
    if (doc && (!isValid || isValid(doc))) return doc;
  }
  return null;
}

const isProtectedResourceDoc = (doc) =>
  typeof doc.resource === 'string' || Array.isArray(doc.authorization_servers) || Array.isArray(doc.scopes_supported);
const isAuthServerDoc = (doc) => typeof doc.issuer === 'string' || (typeof doc.authorization_endpoint === 'string' && typeof doc.token_endpoint === 'string');

/** RFC 9728 §3.3: when protected-resource metadata came from a WWW-Authenticate-provided URL, its own `resource` field, if present, MUST exactly match the MCP URL that was actually requested — guards against a metadata document meant for a different resource being trusted. Lenient when the field is absent entirely (same looseness isProtectedResourceDoc already allows), since real servers vary in how strictly they include every RFC-listed field. Compares origin+path only, ignoring query/fragment: a saved connector URL often carries a query the canonical resource identifier doesn't (confirmed live — a real connector saved as ".../?src=settings" whose own metadata declares plain "https://mcp.lovable.dev"), and a full-string compare threw the correct, authoritative document away as a mismatch rather than accepting it. */
function resourceFieldMatches(meta, mcpUrl) {
  if (typeof meta.resource !== 'string') return true;
  const canonical = (u) => {
    try {
      const p = new URL(u);
      return `${p.origin}${p.pathname}`.replace(/\/$/, '');
    } catch {
      return String(u).replace(/\/$/, '');
    }
  };
  return canonical(meta.resource) === canonical(mcpUrl);
}

/**
 * RFC 9728: whether this MCP resource requires authorization at all, and if
 * so, which authorization server issues its tokens (plus the resource's own
 * declared scopes — used below in place of the AS's full advertised list, a
 * fresh client is far more likely to be granted what the RESOURCE says it
 * needs than everything the identity provider happens to support).
 *
 * Tries the real, spec-primary mechanism first — probeAuthorization()'s live
 * 401 + WWW-Authenticate — and only falls back to well-known-URL guessing
 * (the pre-existing mechanism, still spec-sanctioned as a fallback) when
 * that's unavailable: no WWW-Authenticate hint, a non-401/non-2xx response,
 * or a network failure reaching the server directly.
 */
async function discoverProtectedResource(mcpUrl) {
  const probe = await probeAuthorization(mcpUrl);

  if (probe.authRequired === true && probe.resourceMetadataUrl) {
    const meta = await tryFetchJson(probe.resourceMetadataUrl);
    if (meta && isProtectedResourceDoc(meta) && resourceFieldMatches(meta, mcpUrl)) {
      return {
        authRequired: true,
        issuer: meta.authorization_servers?.[0] || null,
        resourceId: meta.resource || mcpUrl,
        resourceScopes: meta.scopes_supported || null,
        challengeScope: probe.scope || null,
      };
    }
    // Present but unusable (malformed, or resource mismatch — possible
    // impersonation, RFC 9728 §3.3) — don't trust it; fall through below.
  }

  // Well-known lookup runs even when the probe saw NO 401 — a server that
  // PUBLISHES RFC 9728 protected-resource metadata is an OAuth-protected
  // resource by its own declaration, regardless of what an unauthenticated
  // request happened to answer. Confirmed live: Google's Gmail and Drive MCP
  // servers answer both `initialize` and `tools/list` anonymously (a
  // stateless server gating only actual tool calls, not the handshake) yet
  // both publish real protected-resource metadata — probeAuthorization()
  // alone reads that as "no authorization needed" and saves a connector that
  // looks connected and can never do anything (see CLAUDE.md).
  const resource = new URL(mcpUrl);
  const meta = await fetchWellKnown(resource, 'oauth-protected-resource', isProtectedResourceDoc);

  // Only "no 401 AND nothing published" still concludes no-auth. An
  // ambiguous probe (network failure, odd status) keeps the prior behavior
  // of assuming auth is required rather than guessing which way it resolves.
  if (!meta && probe.authRequired === false) {
    return { authRequired: false };
  }

  return {
    authRequired: true,
    issuer: meta?.authorization_servers?.[0] || null,
    resourceId: meta?.resource || mcpUrl,
    resourceScopes: meta?.scopes_supported || null,
    challengeScope: probe.authRequired === true ? probe.scope || null : null,
  };
}

/**
 * RFC 8414: the issuer's real authorize/token/registration endpoints.
 * Returns null (not a throw) on a missing/malformed document so
 * discoverAuthServer() can try another candidate before giving up.
 * Also enforces RFC 8414 §3.3: a returned `issuer` field must exactly match
 * the URL just asked for. Without this check, confirmed live against a real
 * server, a bare-origin guess can land on a DIFFERENT, unrelated
 * authorization server that just happens to also answer with valid-looking
 * (but wrong) metadata at that origin — Jarvis would silently build an
 * authorize URL against the wrong issuer instead of failing honestly. See
 * CLAUDE.md.
 */
async function discoverAuthorizationServer(issuerUrl) {
  const issuer = new URL(issuerUrl);
  const meta = await fetchWellKnown(issuer, 'oauth-authorization-server', isAuthServerDoc);
  if (!meta || !meta.authorization_endpoint || !meta.token_endpoint) return null;
  if (meta.issuer && meta.issuer.replace(/\/$/, '') !== issuer.toString().replace(/\/$/, '')) return null;
  return meta;
}

/**
 * Tries, in order: the issuer the resource explicitly named (always trusted
 * first when present); then the resource's OWN url, path included — a
 * path-scoped MCP server (.../mcp) very often hosts its OAuth server at
 * that same path, and this is the one candidate that actually gets a
 * path-aware well-known lookup; only then the resource's bare origin, as a
 * last resort. Previously jumped straight to the bare origin whenever the
 * resource didn't explicitly name an issuer, which — confirmed live against
 * a real server — can silently resolve to a different, unrelated
 * authorization server rather than either finding the right one or failing
 * honestly. See CLAUDE.md.
 */
async function discoverAuthServer(mcpUrl) {
  const discovery = await discoverProtectedResource(mcpUrl);
  if (!discovery.authRequired) return { authRequired: false };

  const { issuer, resourceId, resourceScopes, challengeScope } = discovery;
  const candidates = issuer ? [issuer] : [mcpUrl, new URL(mcpUrl).origin];
  for (const candidate of candidates) {
    const asMeta = await discoverAuthorizationServer(candidate);
    if (asMeta) return { authRequired: true, asMeta, resourceId: resourceId || mcpUrl, resourceScopes, challengeScope };
  }
  // Worded to invite a retry, not to sound permanent — confirmed live this can
  // be a transient discovery blip rather than a real, lasting incompatibility
  // (a real connector hit exactly this, then succeeded moments later).
  throw new Error(`${serviceName(mcpUrl)} didn't return the OAuth details Jarvis needs — this is sometimes temporary. Check the server address, then click Connect to try again.`);
}

/** Host name for a plain-language, service-specific message — never used to branch logic, only to word one. */
function serviceName(mcpUrl) {
  try {
    return new URL(mcpUrl).host;
  } catch {
    return 'This service';
  }
}

/** Whether this authorization server is set up for Client ID Metadata Documents (both flags, not just the CIMD one alone, since a server without a no-secret auth method can't actually use a public https-URL client either way). Read by obtainClientCredentials() to decide whether to attempt usableCimdClientId() at all, and used to word the manual-fallback hint below (a URL is a valid Client ID here, not just an opaque partner-issued one). */
function detectsCimdSupport(asMeta) {
  return Boolean(asMeta.client_id_metadata_document_supported && asMeta.token_endpoint_auth_methods_supported?.includes('none'));
}

/** A stored client_id is a CIMD identifier, not a traditional opaque one, exactly when it's an https URL — the spec's own distinguishing rule. Used to keep a CIMD client_id alive across an authorization-server change (portable by design) while still invalidating a traditional partner-issued id the same as before. */
function isCimdClientId(id) {
  return typeof id === 'string' && /^https:\/\//i.test(id);
}

/**
 * Posts one RFC 7591 registration request and classifies the outcome instead
 * of collapsing every failure into the same `null` — the original shape here
 * is exactly what let a real, informative rejection (e.g. Lovable's own
 * "Dynamic client registration is restricted to approved partners...") get
 * thrown away and replaced with a silent, unexplained switch to the
 * credentials form. See CLAUDE.md's Lovable investigation for the full story.
 *   {ok:true, clientId, clientSecret}          — registered successfully
 *   {ok:false, reason:'rejected', detail}        — the endpoint exists and said no;
 *                                                  `detail` is the provider's own
 *                                                  explanation when it gave one
 *   {ok:false, reason:'network', detail}         — couldn't even reach it
 */
async function postRegistration(endpoint, body) {
  let res;
  try {
    res = await fetch(endpoint, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
  } catch (err) {
    return { ok: false, reason: 'network', detail: err?.message || 'could not reach the server' };
  }
  if (!res.ok) {
    // Read the body defensively — some servers answer a rejection with a real
    // JSON error_description (Lovable does), others with plain text or nothing.
    let detail = `status ${res.status}`;
    try {
      const errBody = await res.json();
      detail = errBody?.error_description || errBody?.error || detail;
    } catch {
      // not JSON — keep the status-code fallback above
    }
    return { ok: false, reason: 'rejected', detail };
  }
  try {
    const okBody = await res.json();
    return { ok: true, clientId: okBody.client_id, clientSecret: okBody.client_secret || null };
  } catch {
    return { ok: false, reason: 'rejected', detail: 'the server accepted the request but sent back something unreadable' };
  }
}

/**
 * RFC 7591 Dynamic Client Registration — the spec-deprecated fallback
 * mechanism (see obtainClientCredentials()'s priority order below), tried
 * with one retry before being treated as a real refusal:
 *   {ok:false, reason:'unsupported'}  — no registration_endpoint at all; the
 *                                        only case where asking the user for
 *                                        manual credentials is the expected,
 *                                        non-error outcome
 * A public client, authenticated only by PKCE (token_endpoint_auth_method:
 * 'none') — correct for a locally-running app with no way to keep a secret
 * truly secret; confirmed live against Notion's real registration endpoint.
 * `application_type: 'native'` is the current spec's own requirement (Jarvis
 * is reached over 127.0.0.1, not a remote web origin — omitting it defaults
 * to "web" under OIDC, which can reject a loopback redirect URI). The spec
 * separately notes a client MAY retry with an adjusted body on a rejection;
 * a server that rejects an unrecognised member (like application_type)
 * rather than ignoring it looks identical to a real refusal without this,
 * so the plain body is tried once more before giving up.
 */
async function tryDynamicClientRegistration(asMeta) {
  if (!asMeta.registration_endpoint) return { ok: false, reason: 'unsupported' };
  const base = clientRegistrationBody();
  const withRedirect = { ...base, redirect_uris: [redirectUri()] };
  const first = await postRegistration(asMeta.registration_endpoint, { ...withRedirect, application_type: 'native' });
  if (first.ok || first.reason === 'network') return first;
  const second = await postRegistration(asMeta.registration_endpoint, withRedirect);
  return second.ok ? second : first; // first carries the more informative refusal text
}

/**
 * A CIMD client_id Jarvis can actually use with THIS server, or null.
 * Validated before use — https scheme with a real path, the served document's
 * own client_id equal to its URL, the required members present, and Jarvis's
 * real redirect URI listed — i.e. exactly the checks the authorization server
 * is about to run itself, done here first so a broken/unreachable document
 * falls through to DCR instead of failing the whole connection attempt.
 */
async function usableCimdClientId(asMeta) {
  if (!asMeta.client_id_metadata_document_supported) return null;
  const url = cimdUrl();
  if (!url) return null; // no publicly-reachable address configured — see client-identity.js
  try {
    const u = new URL(url);
    if (u.protocol !== 'https:' || u.pathname === '/') return null;
  } catch {
    return null;
  }
  const doc = await tryFetchJson(url);
  if (!doc || doc.client_id !== url) return null;
  if (!doc.client_name || !Array.isArray(doc.redirect_uris) || !doc.redirect_uris.includes(redirectUri())) return null;
  return url;
}

/**
 * The MCP client-registration spec's own stated priority order, applied
 * identically to every connector — nothing here reads or branches on any
 * service's name or domain, only on what the server itself publishes:
 *   1. pre-registered credentials — typed in this call, or stored from a
 *      past attempt (startConnect's manualClientId/storedClientId)
 *   2. Client ID Metadata Document, when the authorization server advertises
 *      support (`client_id_metadata_document_supported`) AND Jarvis has a
 *      publicly-reachable document to offer
 *   3. Dynamic Client Registration — spec-deprecated, still what most real
 *      servers offer today
 *   4. neither worked -> caller (startConnect) asks the user for a Client ID
 */
async function obtainClientCredentials(asMeta, manualClientId, manualClientSecret) {
  if (manualClientId) return { ok: true, clientId: manualClientId, clientSecret: manualClientSecret || null, via: 'preregistered' };
  const cimd = await usableCimdClientId(asMeta);
  if (cimd) return { ok: true, clientId: cimd, clientSecret: null, via: 'cimd' };
  const dcr = await tryDynamicClientRegistration(asMeta);
  return dcr.ok ? { ...dcr, via: 'dcr' } : dcr;
}

function buildAuthorizeUrl(asMeta, clientId, state, challenge, scopes, resource) {
  const url = new URL(asMeta.authorization_endpoint);
  url.searchParams.set('response_type', 'code');
  url.searchParams.set('client_id', clientId);
  url.searchParams.set('redirect_uri', redirectUri());
  url.searchParams.set('state', state);
  url.searchParams.set('code_challenge', challenge);
  url.searchParams.set('code_challenge_method', 'S256');
  // Prefer the protected resource's own declared scopes over the authorization
  // server's full advertised list (asMeta.scopes_supported can be much wider —
  // Lovable's AS advertises 10 scopes spanning every resource it issues tokens
  // for, not just this one) — a fresh client is far more likely to actually be
  // granted what the resource itself says it needs.
  const scopeList = scopes?.length ? scopes : asMeta.scopes_supported;
  if (scopeList?.length) url.searchParams.set('scope', scopeList.join(' '));
  // RFC 8707 resource indicator — required by the MCP authorization spec and
  // expected by a spec-current server (Lovable advertises
  // authorization_response_iss_parameter_supported and CIMD support, both
  // signs of a current implementation).
  if (resource) url.searchParams.set('resource', resource);
  return url.toString();
}

/**
 * Turns a classified obtainClientCredentials() failure into a plain-language,
 * service-specific explanation plus a small structured hint the UI persists
 * and re-shows (connector.config.connectFlow.manualClient) — never a generic
 * "needs a Client ID" sentence for every connector regardless of why. Reads
 * only the failure's own classification and the server's own advertised
 * flags; no service name or catalog id is ever branched on.
 *
 * Purely informational wording, on purpose — no "enter it below"/"click
 * Connect again" call to action. There is no field anywhere in the app to
 * enter a Client ID into (the user's own explicit decision — see CLAUDE.md's
 * "No Client ID/Secret UI anywhere"); telling the user to do something the
 * screen can't actually do would be worse than saying nothing.
 */
function manualClientHint(failure, mcpUrl, supportsCimd) {
  const host = serviceName(mcpUrl);
  if (failure.reason === 'unsupported') {
    return {
      reason: 'unsupported',
      detail: null,
      supportsCimd,
      message: `${host} doesn't hand out app credentials automatically — it needs a Client ID registered with it directly, which isn't something this screen can collect.`,
    };
  }
  if (failure.reason === 'network') {
    return {
      reason: 'network',
      detail: failure.detail || null,
      supportsCimd,
      message: `Jarvis couldn't reach ${host} to register automatically${failure.detail ? ` (${failure.detail})` : ''}. Click Connect to try again.`,
    };
  }
  return {
    reason: 'rejected',
    detail: failure.detail || null,
    supportsCimd,
    message: `${host} declined to register Jarvis automatically${failure.detail ? `: ${failure.detail}` : '.'}`,
  };
}

/**
 * Starts a connection: discovers the server's OAuth setup, obtains client
 * credentials, and returns the URL the browser should open.
 * `manualClientId`/`manualClientSecret` come from the "Set up manually"
 * fields — used when DCR isn't available. A `manualClientId` can be either a
 * traditional opaque client id issued by the provider, OR an `https://` URL
 * to a Client ID Metadata Document — both are just a string handed to the
 * provider as `client_id`, so no special-casing is needed here.
 *
 * When neither is passed, credentials saved from an earlier attempt on this
 * same connector (typed in before, issued by a prior successful DCR, or a
 * CIMD URL) are reused automatically, so finishing authentication later, or
 * retrying, never re-registers a new client at the provider or makes the
 * user retype anything.
 *
 * When `discoverAuthServer` finds that this server needs no authorization at
 * all (root cause of "some servers enter an OAuth configuration flow when
 * they should not" — see CLAUDE.md), the connector is marked usable
 * immediately and `{noAuthNeeded: true}` is returned — no browser tab, no
 * credentials, nothing else.
 *
 * There is deliberately no permanent "this connector needs manual
 * credentials" flag any more — every call attempts the full chain (CIMD,
 * then DCR) fresh unless credentials are already on hand. Obtaining a client
 * automatically can fail in several ways (no registration endpoint at all, a
 * real refusal, a network failure) but NONE of them throw or block the
 * connector any more: every failure returns `{needsManualClient: true, reason,
 * detail, message, supportsCimd}` — a real, specific, service-worded
 * explanation (persisted on the connector as `connectFlow.manualClient` so it
 * survives a page reload) plus an invitation to supply a Client ID or CIMD
 * URL. A past failure never forecloses trying again — the very next Connect
 * click re-attempts CIMD and DCR from scratch. The one remaining throw is
 * `discoverAuthServer`'s own: no usable OAuth metadata published at all,
 * where no Client ID could help since there's no authorize endpoint to send
 * one to.
 */
export async function startConnect(connectorId, mcpUrl, { manualClientId, manualClientSecret } = {}) {
  const discovery = await discoverAuthServer(mcpUrl);
  const connector = getConnector(connectorId);
  const existingFlow = connector?.config?.connectFlow || {};

  if (!discovery.authRequired) {
    // A real, unauthenticated request against the MCP server itself
    // succeeded — this connector needs no OAuth to be usable.
    updateConnector(connectorId, {
      config: { connectFlow: { ...existingFlow, kind: 'none' } },
      status: { state: 'working', checkedAt: new Date().toISOString(), detail: null },
    });
    return { noAuthNeeded: true };
  }

  const { asMeta, resourceId, resourceScopes, challengeScope } = discovery;

  // Current spec: PKCE support must be VERIFIED, not assumed — a client MUST
  // refuse to proceed if the authorization server doesn't advertise it,
  // rather than optimistically sending a code_challenge it can't be sure
  // will be honored (which would silently downgrade a security control
  // Jarvis's whole flow depends on, not just fail loudly later).
  if (!asMeta.code_challenge_methods_supported?.length) {
    throw new Error("This server's OAuth setup doesn't support PKCE, which Jarvis requires for a secure connection — it can't be set up automatically.");
  }

  // Persisted purely as a hint for enriching a manual-fallback message below
  // (a CIMD URL is usable here vs. only a traditional partner-issued id) —
  // never gates or skips anything on its own.
  const supportsCimd = detectsCimdSupport(asMeta);

  // Current spec: credentials are bound to the authorization server that
  // issued them — if the resource's declared AS has changed since a client
  // id was last stored for this connector, that credential belongs to a
  // DIFFERENT issuer and must not be reused against this one. Exception: a
  // CIMD client_id (an https URL) is explicitly spec'd as portable across
  // authorization servers ("Client IDs based on Client ID Metadata Documents
  // are portable... No re-registration is needed when the authorization
  // server changes") — an issuer change must not wipe one of those.
  const issuerChanged = Boolean(existingFlow.issuer && asMeta.issuer && existingFlow.issuer !== asMeta.issuer);
  const storedIsCimd = isCimdClientId(existingFlow.clientId);
  const storedClientId = issuerChanged && !storedIsCimd ? null : existingFlow.clientId || null;
  const storedClientSecret = storedClientId ? getSecret(`connclient_${connectorId}`) : null;
  const effectiveClientId = manualClientId || storedClientId;
  // Relies on the caller (server.js's POST /:id/connect) rejecting a
  // manualClientSecret with no manualClientId before this ever runs — a
  // secret sent without an id would otherwise silently fall through to
  // storedClientSecret and the typed value would vanish with no error.
  const effectiveClientSecret = manualClientId ? manualClientSecret : storedClientSecret;

  const credentials = await obtainClientCredentials(asMeta, effectiveClientId, effectiveClientSecret);

  if (!credentials.ok) {
    // Every way of obtaining a client automatically can fail — no
    // registration_endpoint at all, a real refusal, or a network failure —
    // and every one of them now ends the same way: nothing is blocked, the
    // service's own reason is recorded and reported, and the user can supply
    // a Client ID (or a CIMD URL) to finish connecting. The next Connect
    // click retries the full chain (CIMD, then DCR) from scratch — a past
    // failure never forecloses trying again. This is the direct fix for a
    // real, live bug: Lovable's own server explains exactly why it refused
    // ("restricted to approved partners... or use the
    // client_id_metadata_document flow instead") and that explanation used
    // to be thrown away along with the whole connection attempt.
    const hint = manualClientHint(credentials, mcpUrl, supportsCimd);
    updateConnector(connectorId, { config: { connectFlow: { ...existingFlow, supportsCimd, manualClient: hint } } });
    return { needsManualClient: true, ...hint };
  }

  // One patch on success — also clears any stale manual-fallback hint from a
  // past failed attempt, and records which mechanism actually worked
  // (useful for support/debugging, never read by any decision here).
  const flowPatch = { ...existingFlow, supportsCimd, manualClient: null, credentialSource: credentials.via };
  const newClientId = manualClientId || (!storedClientId ? credentials.clientId : null);
  if (newClientId) {
    flowPatch.clientId = newClientId;
    flowPatch.issuer = asMeta.issuer || null;
  }
  updateConnector(connectorId, { config: { connectFlow: flowPatch } });
  const secretToSave = manualClientId ? manualClientSecret : (!storedClientId ? credentials.clientSecret : null);
  if (secretToSave) saveSecret(`connclient_${connectorId}`, secretToSave);

  const { verifier, challenge } = newPkcePair();
  const state = base64url(crypto.randomBytes(16));
  // WWW-Authenticate's own `scope` (what THIS specific request needs) beats
  // the resource's general scopes_supported, which in turn beats the
  // authorization server's full advertised list — see buildAuthorizeUrl.
  const scopeList = challengeScope ? challengeScope.split(/\s+/).filter(Boolean) : resourceScopes;
  const resource = resourceId || mcpUrl;

  savePendingFlow(state, {
    connectorId,
    verifier,
    clientId: credentials.clientId,
    clientSecret: credentials.clientSecret,
    tokenEndpoint: asMeta.token_endpoint,
    resource,
    // RFC 9207 mix-up-attack protection (current spec) — recorded now, while
    // the discovery that produced them is still trusted, so handleCallback
    // can validate the redirect actually came back from the SAME
    // authorization server this flow was started against.
    issuer: asMeta.issuer || null,
    issSupported: Boolean(asMeta.authorization_response_iss_parameter_supported),
    expiresAt: Date.now() + PENDING_FLOW_TTL_MS,
  });

  return { authUrl: buildAuthorizeUrl(asMeta, credentials.clientId, state, challenge, scopeList, resource) };
}

async function exchangeCodeForTokens(flow, code) {
  const body = new URLSearchParams({
    grant_type: 'authorization_code',
    code,
    redirect_uri: redirectUri(),
    client_id: flow.clientId,
    code_verifier: flow.verifier,
  });
  if (flow.clientSecret) body.set('client_secret', flow.clientSecret);
  if (flow.resource) body.set('resource', flow.resource); // RFC 8707 — same reasoning as buildAuthorizeUrl above

  const res = await fetch(flow.tokenEndpoint, {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body,
  });
  if (!res.ok) throw new Error(`The service would not complete the connection (${res.status}).`);
  return res.json();
}

function tokenSetFrom(tokens, flow) {
  return {
    accessToken: tokens.access_token,
    refreshToken: tokens.refresh_token || null,
    expiresAt: tokens.expires_in ? Date.now() + tokens.expires_in * 1000 : null,
    tokenEndpoint: flow.tokenEndpoint,
    clientId: flow.clientId,
    clientSecret: flow.clientSecret || null,
    resource: flow.resource || null,
  };
}

/** Handles the redirect back from the provider (server.js's GET /api/connectors/oauth/callback). Exchanges the code for tokens and saves them. Never throws — returns {ok, error?, connectorId?} so the route can show a plain page either way and, on failure, record which connector to mark as errored (see server.js's callback route). */
export async function handleCallback(query) {
  const { code, state, error, error_description: errorDescription, iss } = query;
  const flow = state ? getPendingFlow(state) : null;

  if (error) {
    if (flow) deletePendingFlow(state);
    return { ok: false, error: errorDescription || `The service refused to connect: ${error}`, connectorId: flow?.connectorId || null };
  }

  if (!flow) return { ok: false, error: 'This sign-in link is invalid or was already used.', connectorId: null };
  deletePendingFlow(state); // one-time use either way

  if (Date.now() > flow.expiresAt) {
    return { ok: false, error: 'This sign-in link expired — go back and click Connect again.', connectorId: flow.connectorId };
  }

  // RFC 9207 mix-up-attack protection (current MCP spec): if the authorization
  // server told us ahead of time it always includes `iss`, a response
  // missing it is rejected outright; whenever `iss` IS present, it must
  // match — plain string comparison, no normalization — the issuer this
  // specific flow was actually started against, not just any issuer.
  if (flow.issSupported && !iss) {
    return { ok: false, error: 'This sign-in response is missing required security information — go back and click Connect again.', connectorId: flow.connectorId };
  }
  if (iss && flow.issuer && iss !== flow.issuer) {
    return { ok: false, error: 'This sign-in response does not match the service Jarvis was connecting to — go back and click Connect again.', connectorId: flow.connectorId };
  }

  if (!code) {
    return { ok: false, error: 'This sign-in response is missing the required code — go back and click Connect again.', connectorId: flow.connectorId };
  }

  let tokens;
  try {
    tokens = await exchangeCodeForTokens(flow, code);
  } catch (err) {
    return { ok: false, error: err?.message || 'Could not finish connecting to the service.', connectorId: flow.connectorId };
  }

  const secretRef = `connoauth_${flow.connectorId}`;
  saveSecret(secretRef, JSON.stringify(tokenSetFrom(tokens, flow)));

  try {
    updateConnector(flow.connectorId, {
      config: { secretRef },
      status: { state: 'working', checkedAt: new Date().toISOString(), detail: null },
    });
  } catch (err) {
    return { ok: false, error: `Connected, but could not save it: ${err.message}`, connectorId: flow.connectorId };
  }

  return { ok: true, connectorId: flow.connectorId };
}

async function refreshTokenSet(tokenSet) {
  if (!tokenSet.refreshToken) throw new Error('This connection expired and has no way to refresh itself — reconnect it.');
  const body = new URLSearchParams({
    grant_type: 'refresh_token',
    refresh_token: tokenSet.refreshToken,
    client_id: tokenSet.clientId,
  });
  if (tokenSet.clientSecret) body.set('client_secret', tokenSet.clientSecret);
  if (tokenSet.resource) body.set('resource', tokenSet.resource); // RFC 8707

  const res = await fetch(tokenSet.tokenEndpoint, {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body,
  });
  if (!res.ok) throw new Error('This connection expired and could not be refreshed — reconnect it.');
  const fresh = await res.json();
  return {
    ...tokenSet,
    accessToken: fresh.access_token,
    refreshToken: fresh.refresh_token || tokenSet.refreshToken,
    expiresAt: fresh.expires_in ? Date.now() + fresh.expires_in * 1000 : null,
  };
}

/** A valid access token for a connected connector, refreshing first if it's expired (or about to be, within 30s). Returns null for a connector verified (via probeAuthorization, at connect time) to need no authorization at all — that's an expected, non-error state, not something with a token to fetch. Throws a plain-language error if there's nothing to refresh with. */
export async function getAccessToken(connectorId) {
  const connector = getConnector(connectorId);
  if (connector?.config?.connectFlow?.kind === 'none') return null;
  const ref = connector?.config?.secretRef;
  if (!ref) throw new Error('This connector is not connected yet.');
  const raw = getSecret(ref);
  if (!raw) throw new Error('This connector is not connected yet.');

  let tokenSet;
  try {
    tokenSet = JSON.parse(raw);
  } catch {
    throw new Error('This connection is in a bad state — reconnect it.');
  }

  if (tokenSet.expiresAt && Date.now() > tokenSet.expiresAt - TOKEN_REFRESH_SLACK_MS) {
    tokenSet = await refreshTokenSet(tokenSet);
    saveSecret(ref, JSON.stringify(tokenSet));
  }

  return tokenSet.accessToken;
}

/** Disconnects a connector — deletes its stored token set. The connector record itself is untouched (caller decides whether to also remove it entirely). */
export function disconnect(connectorId) {
  const connector = getConnector(connectorId);
  if (connector?.config?.secretRef) deleteSecret(connector.config.secretRef);
}

/**
 * Re-checks every connector currently saved as needing no authorization
 * (`connectFlow.kind: 'none'`): if its server actually publishes RFC 9728
 * protected-resource metadata, the original conclusion was wrong (see
 * discoverProtectedResource()'s header comment — a real example, verified
 * live: Google's Gmail/Drive MCP servers answer both `initialize` and
 * `tools/list` anonymously yet both publish real protected-resource
 * metadata) and the connector is demoted back to needing a real connect —
 * otherwise a connector saved wrong before this fix existed shows
 * "working" with a tools list that will always be empty, with no path back
 * to Connect (the detail page only shows the tools view for a 'working'
 * connector). Called once, best-effort, at server startup — never blocks it;
 * any per-connector failure is skipped rather than aborting the whole pass.
 */
export async function recheckNoAuthConnectors() {
  for (const c of listConnectors()) {
    if (c.type !== 'mcp' || c.config?.connectFlow?.kind !== 'none') continue;
    const url = c.config.connectFlow.url;
    if (!url) continue;
    try {
      const { authRequired } = await discoverProtectedResource(url);
      if (!authRequired) continue;
      updateConnector(c.id, { config: { connectFlow: { ...c.config.connectFlow, kind: 'oauth_dcr' } } });
      recordStatus(c.id, 'error', 'This service needs you to sign in — click Connect.');
    } catch {
      // Best-effort — leave this connector exactly as it was and move on.
    }
  }
}

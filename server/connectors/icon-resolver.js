// Automatic, general icon resolution — for EVERY connector, catalog or
// custom alike, not just unknown ones. Originally built only for custom
// connectors (leaving the catalog's 5 official entries as hand-drawn SVGs)
// — corrected after the user pointed out those hand-drawn marks were
// themselves already stale (Slack/Drive/Gmail's real logos had moved on)
// and asked for one real auto-updating system instead of two.
//
// Two separate resolution paths, not one, after a second real gap the user
// found: a catalog entry the user has never clicked "Connect" on has no
// connector record at all (nothing exists yet to cache an icon ON), so
// resolving/caching per-connector — this file's original design — silently
// never ran for anything still sitting in the Browse-connectors directory,
// which is exactly the most visible place to see a wrong icon. A catalog
// entry's icon never actually needed a connector record to resolve in the
// first place (its domain/URL is known from catalog.json alone, connected
// or not), so catalog icons now resolve into their OWN small independent
// cache (CATALOG_ICON_CACHE_FILE below) instead:
//
//   - resolveCatalogIcons() / getCatalogIcon() — every one of Jarvis's
//     bundled catalog entries, regardless of connection status.
//   - resolveConnectorIcon() / backfillConnectorIcons() — custom mcp/api
//     connectors only now; a real connector record is genuinely required
//     here, since a custom connector's own address IS what's being resolved.
//
// Resolution order for a CUSTOM connector, most authoritative first:
//   1. The MCP server's OWN declared icon, from its `initialize` response's
//      `serverInfo.icons` — an optional, fairly new part of the MCP spec,
//      custom mcp connectors only. Not every real server populates it yet;
//      when one does, it's more authoritative than guessing from a domain.
//   2. A real favicon for the connector's own domain, via DuckDuckGo's
//      public icon endpoint (no key needed) — verified live against
//      lovable.dev and composio.dev before this was written; both returned
//      genuine brand marks, and a domain with nothing available returns a
//      clean 404 rather than a disguised placeholder, so this can reliably
//      tell "found one" from "found nothing." The domain comes from the
//      connector's own configured address, retrying against the domain's
//      apex (e.g. "connect.composio.dev" -> "composio.dev") since an
//      API/MCP subdomain often has no favicon of its own even when the
//      marketing root does.
//   3. null — the caller (server.js) keeps whatever it already had rather
//      than losing an icon to a transient failure.
//
// A catalog entry's own resolution (CATALOG_DOMAINS / CATALOG_ICON_URLS
// below) is simpler and needs no MCP handshake at all — see
// resolveCatalogIcons() further down.
//
// Every step here is best-effort and never throws: a missing or ugly icon
// must never block a connector from actually working, and a fetch failure
// here is exactly as harmless as one never having been attempted.
//
// A CLI connector (a bare command name like "git") has no domain concept
// at all — there is no automatic source for one, and this file doesn't
// pretend otherwise; resolveConnectorIcon() returns null for cli immediately.
//
// Kept fresh, not just fetched once: a cached icon older than
// ICON_MAX_AGE_MS is treated the same as a missing one everywhere below —
// an occasional real re-check, not a one-time snapshot that could itself
// go stale the same way the old hand-drawn marks did.

import { getServerInfo, listTools as listRemoteTools } from './mcp-remote-client.js';
import { listTools as listStdioTools } from './mcp-client.js';
import { listConnectors, updateConnector } from './store.js';
import { listCatalog } from './get-catalog.js';
import { readJson, writeJson } from '../store.js';

const FETCH_TIMEOUT_MS = 5000;
// Real examples fetched during design were both under 2KB; generous but
// bounded so a misbehaving endpoint can't hand back something huge.
const MAX_ICON_BYTES = 200 * 1000;
// How long a resolved icon is trusted before this file tries again to pick
// up a real-world rebrand — not aggressive (no user is refreshing a tab
// fast enough to notice the difference between this and "instant"), just
// not "forever."
const ICON_MAX_AGE_MS = 30 * 24 * 60 * 60 * 1000; // 30 days

// Jarvis's own bundled catalog is small (5 entries) and stable enough that
// hand-picking each one's real marketing domain once is reasonable — unlike
// hand-drawing the logo itself, a domain rarely changes, and even if one
// did this is a one-line update instead of re-tracing an SVG. Keys match
// catalog.json's own `icon` field / _connector-icons.js's ICONS map.
// Verified live against each real domain before shipping: notion.so,
// github.com, and slack.com all return that service's own distinctive
// favicon (Slack's fetched result is even MORE current than this file's
// old hand-drawn fallback — a real multi-colour swirl, not a flat single
// colour). Gmail and Google Drive are deliberately NOT here — verified
// live that gmail.com/drive.google.com/mail.google.com/workspace.google.com
// all return the exact same generic Google "G", not a per-product icon;
// see CATALOG_ICON_URLS below for how those two are actually resolved.
const CATALOG_DOMAINS = {
  notion: 'notion.so',
  github: 'github.com',
  slack: 'slack.com',
};

// Gmail and Google Drive specifically: Google doesn't serve a distinct
// favicon per product (see CATALOG_DOMAINS' comment above), so a curated
// direct asset URL is fetched instead of a guessed domain — still fetched
// and cached the same way as everything else, still re-checked on the same
// staleness timer, just pointed at a specific known-good asset rather than
// inferred. Wikimedia Commons was chosen for being freely-licensed and
// directly fetchable; both were verified live while building this — and
// verifying live is what caught that Google Drive's real logo changed on
// May 19, 2026 (a genuine, recent rebrand: a gradient-hexagon mark,
// nothing like the older flat triangle), the exact kind of drift a
// hand-drawn SVG would have silently kept showing.
const CATALOG_ICON_URLS = {
  gmail: 'https://upload.wikimedia.org/wikipedia/commons/7/7e/Gmail_icon_%282020%29.svg',
  'google-drive': 'https://upload.wikimedia.org/wikipedia/commons/5/5f/Google_Drive_icon_%282026%29.svg',
};

export function isIconStale(fetchedAt) {
  if (!fetchedAt) return true;
  return Date.now() - new Date(fetchedAt).getTime() > ICON_MAX_AGE_MS;
}

function hostnameOf(urlStr) {
  try {
    return new URL(urlStr).hostname;
  } catch {
    return null;
  }
}

// "connect.composio.dev" -> "composio.dev" — a plain last-two-labels
// heuristic. Doesn't handle multi-part ccTLDs correctly (e.g.
// "example.co.uk" would reduce to "co.uk", not "example.co.uk") — a full
// public-suffix-list would, but that's a real new dependency for what's
// already just an optional fallback step. Noted honestly rather than
// silently wrong for that narrower case; the common .com/.dev/.io/.ai
// shape real API/MCP vendors overwhelmingly use is unaffected.
function apexDomain(hostname) {
  const parts = hostname.split('.');
  return parts.length > 2 ? parts.slice(-2).join('.') : hostname;
}

async function fetchImageDataUri(url, fallbackMimeType) {
  try {
    const res = await fetch(url, { signal: AbortSignal.timeout(FETCH_TIMEOUT_MS) });
    if (!res.ok) {
      console.warn(`[icons] ${url} -> HTTP ${res.status}`);
      return null;
    }
    const contentType = res.headers.get('content-type') || fallbackMimeType || 'image/x-icon';
    if (!contentType.startsWith('image/')) {
      console.warn(`[icons] ${url} -> not an image (content-type: ${contentType})`);
      return null; // a 200 that isn't actually an image (e.g. an HTML error page) is a soft no
    }
    const buf = Buffer.from(await res.arrayBuffer());
    if (!buf.length || buf.length > MAX_ICON_BYTES) {
      console.warn(`[icons] ${url} -> bad size (${buf.length} bytes)`);
      return null;
    }
    return `data:${contentType};base64,${buf.toString('base64')}`;
  } catch (err) {
    // network failure, timeout, bad host — always a soft no, never thrown,
    // but logged: silent failure here is exactly what made a real live
    // failure impossible to diagnose once already.
    console.warn(`[icons] ${url} -> ${err?.name || 'error'}: ${err?.message || err}`);
    return null;
  }
}

async function faviconForDomain(host) {
  if (!host) return null;
  const exact = await fetchImageDataUri(`https://icons.duckduckgo.com/ip3/${host}.ico`);
  if (exact) return exact;
  const apex = apexDomain(host);
  if (apex === host) return null;
  return fetchImageDataUri(`https://icons.duckduckgo.com/ip3/${apex}.ico`);
}

function faviconForAddress(address) {
  return faviconForDomain(hostnameOf(address));
}

/** The MCP server's own declared icon, if it provided one. Only meaningful right after a real initialize() has actually run for this connector this session (verifyConnectorIsUsable()'s listTools() call already does this before resolveConnectorIcon() is ever called) — reads that cached result, no extra round trip of its own beyond fetching the declared icon URL itself. */
async function mcpDeclaredIcon(connector) {
  if (connector.config?.connectFlow?.kind === 'stdio') return null; // internal plumbing only, never a custom connector's own flow
  const info = getServerInfo(connector.id);
  const src = info?.icons?.[0]?.src;
  if (!src || typeof src !== 'string') return null;
  if (src.startsWith('data:')) return src; // already inline, nothing to fetch
  return fetchImageDataUri(src, info.icons[0]?.mimeType);
}

/**
 * Best-effort icon resolution for a CUSTOM mcp/api connector — returns a
 * `data:` URI to cache on the connector's own record, or null if nothing
 * was found this attempt (caller keeps whatever it already had rather than
 * treating a transient failure as "no icon"). Never throws. Catalog
 * connectors never reach this any more — see resolveCatalogIcons() below,
 * which needs no connector record to exist at all.
 */
export async function resolveConnectorIcon(connector) {
  if (connector.type === 'mcp') {
    const declared = await mcpDeclaredIcon(connector);
    return declared || faviconForAddress(connector.config?.connectFlow?.url);
  }
  if (connector.type === 'api') {
    return faviconForAddress(connector.config?.baseUrl);
  }
  return null; // cli — no domain to ask, see this file's header comment
}

/**
 * Catch-up pass for any CUSTOM (non-catalog) connector missing an icon, or
 * whose cached one has aged past ICON_MAX_AGE_MS — called once at server
 * startup, fire-and-forget (server.js doesn't await this; a slow or
 * unreachable server here must never delay Jarvis actually starting).
 *
 * Deliberately NOT restricted to connectors already confirmed `working` —
 * a real gap found live (a connector added but never yet successfully
 * connected got no icon at all, indefinitely). The favicon-by-domain path
 * inside resolveConnectorIcon() only needs the address the user already
 * typed in when adding the connector; it doesn't touch the service itself
 * at all, so it needs no working connection any more than
 * resolveCatalogIcons() below needs one. What DOES still require a real
 * connection — the MCP server's own declared-icon check — stays gated on
 * `status.state === 'working'` right here, inside the loop: this never
 * attempts a fresh, possibly-unauthenticated listTools()/initialize() call
 * purely to fetch an icon; an untested connector just falls straight
 * through resolveConnectorIcon() to the favicon fallback, which already
 * handles an empty serverInfo cache gracefully with no code change needed
 * there. A failed attempt leaves `iconFetchedAt` untouched on purpose, so
 * the next startup or /test call retries rather than waiting out the full
 * staleness window on what might have been a one-off network blip.
 */
export async function backfillConnectorIcons() {
  const candidates = listConnectors().filter(
    (c) => c.source?.type !== 'catalog' && (c.type === 'mcp' || c.type === 'api') && (!c.iconDataUri || isIconStale(c.iconFetchedAt))
  );
  console.log('[icons] backfillConnectorIcons() starting —', candidates.length, 'candidate connector(s)');
  for (const connector of candidates) {
    try {
      if (connector.type === 'mcp' && connector.status?.state === 'working') {
        const flow = connector.config?.connectFlow;
        if (flow?.kind === 'stdio') await listStdioTools(connector.id, flow);
        else await listRemoteTools(connector.id, flow?.url);
      }
      const icon = await resolveConnectorIcon(connector);
      if (icon) {
        updateConnector(connector.id, { iconDataUri: icon, iconFetchedAt: new Date().toISOString() });
        console.log(`[icons] resolved connector "${connector.label}" (${connector.id})`);
      } else {
        console.warn(`[icons] backfill: nothing found for connector "${connector.label}" (${connector.id})`);
      }
    } catch (err) {
      // One connector being temporarily unreachable (or anything else going
      // wrong) must never stop the rest of the backfill from running — but
      // logged, not silent, so a real failure is actually diagnosable.
      console.warn(`[icons] backfill failed for connector "${connector.label}" (${connector.id}):`, err?.message || err);
    }
  }
}

// ---------- catalog icons — independent of any connector record ----------
// A catalog entry's icon is knowable straight from catalog.json (its
// CATALOG_DOMAINS/CATALOG_ICON_URLS entry above) whether or not the user
// has ever clicked into it — unlike a custom connector, nothing about
// resolving one requires a live connection to exist first. Cached
// separately from connectors.json for exactly that reason: one small file,
// one entry per catalog id, entirely decoupled from connection status.

const CATALOG_ICON_CACHE_FILE = 'catalog-icons';

function loadCatalogIconCache() {
  return readJson(CATALOG_ICON_CACHE_FILE, {});
}

/** `data:` URI for a bundled catalog entry, or null if never successfully resolved yet — used by server.js's /api/connectors/catalog route and publicConnector() so a catalog service's icon reads the same whether or not it's been connected. */
export function getCatalogIcon(catalogId) {
  return loadCatalogIconCache()[catalogId]?.iconDataUri || null;
}

/**
 * Resolves (or re-checks, past ICON_MAX_AGE_MS) every bundled catalog
 * entry's icon, independent of whether the user has connected any of
 * them — called once at server startup, fire-and-forget, same reasoning
 * as backfillConnectorIcons() (must never delay startup, never throws). A
 * failed attempt leaves that entry's cached value (and its `fetchedAt`)
 * untouched, same "retry next time rather than go blank" behavior as the
 * connector-icon path.
 */
export async function resolveCatalogIcons() {
  console.log('[icons] resolveCatalogIcons() starting —', listCatalog().length, 'catalog entries');
  const cache = loadCatalogIconCache();
  let changed = false;
  for (const entry of listCatalog()) {
    const cached = cache[entry.id];
    if (cached?.iconDataUri && !isIconStale(cached.fetchedAt)) continue;
    try {
      const curatedUrl = CATALOG_ICON_URLS[entry.id];
      const icon = curatedUrl ? await fetchImageDataUri(curatedUrl) : await faviconForDomain(CATALOG_DOMAINS[entry.id]);
      if (icon) {
        cache[entry.id] = { iconDataUri: icon, fetchedAt: new Date().toISOString() };
        changed = true;
        console.log(`[icons] resolved ${entry.id}`);
      } else {
        console.warn(`[icons] resolveCatalogIcons: nothing found for ${entry.id}`);
      }
    } catch (err) {
      // Same rule as everywhere else in this file — one entry failing must
      // never stop the rest, and never clears what was already cached —
      // but logged, not silent.
      console.warn(`[icons] resolveCatalogIcons failed for ${entry.id}:`, err?.message || err);
    }
  }
  if (changed) writeJson(CATALOG_ICON_CACHE_FILE, cache);
  console.log('[icons] resolveCatalogIcons() done, changed:', changed);
}

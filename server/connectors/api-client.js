// The API-key mechanism: a plain REST/HTTP service reached with `fetch()`
// and a key injected server-side, never seen by the model. One of Jarvis's
// three peer integration mechanisms (alongside mcp and cli) — see
// connectors/store.js's header comment.
//
// Tools ("operations") come from one of two places, both ending in the same
// shape (`{name, description, method, path, parameters}`, saved into
// `connector.config.operations`):
//   - discoverFromSpec(specUrl) — parses a real OpenAPI/Swagger JSON document
//     into a full proposed list; the user reviews and ticks which to enable
//     before anything is saved (server.js's route does the ticking/saving,
//     this file only does the parsing).
//   - a manually-added operation the user typed in one at a time (built the
//     same shape directly by the App Control form, no parsing involved).
// Either way, `toolDeclarations()`/`dispatch()` below only ever look at the
// saved `operations` array — they don't care where it came from.

import { getSecret } from '../config.js';

const HTTP_METHODS = ['get', 'post', 'put', 'patch', 'delete'];

// Real bug found in live testing: a Swagger/OpenAPI operationId is very
// often camelCase ("addPet", "updatePet", "findPetsByStatus" — confirmed
// against a real spec) — lowercasing FIRST, before ever looking for a word
// boundary, merges "updatePet" into the single indivisible blob "updatepet".
// That's cosmetic for display, but it's a real safety regression: guard.js's
// risk classifier tokenizes on exact words, so "updatepet" as one token
// never matches the keyword "update" the way "update_pet" (two tokens)
// would — a genuinely risky operation would have silently stopped being
// classified as risky. Camel-case boundaries are split into their own
// underscore-separated word BEFORE lowercasing, same fix shape as
// `_connector-detail.js`'s `groupFor()` and `guard.js`'s own tokenizer.
function sanitizeName(text) {
  return String(text || '')
    .replace(/([a-z0-9])([A-Z])/g, '$1_$2')
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '_')
    .replace(/^_+|_+$/g, '')
    .slice(0, 60) || 'operation';
}

/** Pulls `{name, in: 'path'|'query', required}` out of an OpenAPI operation's own `parameters` array — path/query params only; request bodies are handled separately below since they're a single JSON blob, not individually named. */
function paramsFromSpec(opParams) {
  return (opParams || [])
    .filter((p) => p.in === 'path' || p.in === 'query')
    .map((p) => ({ name: p.name, in: p.in, required: Boolean(p.required), description: p.description || '' }));
}

/**
 * Parses a real OpenAPI/Swagger JSON document's `paths` into a proposed list
 * of operations — one per method+path — for the user to review and tick.
 * JSON only: a YAML spec is a real possibility for some services, but this
 * project has no YAML parser and adding one is out of scope for a single
 * connector type — reported plainly rather than half-parsed, since a wrong
 * parse would be worse than an honest "can't read this format" here.
 */
export async function discoverFromSpec(specUrl) {
  let res;
  try {
    res = await fetch(specUrl);
  } catch (err) {
    throw new Error(`Couldn't reach that address: ${err?.message || 'unknown error'}.`);
  }
  if (!res.ok) throw new Error(`That address responded with an error (${res.status}).`);

  const text = await res.text();
  let spec;
  try {
    spec = JSON.parse(text);
  } catch {
    throw new Error(
      "That doesn't look like a JSON OpenAPI/Swagger document — a YAML spec isn't supported yet. " +
        'Look for a "/openapi.json" or similar JSON address for this service, or add endpoints by hand instead.'
    );
  }
  if (!spec.paths || typeof spec.paths !== 'object') {
    throw new Error("That document doesn't have an OpenAPI \"paths\" section — is this really an API spec?");
  }

  // Two real spec shapes exist and both are still common — confirmed live
  // against two different real Petstore instances: OpenAPI 3.x's
  // `servers[0].url` (often relative, e.g. "/api/v3" — resolved against the
  // spec document's own origin so it's always absolute), and the older
  // Swagger 2.0 shape (`host` + `basePath` + `schemes`, no `servers` array
  // at all — a real, still-live example server uses exactly this). Both are
  // normalized to one absolute baseUrl here so dispatch() never has to care
  // which shape the spec came in.
  const rawServerUrl = spec.servers?.[0]?.url || null;
  let baseUrl = rawServerUrl ? new URL(rawServerUrl, specUrl).href : null;
  if (!baseUrl && spec.host) {
    const scheme = spec.schemes?.[0] || 'https';
    baseUrl = `${scheme}://${spec.host}${spec.basePath || ''}`;
  }
  const operations = [];
  for (const [pathTemplate, methods] of Object.entries(spec.paths)) {
    for (const method of HTTP_METHODS) {
      const op = methods[method];
      if (!op) continue;
      const pathParams = paramsFromSpec(op.parameters);
      const hasBody = ['post', 'put', 'patch'].includes(method) && Boolean(op.requestBody);
      const properties = {};
      const required = [];
      for (const p of pathParams) {
        properties[p.name] = { type: 'string', description: p.description || undefined };
        if (p.required) required.push(p.name);
      }
      if (hasBody) {
        // A full JSON-Schema translation of an arbitrary requestBody is real
        // work with limited payoff here — one generic `body` object property
        // (the model fills it in from the operation's own description) covers
        // the common case without pretending to a precision this parser
        // doesn't have.
        properties.body = { type: 'object', description: 'The request body, as JSON.' };
      }
      operations.push({
        name: sanitizeName(op.operationId || `${method}_${pathTemplate}`),
        description: op.summary || op.description || `${method.toUpperCase()} ${pathTemplate}`,
        method: method.toUpperCase(),
        path: pathTemplate,
        parameters: { type: 'object', properties, required },
      });
    }
  }

  if (!operations.length) throw new Error('No usable endpoints were found in that document.');
  return { baseUrl, operations };
}

/** `{name, description, parameters}` per saved, enabled operation. */
export function toolDeclarations(config) {
  return (config?.operations || []).map((op) => ({
    name: op.name,
    description: op.description || `${op.method} ${op.path}`,
    parameters: op.parameters || { type: 'object', properties: {}, required: [] },
  }));
}

function buildAuth(config) {
  const auth = config?.auth || { kind: 'bearer' };
  const secret = getSecret(config?.secretRef);
  if (!secret) return { headers: {}, query: {} };
  if (auth.kind === 'query') return { headers: {}, query: { [auth.name || 'api_key']: secret } };
  if (auth.kind === 'header') return { headers: { [auth.name || 'X-API-Key']: `${auth.prefix || ''}${secret}` }, query: {} };
  // 'bearer' (default)
  return { headers: { Authorization: `Bearer ${secret}` }, query: {} };
}

/** Runs one saved operation by name. Throws on failure — callers (connectors/index.js) already catch and wrap. */
export async function dispatch(name, args, config) {
  const op = (config?.operations || []).find((o) => o.name === name);
  if (!op) throw new Error(`Unknown API operation: ${name}`);

  const usedKeys = new Set();
  let path = op.path;
  for (const match of op.path.matchAll(/\{([^}]+)\}/g)) {
    const key = match[1];
    const value = args?.[key];
    if (value === undefined) throw new Error(`Missing required value "${key}" for this call.`);
    path = path.replace(`{${key}}`, encodeURIComponent(value));
    usedKeys.add(key);
  }

  if (!config?.baseUrl) throw new Error('This connector has no base address configured yet.');

  const { headers, query } = buildAuth(config);
  // NOT `new URL(path, baseUrl)` — a leading-slash relative reference
  // resolves against the ORIGIN in the WHATWG URL spec, discarding baseUrl's
  // own path entirely (confirmed live: dropped "/api/v3" from a real
  // baseUrl, 404ing against the bare origin instead). Plain string
  // concatenation, then parsed, is what actually appends an OpenAPI-style
  // absolute path onto a base that itself has a path component.
  const base = String(config?.baseUrl || '').replace(/\/$/, '');
  const url = new URL(base + path);
  for (const [key, value] of Object.entries(query)) url.searchParams.set(key, value);
  // Any declared property not already consumed as a path param, and not the
  // generic `body` blob, is treated as a query parameter — covers the
  // common "?limit=10&query=..." case without the caller needing to say
  // which is which.
  for (const [key, value] of Object.entries(args || {})) {
    if (key === 'body' || usedKeys.has(key)) continue;
    if (value !== undefined) url.searchParams.set(key, value);
  }

  const init = { method: op.method || 'GET', headers: { ...headers } };
  if (args?.body !== undefined && ['POST', 'PUT', 'PATCH'].includes(init.method)) {
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(args.body);
  }

  let res;
  try {
    res = await fetch(url, init);
  } catch (err) {
    throw new Error(`Couldn't reach the service: ${err?.message || 'unknown error'}.`);
  }

  const text = await res.text();
  let data = text;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    // Not JSON — the raw text is still useful to hand back.
  }

  if (!res.ok) {
    const detail = typeof data === 'object' && data ? data.message || data.error || JSON.stringify(data) : text;
    throw new Error(`The service responded with an error (${res.status}): ${detail || 'no details given'}.`);
  }
  return { ok: true, status: res.status, data };
}

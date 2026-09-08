// Systematically exercises every safe route through the recording proxy, so the
// contract suite starts from real coverage rather than whatever a browsing
// session happened to touch.
//
//   node tools/record/drive.mjs                 # against http://127.0.0.1:3100
//   RECORD_PORT=39118 node tools/record/drive.mjs
//
// Only READ-ONLY routes are driven automatically. Anything that mutates state
// (POST/PUT/PATCH/DELETE) needs a real, meaningful payload and would leave the
// recorded database in an arbitrary state — those are captured by using the app
// for real through the proxy instead.
//
// Two route shapes are driven:
//   - plain GETs, for the happy path
//   - GETs taking an :id, called with an id that cannot exist, to pin down the
//     not-found behaviour (a clean 404, never a crash) for every one of them
//
// Endpoints that stream forever (/api/events) or cost real model quota
// (/api/chat/stream) are skipped by name.

import fs from 'node:fs';
import path from 'node:path';

const PORT = Number(process.env.RECORD_PORT || 3100);
const HOST = process.env.RECORD_HOST || '127.0.0.1';
const BASE = `http://${HOST}:${PORT}`;
const SERVER_JS = process.env.SERVER_JS || path.join(process.cwd(), 'server', 'server.js');

// Never driven automatically:
//   events      — an SSE stream that never ends; the driver would hang on it
//   chat/stream — spends real model quota, and its contract is an event
//                 sequence verified separately in Wave 5
//   oauth/*     — starts a real third-party authorisation flow
const SKIP = [
  '/api/events',
  '/api/chat/stream',
  '/api/connectors/oauth/callback',
  '/api/connectors/oauth/redirect-uri',
  '/api/connectors/oauth/public-url',
];

const NONEXISTENT_ID = 'zzz-does-not-exist-zzz';

function extractRoutes() {
  const src = fs.readFileSync(SERVER_JS, 'utf8');
  const routes = [];
  const re = /app\.(get|post|put|patch|delete)\('([^']+)'/g;
  let m;
  while ((m = re.exec(src)) !== null) {
    routes.push({ method: m[1].toUpperCase(), path: m[2] });
  }
  return routes;
}

async function hit(method, url) {
  const started = Date.now();
  try {
    const res = await fetch(url, { method, signal: AbortSignal.timeout(15000) });
    // Drain the body so the proxy sees a completed response and writes its fixture.
    await res.arrayBuffer();
    return { status: res.status, ms: Date.now() - started };
  } catch (err) {
    return { status: 'ERR', error: err.message, ms: Date.now() - started };
  }
}

const routes = extractRoutes();
const gets = routes.filter((r) => r.method === 'GET' && !SKIP.includes(r.path));
const plain = gets.filter((r) => !r.path.includes(':'));
const parameterised = gets.filter((r) => r.path.includes(':'));

console.log(`[drive] ${routes.length} routes found; driving ${plain.length} plain GETs + ${parameterised.length} :id GETs against ${BASE}`);

let ok = 0;
let notFound = 0;
let failed = 0;

console.log('\n--- plain GETs (happy path) ---');
for (const route of plain) {
  const { status, ms, error } = await hit('GET', BASE + route.path);
  if (status === 'ERR') { failed += 1; console.log(`  ERR   ${route.path}  ${error}`); }
  else if (status < 400) { ok += 1; console.log(`  ${status}   ${route.path}  (${ms}ms)`); }
  else { console.log(`  ${status}   ${route.path}  (${ms}ms)`); }
}

console.log('\n--- :id GETs with a nonexistent id (expect a clean 404) ---');
for (const route of parameterised) {
  const url = BASE + route.path.replace(/:[A-Za-z0-9_]+/g, NONEXISTENT_ID);
  const { status, ms, error } = await hit('GET', url);
  if (status === 'ERR') { failed += 1; console.log(`  ERR   ${route.path}  ${error}`); }
  else if (status === 404) { notFound += 1; console.log(`  404   ${route.path}  (${ms}ms)`); }
  else { console.log(`  ${status}   ${route.path}  (${ms}ms)  <- not a 404, worth a look`); }
}

console.log(`\n[drive] done. ${ok} ok, ${notFound} clean 404s, ${failed} transport failures.`);
if (failed) process.exitCode = 1;

// A recording reverse proxy, for capturing exactly what the Node server answers
// so the Python port can be held to the same answers.
//
// Deliberately a PROXY rather than Express middleware: the migration's ground
// rules say server/ is not edited for the duration of the build, and the owner
// runs this app daily. Sitting in front of the real server on a different port
// means the thing being recorded is the untouched production code path, and
// nothing about the owner's own instance changes.
//
//   node tools/record/proxy.mjs              # :3100 -> :3000
//   RECORD_PORT=3100 TARGET_PORT=3000 node tools/record/proxy.mjs
//
// Point a browser (or tools/record/drive.mjs) at the record port and use the app
// normally. Every exchange lands in backend/tests/contract/fixtures/.
//
// SSE and WebSocket traffic is recorded as an ORDERED EVENT SEQUENCE rather than
// a response body: a chat turn's bytes are never identical twice (a model is
// non-deterministic), but the shape and order of the events it emits is the
// actual contract the front end depends on.

import http from 'node:http';
import net from 'node:net';
import fs from 'node:fs';
import path from 'node:path';

const RECORD_PORT = Number(process.env.RECORD_PORT || 3100);
const TARGET_PORT = Number(process.env.TARGET_PORT || 3000);
const TARGET_HOST = process.env.TARGET_HOST || '127.0.0.1';
const OUT_DIR = process.env.FIXTURE_DIR
  || path.join(process.cwd(), 'backend', 'tests', 'contract', 'fixtures');

fs.mkdirSync(OUT_DIR, { recursive: true });

// Anything that could carry a real credential is redacted before it ever
// reaches a fixture file. Fixtures are committed; secrets are not.
const SECRET_KEY_RE = /(key|token|secret|password|authorization|apikey)/i;
const REDACTED = '<redacted>';

function redact(value, keyHint = '') {
  if (value === null || value === undefined) return value;
  if (Array.isArray(value)) return value.map((v) => redact(v, keyHint));
  if (typeof value === 'object') {
    const out = {};
    for (const [k, v] of Object.entries(value)) {
      out[k] = SECRET_KEY_RE.test(k) && typeof v === 'string' && v ? REDACTED : redact(v, k);
    }
    return out;
  }
  if (typeof value === 'string' && SECRET_KEY_RE.test(keyHint) && value) return REDACTED;
  return value;
}

function redactHeaders(headers) {
  const out = {};
  for (const [k, v] of Object.entries(headers)) {
    out[k] = SECRET_KEY_RE.test(k) ? REDACTED : v;
  }
  return out;
}

function parseBody(raw, contentType = '') {
  if (!raw || !raw.length) return null;
  if (contentType.includes('application/json')) {
    try {
      return redact(JSON.parse(raw.toString('utf8')));
    } catch {
      return { _unparseable: raw.toString('utf8').slice(0, 2000) };
    }
  }
  if (contentType.startsWith('text/') || contentType.includes('javascript')) {
    return { _text: raw.toString('utf8').slice(0, 4000) };
  }
  return { _binary: raw.length };
}

// Splits an SSE byte stream into the typed events the front end actually sees.
function parseSseEvents(text) {
  const events = [];
  for (const block of text.split('\n\n')) {
    const line = block.split('\n').find((l) => l.startsWith('data: '));
    if (!line) continue;
    try {
      events.push(redact(JSON.parse(line.slice(6))));
    } catch {
      events.push({ _unparseable: line.slice(6, 500) });
    }
  }
  return events;
}

let counter = 0;

function fixtureName(method, url) {
  counter += 1;
  const slug = url.split('?')[0].replace(/[^a-zA-Z0-9]+/g, '-').replace(/^-|-$/g, '') || 'root';
  return `${String(counter).padStart(4, '0')}-${method.toLowerCase()}-${slug.slice(0, 80)}.json`;
}

const server = http.createServer((clientReq, clientRes) => {
  const chunks = [];
  clientReq.on('data', (c) => chunks.push(c));
  clientReq.on('end', () => {
    const reqBody = Buffer.concat(chunks);
    const proxyReq = http.request(
      {
        host: TARGET_HOST,
        port: TARGET_PORT,
        method: clientReq.method,
        path: clientReq.url,
        headers: { ...clientReq.headers, host: `${TARGET_HOST}:${TARGET_PORT}` },
      },
      (proxyRes) => {
        const resChunks = [];
        clientRes.writeHead(proxyRes.statusCode, proxyRes.headers);
        proxyRes.on('data', (c) => {
          resChunks.push(c);
          clientRes.write(c);
        });
        proxyRes.on('end', () => {
          clientRes.end();
          record(clientReq, reqBody, proxyRes, Buffer.concat(resChunks));
        });
      }
    );
    proxyReq.on('error', (err) => {
      console.error(`[record] upstream error for ${clientReq.method} ${clientReq.url}:`, err.message);
      if (!clientRes.headersSent) clientRes.writeHead(502);
      clientRes.end('recorder: upstream unreachable');
    });
    if (reqBody.length) proxyReq.write(reqBody);
    proxyReq.end();
  });
});

function record(req, reqBody, res, resBody) {
  const url = req.url || '/';
  // Static assets are not part of the API contract the port has to reproduce.
  if (!url.startsWith('/api/')) return;

  const contentType = String(res.headers['content-type'] || '');
  const isSse = contentType.includes('text/event-stream');
  const [pathname, query = ''] = url.split('?');

  const fixture = {
    request: {
      method: req.method,
      path: pathname,
      query,
      headers: redactHeaders(req.headers),
      body: parseBody(reqBody, String(req.headers['content-type'] || '')),
    },
    response: {
      status: res.statusCode,
      contentType,
      // Full response headers, not just the content type: several routes'
      // actual contract lives in a header rather than the body. The artifacts
      // route in particular must force Content-Disposition: attachment
      // unconditionally, with nosniff and a sandboxing CSP — a real stored-XSS
      // fix that a body-only fixture would not notice regressing.
      headers: redactHeaders(res.headers),
      kind: isSse ? 'sse' : 'json',
      // An SSE response's bytes differ every run (a model is non-deterministic);
      // its event sequence is the part the front end is actually coupled to.
      events: isSse ? parseSseEvents(resBody.toString('utf8')) : undefined,
      body: isSse ? undefined : parseBody(resBody, contentType),
    },
    recordedAt: new Date().toISOString(),
  };

  const name = fixtureName(req.method, url);
  fs.writeFileSync(path.join(OUT_DIR, name), JSON.stringify(fixture, null, 2));
  const detail = isSse ? `${fixture.response.events.length} events` : `${resBody.length}b`;
  console.log(`[record] ${res.statusCode} ${req.method} ${pathname} -> ${name} (${detail})`);
}

// WebSocket upgrades are tunnelled straight through, untouched and unrecorded.
// /api/live and /api/duplex carry raw audio; their contract is verified by live
// tests in Wave 5, not by fixture replay. Tunnelling rather than rejecting
// matters: voice has to keep working while a session is being recorded, or the
// recording only ever covers the typed half of the app.
server.on('upgrade', (req, clientSocket, head) => {
  console.log(`[record] websocket ${req.url} — tunnelled, not recorded`);
  const upstream = net.connect(TARGET_PORT, TARGET_HOST, () => {
    const headerLines = Object.entries(req.headers)
      .map(([k, v]) => `${k}: ${Array.isArray(v) ? v.join(', ') : v}`)
      .join('\r\n');
    upstream.write(`${req.method} ${req.url} HTTP/1.1\r\n${headerLines}\r\n\r\n`);
    if (head && head.length) upstream.write(head);
    upstream.pipe(clientSocket);
    clientSocket.pipe(upstream);
  });
  const drop = (err) => {
    if (err) console.error('[record] websocket tunnel error:', err.message);
    upstream.destroy();
    clientSocket.destroy();
  };
  upstream.on('error', drop);
  clientSocket.on('error', drop);
});

server.listen(RECORD_PORT, '127.0.0.1', () => {
  console.log(`[record] listening on http://127.0.0.1:${RECORD_PORT} -> http://${TARGET_HOST}:${TARGET_PORT}`);
  console.log(`[record] fixtures -> ${OUT_DIR}`);
});

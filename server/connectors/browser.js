// The browser connector: drives a real Chrome/Edge window over the Chrome
// DevTools Protocol (CDP), via a plain WebSocket (the already-installed `ws`
// package — no Playwright/Puppeteer). Launched with its own dedicated
// profile directory and debugging port, entirely separate from the user's
// normal browser windows/profile/history/logins.
//
// Navigate/read/click/type all go through here rather than the desktop
// control loop's mouse — clicking a browser blindly with real mouse
// coordinates is the fragile way; running a small script against the actual
// page (`Runtime.evaluate`) is the correct one, and is how `read_page`
// returns real text instead of a screenshot guess.

import { spawn } from 'node:child_process';
import path from 'node:path';
import fs from 'node:fs';
import WebSocket from 'ws';
import { dataDir } from '../store.js';

const DEBUG_PORT = 9333; // fixed, dedicated to Jarvis's own browser connector — never the user's own Chrome's port
// A second, separate port + profile for the headless renderer below
// (renderPageHeadless) — deliberately never the same instance as the
// visible connector above: a headless render is a short-lived, one-off
// fetch-with-real-rendering used by read_web_page's own fallback, and must
// never navigate away from a page the user might be mid-interacting with in
// the visible browser, or contend with it over the same profile lock file.
const HEADLESS_DEBUG_PORT = 9334;
// Goes through store.js's dataDir() (JARVIS_DATA_DIR-aware) rather than a
// path hardcoded relative to this file — see store.js's dataDir() doc
// comment for the real bug that taught this.
const PROFILE_DIR = path.join(dataDir(), 'browser-profile');
const NAV_TIMEOUT_MS = 20 * 1000;
const CMD_TIMEOUT_MS = 15 * 1000;
const HEADLESS_NAV_TIMEOUT_MS = 15 * 1000; // a bit tighter — this is a fallback path, not worth a long hang

// Common install locations — checked in order, first one that exists wins.
// No registry lookup, no new dependency; falls back to plain "chrome" on
// PATH, which works when the user's shell already resolves it.
const CHROME_CANDIDATES = [
  'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
  'C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe',
  'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
];

let browserState = null; // { proc, ws, pending, nextId }

function findChromeExecutable() {
  for (const candidate of CHROME_CANDIDATES) {
    if (fs.existsSync(candidate)) return candidate;
  }
  return 'chrome'; // let the OS PATH resolve it
}

async function fetchJson(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`${url} responded with ${res.status}`);
  return res.json();
}

async function waitForDevtools(port, retries = 30) {
  for (let i = 0; i < retries; i++) {
    try {
      return await fetchJson(`http://127.0.0.1:${port}/json/version`);
    } catch {
      await new Promise((resolve) => setTimeout(resolve, 300));
    }
  }
  throw new Error('The browser did not become ready in time.');
}

async function pickPageTarget(port) {
  const targets = await fetchJson(`http://127.0.0.1:${port}/json/list`);
  const page = targets.find((t) => t.type === 'page');
  if (!page) throw new Error('No browser tab is available to control.');
  return page;
}

/**
 * Confirmed by hand: right after a cold Chrome launch, the page target's own
 * DevTools agent isn't always fully attached the instant its WebSocket opens
 * — the very first command sent can go completely unanswered even though the
 * browser-level HTTP endpoint (waitForDevtools) was already responding. A
 * short-timeout retry loop, used only for this one-time bootstrap step,
 * papers over that window without making every ordinary command wait
 * needlessly long on a false failure.
 */
async function sendCdpBootstrap(state, method, attempts = 5) {
  let lastErr;
  for (let i = 0; i < attempts; i++) {
    try {
      return await sendCdp(state, method, {}, 2000);
    } catch (err) {
      lastErr = err;
    }
  }
  throw lastErr;
}

function sendCdp(state, method, params, timeoutMs = CMD_TIMEOUT_MS) {
  const id = state.nextId++;
  const payload = JSON.stringify({ id, method, params: params || {} });
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      state.pending.delete(id);
      reject(new Error(`Browser did not respond to "${method}" in time.`));
    }, timeoutMs);
    state.pending.set(id, { resolve, reject, timer });
    state.ws.send(payload, (err) => {
      if (err) {
        state.pending.delete(id);
        clearTimeout(timer);
        reject(new Error(`Could not reach the browser: ${err.message}`));
      }
    });
  });
}

/**
 * Launches a fresh Chrome/Edge process against `port`/`profileDir` and wires
 * up a connected CDP `state` object — the shared bootstrap both the
 * persistent visible connector (ensureBrowser, below) and the ephemeral
 * headless renderer (renderPageHeadless, further below) build on, so the
 * cold-start fragility fixes (the flat post-devtools pause, the bootstrap
 * retry loop) only ever live in one place. `onClose` lets a caller (only the
 * persistent singleton needs this) know when the connection drops so it can
 * clear its own reference; the ephemeral caller tears its own state down
 * explicitly instead and passes nothing.
 */
async function launchAndConnect({ port, profileDir, extraArgs = [], onClose }) {
  const exe = findChromeExecutable();
  const proc = spawn(
    exe,
    [`--remote-debugging-port=${port}`, `--user-data-dir=${profileDir}`, '--no-first-run', '--no-default-browser-check', ...extraArgs, 'about:blank'],
    { detached: true, stdio: 'ignore', windowsHide: false }
  );
  proc.unref();
  proc.on('error', (err) => {
    console.error('[browser connector] failed to launch:', err.message);
  });
  await waitForDevtools(port);
  // Confirmed by hand: Chrome's HTTP debug endpoint (waitForDevtools above)
  // can start responding before its internal engine is fully warmed up — a
  // CDP command sent within roughly the first second of the browser
  // process's own life can go completely unanswered even on a freshly
  // opened, healthy WebSocket, while the exact same command a second or two
  // later works instantly. This fixed pause is the real fix; the retry loop
  // around Page.enable/Runtime.enable below is defense in depth, not the
  // primary mechanism.
  await new Promise((resolve) => setTimeout(resolve, 1200));
  const target = await pickPageTarget(port);
  const ws = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    ws.once('open', resolve);
    ws.once('error', reject);
  });

  const state = { proc, ws, pending: new Map(), nextId: 1 };
  ws.on('message', (data) => {
    let msg;
    try {
      msg = JSON.parse(data.toString());
    } catch {
      return;
    }
    if (msg.id === undefined) return; // an unsolicited CDP event — not something any call here awaits
    const entry = state.pending.get(msg.id);
    if (!entry) return;
    state.pending.delete(msg.id);
    clearTimeout(entry.timer);
    if (msg.error) entry.reject(new Error(msg.error.message || 'The browser reported an error.'));
    else entry.resolve(msg.result);
  });
  ws.on('close', () => {
    for (const [, entry] of state.pending) {
      clearTimeout(entry.timer);
      entry.reject(new Error('The browser connection closed.'));
    }
    onClose?.();
  });

  await sendCdpBootstrap(state, 'Page.enable');
  await sendCdpBootstrap(state, 'Runtime.enable');
  return state;
}

/** Ensures a VISIBLE browser is running and connected, returning the shared CDP state. Launches once, reused for every subsequent call — this is Jarvis's own dedicated automation browser, used only when a task genuinely needs to click/type/interact with a real page. For a plain information lookup, prefer read_web_page (or, for a JS-heavy page it can't read, renderPageHeadless below) — neither ever pops a window. */
export async function ensureBrowser() {
  if (browserState && browserState.ws.readyState === WebSocket.OPEN) return browserState;
  if (!browserState || browserState.proc.killed) {
    browserState = await launchAndConnect({
      port: DEBUG_PORT,
      profileDir: PROFILE_DIR,
      onClose: () => {
        browserState = null;
      },
    });
  }
  return browserState;
}

/** Navigates a given, already-connected `state` — the shared logic behind both navigate() (the visible connector) and renderPageHeadless() (below) below it. */
async function navigateOn(state, url, timeoutMs = NAV_TIMEOUT_MS) {
  const navPromise = new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      state.ws.off('message', onMessage);
      reject(new Error('The page took too long to load.'));
    }, timeoutMs);
    function onMessage(data) {
      let msg;
      try {
        msg = JSON.parse(data.toString());
      } catch {
        return;
      }
      if (msg.method === 'Page.loadEventFired') {
        clearTimeout(timer);
        state.ws.off('message', onMessage);
        resolve();
      }
    }
    state.ws.on('message', onMessage);
  });
  await sendCdp(state, 'Page.navigate', { url });
  await navPromise;
  return { navigatedTo: url };
}

export async function navigate(url) {
  return navigateOn(await ensureBrowser(), url);
}

/** Runs a small JS expression in the page and returns its JSON-serializable result — the shared logic behind evaluate() (the visible connector) and renderPageHeadless() (below). Used for read/click/type instead of the Input domain's raw mouse/keyboard events — simpler and more reliable for ordinary page interaction. */
async function evaluateOn(state, expression) {
  const result = await sendCdp(state, 'Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true });
  if (result.exceptionDetails) {
    throw new Error(result.exceptionDetails.exception?.description || result.exceptionDetails.text || 'The page script failed.');
  }
  return result.result?.value;
}

async function evaluate(expression) {
  return evaluateOn(await ensureBrowser(), expression);
}

export async function readPage() {
  const [title, url, text] = await Promise.all([
    evaluate('document.title'),
    evaluate('location.href'),
    evaluate('document.body ? document.body.innerText.slice(0, 8000) : ""'),
  ]);
  return { title, url, text };
}

export async function clickSelector(selector) {
  const sel = JSON.stringify(selector);
  const outcome = await evaluate(
    `(function(){var el=document.querySelector(${sel}); if(!el) return {ok:false,error:'No element matches that selector.'}; el.scrollIntoView({block:'center'}); el.click(); return {ok:true};})()`
  );
  if (!outcome?.ok) throw new Error(outcome?.error || 'Click failed.');
  return { clicked: selector };
}

export async function typeIntoSelector(selector, text) {
  const sel = JSON.stringify(selector);
  const value = JSON.stringify(text);
  // Plain `el.value = ...` doesn't notify React/Vue-controlled inputs — the
  // native property setter + a real 'input' event is what actually gets
  // picked up by frameworks that override the value setter on their own
  // controlled components.
  const outcome = await evaluate(`
    (function(){
      var el = document.querySelector(${sel});
      if (!el) return { ok: false, error: 'No element matches that selector.' };
      el.scrollIntoView({ block: 'center' });
      el.focus();
      var proto = el.tagName === 'TEXTAREA' ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
      var setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
      setter.call(el, ${value});
      el.dispatchEvent(new Event('input', { bubbles: true }));
      el.dispatchEvent(new Event('change', { bubbles: true }));
      return { ok: true };
    })()
  `);
  if (!outcome?.ok) throw new Error(outcome?.error || 'Typing failed.');
  return { typed: text };
}

/**
 * A short-lived, fully invisible (`--headless=new`, no window ever appears)
 * Chrome instance used ONLY as read_web_page's own fallback — a plain
 * `fetch()` gets nothing useful from a JS-rendered page (a real SPA with no
 * server-rendered HTML), so this renders the page for real without ever
 * popping a visible window, keeping "most lookups shouldn't show you a
 * browser" true even for the pages a plain fetch can't read. Deliberately a
 * SEPARATE process/port/profile from ensureBrowser()'s persistent visible
 * one (see HEADLESS_DEBUG_PORT above) — this must never navigate the
 * window the user might actually be looking at, and always launches, reads,
 * and closes itself within one call, never left running between calls the
 * way the visible connector is. Any failure (chrome not found, page never
 * loads, script throws) surfaces as a thrown error — callers already know
 * how to fall back from that (see read_web_page.js).
 */
export async function renderPageHeadless(url, { maxChars = 6000 } = {}) {
  const profileDir = path.join(dataDir(), 'browser-profile-headless');
  let state;
  try {
    state = await launchAndConnect({
      port: HEADLESS_DEBUG_PORT,
      profileDir,
      extraArgs: ['--headless=new', '--disable-gpu'],
    });
    await navigateOn(state, url, HEADLESS_NAV_TIMEOUT_MS);
    const [title, finalUrl, text] = await Promise.all([
      evaluateOn(state, 'document.title'),
      evaluateOn(state, 'location.href'),
      evaluateOn(state, `document.body ? document.body.innerText.slice(0, ${Number(maxChars) || 6000}) : ""`),
    ]);
    return { title, url: finalUrl, text };
  } finally {
    if (state) {
      try {
        state.ws.close();
      } catch {
        // Fine either way.
      }
      try {
        state.proc.kill();
      } catch {
        // Fine either way.
      }
    }
  }
}

export function closeBrowser() {
  if (!browserState) return;
  try {
    browserState.ws.close();
  } catch {
    // Fine either way.
  }
  try {
    browserState.proc.kill();
  } catch {
    // Fine either way.
  }
  browserState = null;
}

// ---------- tool declarations + dispatch (consumed by connectors/index.js) ----------

export function toolDeclarations() {
  return [
    {
      name: 'browser_navigate',
      description:
        'Opens a URL in a REAL, VISIBLE browser window on the user\'s screen (Jarvis\'s own separate, isolated browser — never the user\'s real Chrome). ' +
        'Only use this when the task genuinely needs clicking, typing, or filling something in on a real page, or the user explicitly asked to browse/open ' +
        'a site to look at. For a plain information lookup, use read_web_page instead — it works invisibly in the background with no window popping up.',
      parameters: { type: 'object', properties: { url: { type: 'string' } }, required: ['url'] },
    },
    {
      name: 'browser_read_page',
      description: 'Read the current page\'s title, URL, and visible text, from the visible browser window opened by browser_navigate.',
      parameters: { type: 'object', properties: {}, required: [] },
    },
    {
      name: 'browser_click',
      description: 'Click an element on the current page in the visible browser window, given a CSS selector.',
      parameters: { type: 'object', properties: { selector: { type: 'string' } }, required: ['selector'] },
    },
    {
      name: 'browser_type',
      description: 'Type text into an element on the current page in the visible browser window, given a CSS selector.',
      parameters: {
        type: 'object',
        properties: { selector: { type: 'string' }, text: { type: 'string' } },
        required: ['selector', 'text'],
      },
    },
  ];
}

export async function dispatch(name, args) {
  if (name === 'browser_navigate') return { ok: true, ...(await navigate(args.url)) };
  if (name === 'browser_read_page') return { ok: true, ...(await readPage()) };
  if (name === 'browser_click') return { ok: true, ...(await clickSelector(args.selector)) };
  if (name === 'browser_type') return { ok: true, ...(await typeIntoSelector(args.selector, args.text)) };
  throw new Error(`Unknown browser tool: ${name}`);
}

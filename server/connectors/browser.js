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
// Goes through store.js's dataDir() (JARVIS_DATA_DIR-aware) rather than a
// path hardcoded relative to this file — see store.js's dataDir() doc
// comment for the real bug that taught this.
const PROFILE_DIR = path.join(dataDir(), 'browser-profile');
const NAV_TIMEOUT_MS = 20 * 1000;
const CMD_TIMEOUT_MS = 15 * 1000;

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

async function waitForDevtools(retries = 30) {
  for (let i = 0; i < retries; i++) {
    try {
      return await fetchJson(`http://127.0.0.1:${DEBUG_PORT}/json/version`);
    } catch {
      await new Promise((resolve) => setTimeout(resolve, 300));
    }
  }
  throw new Error('The browser did not become ready in time.');
}

async function pickPageTarget() {
  const targets = await fetchJson(`http://127.0.0.1:${DEBUG_PORT}/json/list`);
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

/** Ensures a browser is running and connected, returning the shared CDP state. Launches once, reused for every subsequent call. */
export async function ensureBrowser() {
  if (browserState && browserState.ws.readyState === WebSocket.OPEN) return browserState;

  if (!browserState || browserState.proc.killed) {
    const exe = findChromeExecutable();
    const proc = spawn(
      exe,
      [
        `--remote-debugging-port=${DEBUG_PORT}`,
        `--user-data-dir=${PROFILE_DIR}`,
        '--no-first-run',
        '--no-default-browser-check',
        'about:blank',
      ],
      { detached: true, stdio: 'ignore', windowsHide: false }
    );
    proc.unref();
    proc.on('error', (err) => {
      console.error('[browser connector] failed to launch:', err.message);
    });
    await waitForDevtools();
    // Confirmed by hand: Chrome's HTTP debug endpoint (waitForDevtools above)
    // can start responding before its internal engine is fully warmed up —
    // a CDP command sent within roughly the first second of the browser
    // process's own life can go completely unanswered even on a freshly
    // opened, healthy WebSocket, while the exact same command a second or
    // two later works instantly. This fixed pause is the real fix; the
    // retry loop around Page.enable/Runtime.enable below is defense in
    // depth, not the primary mechanism.
    await new Promise((resolve) => setTimeout(resolve, 1200));
    const target = await pickPageTarget();
    const ws = new WebSocket(target.webSocketDebuggerUrl);
    await new Promise((resolve, reject) => {
      ws.once('open', resolve);
      ws.once('error', reject);
    });

    browserState = { proc, ws, pending: new Map(), nextId: 1 };
    ws.on('message', (data) => {
      let msg;
      try {
        msg = JSON.parse(data.toString());
      } catch {
        return;
      }
      if (msg.id === undefined) return; // an unsolicited CDP event — not something any call here awaits
      const entry = browserState.pending.get(msg.id);
      if (!entry) return;
      browserState.pending.delete(msg.id);
      clearTimeout(entry.timer);
      if (msg.error) entry.reject(new Error(msg.error.message || 'The browser reported an error.'));
      else entry.resolve(msg.result);
    });
    ws.on('close', () => {
      for (const [, entry] of browserState.pending) {
        clearTimeout(entry.timer);
        entry.reject(new Error('The browser connection closed.'));
      }
      browserState = null;
    });

    await sendCdpBootstrap(browserState, 'Page.enable');
    await sendCdpBootstrap(browserState, 'Runtime.enable');
  }

  return browserState;
}

export async function navigate(url) {
  const state = await ensureBrowser();
  const navPromise = new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      state.ws.off('message', onMessage);
      reject(new Error('The page took too long to load.'));
    }, NAV_TIMEOUT_MS);
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

/** Runs a small JS expression in the page and returns its JSON-serializable result. Used for read/click/type instead of the Input domain's raw mouse/keyboard events — simpler and more reliable for ordinary page interaction. */
async function evaluate(expression) {
  const state = await ensureBrowser();
  const result = await sendCdp(state, 'Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true });
  if (result.exceptionDetails) {
    throw new Error(result.exceptionDetails.exception?.description || result.exceptionDetails.text || 'The page script failed.');
  }
  return result.result?.value;
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
      description: "Open a URL in Jarvis's own controlled browser window.",
      parameters: { type: 'object', properties: { url: { type: 'string' } }, required: ['url'] },
    },
    {
      name: 'browser_read_page',
      description: 'Read the current page\'s title, URL, and visible text.',
      parameters: { type: 'object', properties: {}, required: [] },
    },
    {
      name: 'browser_click',
      description: 'Click an element on the current page, given a CSS selector.',
      parameters: { type: 'object', properties: { selector: { type: 'string' } }, required: ['selector'] },
    },
    {
      name: 'browser_type',
      description: 'Type text into an element on the current page, given a CSS selector.',
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

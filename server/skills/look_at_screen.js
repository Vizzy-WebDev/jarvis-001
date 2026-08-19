// Skill: look at what's currently on screen and describe/evaluate it — without
// controlling anything. Deliberately separate from control_computer.js: this
// never starts a control session, never shows the red control overlay, never
// touches the mouse or keyboard, and needs no confirmation (looking is not an
// action with a side effect). It answers a single question about a single
// moment, not a repeating watch — for "tell me when X happens", see
// watch_for.js instead.
//
// Imports only control/ps-bridge.js (the same low-level screen/window reader
// control/session.js uses) and ai.js's askModel — never control/session.js
// itself, so this stays safe to import from anywhere skills/index.js reaches.

import { send as sendCommand } from '../control/ps-bridge.js';
import { saveScreenshot } from '../control/screenshot-store.js';
import { askModel } from '../ai.js';
import { broadcast } from '../events.js';
import { showIndicator, hideIndicator } from '../control/observation-bridge.js';

// Real bug found in live testing: the user is normally TALKING to Jarvis
// through a browser tab, so at the moment "look at my screen" runs, the OS
// foreground window (and the topmost window in z-order) is very often
// Jarvis's own tab — not whatever app the user actually means. The same
// ambiguity broke explicit targeting too: asking for "my Chrome window"
// while Jarvis also happens to be running in a Chrome tab could match either
// one, since both share the same process name. Fix: never treat Jarvis's own
// tab as a candidate unless it is genuinely the only window open — matched by
// a known browser process AND the page's real <title> (see public/index.html),
// not a bare "chrome" process check, so a File Explorer window titled
// "Jarvis-001" (a different process entirely) is never wrongly excluded.
const BROWSER_PROCESSES = new Set(['chrome', 'msedge', 'firefox', 'brave', 'opera', 'vivaldi']);
export function isJarvisOwnWindow(w) {
  if (!BROWSER_PROCESSES.has(String(w.processName || '').toLowerCase())) return false;
  const title = String(w.title || '').trim();
  return title === 'Jarvis' || /^Jarvis\s*[-–—]/.test(title);
}

/** Finds the best-matching open window for a free-text target, or null for "whichever is in front"/"the whole screen". Exported (alongside the default skill export) purely so it's directly testable per CLAUDE.md's pure-logic-module pattern — no server needed. */
export function pickWindow(windows, target) {
  // Exclude Jarvis's own tab from the candidate pool up front — only fall
  // back to including it if it's truly the only window open, since looking
  // at nothing at all would be a worse answer than looking at ourselves.
  const others = windows.filter((w) => !isJarvisOwnWindow(w));
  const pool = others.length ? others : windows;

  const cleaned = String(target || '').trim().toLowerCase();
  if (!cleaned || ['screen', 'desktop', 'everything', 'my screen', 'whole screen'].includes(cleaned)) {
    // windows[] is already topmost-first (EnumWindows' Z-order), so once
    // Jarvis's own tab is excluded, "the first one left" is a good proxy for
    // "whatever I was just looking at" even when nothing currently holds OS
    // input focus (e.g. focus is on Jarvis's text box, but the app the user
    // means is still sitting right behind it).
    return pool.find((w) => w.foreground) || pool[0] || null;
  }
  const byTitleOrProcess = pool.find(
    (w) => w.title.toLowerCase().includes(cleaned) || w.processName.toLowerCase().includes(cleaned)
  );
  return byTitleOrProcess || pool.find((w) => w.foreground) || pool[0] || null;
}

export default {
  name: 'look_at_screen',
  description:
    'Look at what is currently visible on the screen and describe or evaluate it, without taking any action or ' +
    'controlling anything. Use this for requests like "look at my screen and tell me what you think", "what does ' +
    'this say", or "is this window showing X" — never for a task where the user wants something actually done ' +
    '(use control_computer for that).',
  parameters: {
    type: 'object',
    properties: {
      question: {
        type: 'string',
        description: 'What the user wants to know or have evaluated about what\'s on screen. If they just said "look at my screen", describe generally.',
      },
      target: {
        type: 'string',
        description: 'Optional — which window to look at, by app name or part of its title (e.g. "chrome", "notepad"). Leave out to use whichever window is currently in front.',
      },
    },
    required: [],
  },
  async run({ question, target } = {}) {
    let windows = [];
    try {
      ({ windows } = await sendCommand('windows'));
    } catch {
      return { ok: false, error: "I couldn't reach the screen right now." };
    }

    const win = pickWindow(windows, target);
    if (!win && !windows.length) {
      return { ok: false, error: 'Nothing appears to be open on screen right now.' };
    }

    // The badge covers the whole "actually looking" span — from right before
    // the screenshot is captured through the model finishing its answer —
    // not just the instant sendCommand('screenshot') resolves, since the
    // point is to reflect real observation state, not just the moment the
    // OS-level pixel grab happens. Always released via finally, on every
    // return path below (success, model failure, or a thrown error).
    const observationToken = showIndicator('Jarvis is looking at your screen');
    try {
      let shot;
      try {
        shot = await sendCommand('screenshot', win ? { handle: win.handle } : {});
      } catch (err) {
        return { ok: false, error: err?.message || "I couldn't take a look at the screen." };
      }

      // Kept alongside control-session screenshots so there's one place to
      // review anything Jarvis has looked at, whether it acted or not.
      try {
        saveScreenshot(shot.base64, { label: win ? win.processName : 'screen' });
      } catch {
        // Not saving it is a lesser failure than not answering the question.
      }

      // A UIA text read is cheap and often more exact than a picture (real
      // button labels/values) — attached as extra context when available, and
      // it's also what lets a text-only model still answer instead of failing
      // outright on the vision requirement below.
      let elementsText = '';
      if (win) {
        try {
          const { elements } = await sendCommand('read_window', { handle: win.handle });
          if (elements?.length > 1) {
            elementsText = elements
              .slice(0, 100)
              .map((e) => `${e.role || 'element'}${e.name ? ` "${e.name}"` : ''}${e.value ? ` = "${String(e.value).slice(0, 200)}"` : ''}`)
              .join('\n');
          }
        } catch {
          // No readable tree — the screenshot alone is still useful.
        }
      }

      const askedQuestion = String(question || '').trim() || 'Describe what you see and share anything worth noting.';
      const windowNote = win ? `The window being looked at is "${win.title}" (${win.processName}).` : 'This is a capture of the whole screen — no single window was specified.';
      const promptText =
        `${windowNote}\n\n${elementsText ? `Its readable contents:\n${elementsText}\n\n` : ''}` +
        `The user asked: ${askedQuestion}\n\nAnswer plainly and conversationally, as if glancing over at their screen. Do not mention that you were given a screenshot or a list of elements — just answer.`;

      const result = await askModel({
        prompt: promptText,
        media: [{ kind: 'image', mimeType: shot.mimeType, dataBase64: shot.base64 }],
        need: { vision: true },
      });

      broadcast({ type: 'observed', target: win ? win.title : 'the screen' });

      if (!result.ok) {
        // A text-only fallback is only meaningful if we actually have real
        // window text to answer from — otherwise this is just failing quieter.
        if (elementsText) {
          const textOnly = await askModel({ prompt: promptText.replace(/\n\nThe user asked:/, '\n\n(No image is available for this model — answering from the window\'s text alone.)\n\nThe user asked:') });
          if (textOnly.ok) {
            return { ok: true, answer: textOnly.text, sawImage: false };
          }
        }
        return { ok: false, error: result.error };
      }

      return { ok: true, answer: result.text, sawImage: true };
    } finally {
      hideIndicator(observationToken);
    }
  },
};

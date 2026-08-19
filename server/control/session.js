// The computer-control loop: plan once, then perceive -> decide -> guard ->
// act -> verify, repeating until the goal is done, the model gives up, the
// user stops it, or a step cap is hit. Deliberately separate from
// models/runner.js's chat-turn loop (see the design plan's architecture
// notes) — this is long-running, carries screenshots, and must be
// independently cancellable without any risk of the model rescheduling
// itself mid-click via a tool like schedule_task.
//
// Never imports skills/index.js — see CLAUDE.md's circular-import
// invariant. Talks to models directly through the adapter layer, and to the
// screen/mouse/keyboard through ps-bridge.js.
//
// Only one session runs at a time (matching the one-overlay-at-a-time
// design) — `activeSession` below is the single source of truth server.js's
// routes read and act on.

import { send as sendCommand } from './ps-bridge.js';
import { evaluateAction, checkBlocklist } from './guard.js';
import { getSafetyConfig } from './safety.js';
import { showOverlay, updateStep, hideOverlay } from './overlay-bridge.js';
import { saveScreenshot } from './screenshot-store.js';
import { getAdapter } from '../adapters/index.js';
import { rankCandidates, controlTaskProfile } from '../models/router.js';
import { getPrefs } from '../prefs.js';
import { broadcast } from '../events.js';
// A direct import of one specific leaf skill file, NOT of skills/index.js —
// open_app.js has no dependency on the skill loader itself, so this doesn't
// create the circular-import edge the invariant forbids. Needed because the
// control loop otherwise has no way to launch an app that isn't already
// open — perceive() only ever looks at whatever window is currently in
// front.
import openApp from '../skills/open_app.js';
// connectors/index.js does not import skills/index.js (or anything that
// does), so pulling it in directly here doesn't cross the circular-import
// invariant — same reasoning as the open_app.js import above. This is what
// lets a control session call a connected service (Notion, a CLI tool, ...)
// mid-task, through the exact same guard/confirm discipline as a desktop
// action (see runControlSession's connector-tool branch below).
import { getToolDeclarations as getConnectorTools, runConnectorTool } from '../connectors/index.js';
import { askModel } from '../ai.js';

const MAX_STEPS = 25;
const MAX_ACTIONS_PER_BATCH = 4;
// px — small OS-rounding slack, not a real "did the user move it" false
// positive. JARVIS_DISABLE_MOUSE_GUARD is a verification-only escape hatch
// (see CLAUDE.md) for proving the rest of the loop completes a task without
// a real human's mouse activity — inevitable on a desktop actually in use —
// tripping the safety stop on every run. Never set outside of testing.
const CURSOR_DRIFT_TOLERANCE = process.env.JARVIS_DISABLE_MOUSE_GUARD ? Infinity : 4;
const DECIDE_TIMEOUT_MS = 45 * 1000; // generous for a slower free/reasoning model

/**
 * Races real work against both a timeout AND the session being stopped —
 * without this, pressing Stop while a model call is in flight wouldn't
 * actually interrupt anything until that call happened to resolve on its
 * own, since nothing else in the loop checks `session.stopped` while
 * `await`ing a single adapter call. There's no cancellation token wired
 * into the adapters, so the abandoned request itself keeps running in the
 * background and its result is simply discarded — a small, acceptable
 * leak per stopped/timed-out step, not a correctness problem.
 */
function raceAgainstStop(session, workPromise, timeoutMs) {
  workPromise.catch(() => {}); // swallow a late rejection from an abandoned call — already handled below if it wins the race
  return new Promise((resolve, reject) => {
    let settled = false;
    const finish = (fn, val) => {
      if (settled) return;
      settled = true;
      clearInterval(poller);
      clearTimeout(timer);
      fn(val);
    };
    const poller = setInterval(() => {
      if (session.stopped) finish(resolve, { interrupted: true, reason: 'stopped' });
    }, 250);
    const timer = setTimeout(() => finish(resolve, { interrupted: true, reason: 'timeout' }), timeoutMs);
    workPromise.then((value) => finish(resolve, { value })).catch((err) => finish(reject, err));
  });
}

const CONTROL_SYSTEM_INSTRUCTION = `You are Jarvis's computer-control agent. You operate the user's Windows computer directly — moving the mouse, clicking, typing, and reading what's on screen — to accomplish a stated goal.

Each turn you are shown every currently open window (title + program), the front one's title/process in more detail, and either a list of its on-screen elements (id, role, name, current value, position) or a screenshot when a readable element list isn't available. Elements are referenced by their "id" number from the list you were most recently shown — ids are not stable across turns, always use the ones from the latest list.

Important rules about other open windows:
- If the app you need is already open in the window list, switch to it (switch_window) rather than launching a new copy of it.
- Minimizing or switching away from a window is the correct way to move on from it when you're just done with it for NOW. Reuse and preserve the user's own existing workspace — treat anything that was already open as untouched unless the goal explicitly asks you to change it.
- You CAN close a window (close_window) when the goal genuinely calls for it, or when it's scratch you opened yourself and no longer need. Closing anything the user already had open, or anything you're not certain about, always pauses to ask them first — that confirmation happens automatically, you don't need to ask a second time in words. Closing only ever *requests* it (like clicking the window's own X) — the app itself may decline (e.g. an unsaved-changes prompt), which is expected, not a failure on your part.
- restore_window brings a minimized window back into view; arrange_window repositions/resizes one (useful for working with two windows side by side) — neither changes which window is focused.
- A task can span several already-open windows (e.g. copy something from one, paste it into another) — switch between them as needed, in any order, as many times as needed.
- When you're finished, Jarvis automatically tidies up any window IT opened for this task that turned out to be scratch (not needed once you're done) — you don't need to close those yourself first, just call report_done.

If a connected service's tool is offered to you (alongside the desktop actions below), use it exactly like any other tool when the goal calls for it — reaching for the right capability for the task, desktop or connected service, sometimes both in the same task.

You must always respond by calling exactly one of these tools:
- perform_actions: one or a few concrete actions to take right now (launch_app to open a program that isn't already running, switch_window/minimize_window/restore_window/arrange_window/close_window to change window state, click/double_click/right_click by element id, type text, press a key combo, scroll, or wait a couple of seconds for something slow to finish loading). Keep batches small and check the result before assuming a multi-step sequence worked.
- report_done: call this once the goal has genuinely been accomplished. Include a short summary of what you did.
- report_stuck: call this if you cannot proceed — wrong app open, needed element not found, an unexpected dialog, or anything else blocking progress. Explain why in plain language.
- (any connected-service tool offered to you this turn)

Never invent an element id or window handle that wasn't in the list you were just shown. If you're not sure what's on screen, take a smaller action (like clicking one element) and look again rather than guessing a long sequence.`;

// ---------- CONTROL_TOOLS: this loop's own small, fixed tool set ----------
// Not the general Skills tool list (server/skills/index.js) — a control
// session's available actions are these desktop primitives, merged at
// runtime with whatever connector tools are currently enabled (see
// runControlSession's `tools` build below) — never the chat skill catalog
// wholesale.

const CONTROL_TOOLS = [
  {
    name: 'perform_actions',
    description: 'Perform one or a few concrete actions on the currently focused window.',
    parameters: {
      type: 'object',
      properties: {
        reasoning: { type: 'string', description: 'One short sentence on why these actions move toward the goal.' },
        actions: {
          type: 'array',
          items: {
            type: 'object',
            properties: {
              kind: { type: 'string', enum: ['launch_app', 'switch_window', 'minimize_window', 'restore_window', 'arrange_window', 'close_window', 'click', 'double_click', 'right_click', 'type', 'key', 'scroll', 'wait'] },
              app: { type: 'string', description: 'For "launch_app" — a friendly app name, e.g. "notepad" or "calculator".' },
              windowHandle: { type: 'string', description: 'For "switch_window"/"minimize_window"/"restore_window"/"arrange_window"/"close_window" — the "handle" of a window from the current open-windows list.' },
              x: { type: 'number', description: 'For "arrange_window" — the new left position, in screen pixels.' },
              y: { type: 'number', description: 'For "arrange_window" — the new top position, in screen pixels.' },
              width: { type: 'number', description: 'For "arrange_window" — the new width, in pixels.' },
              height: { type: 'number', description: 'For "arrange_window" — the new height, in pixels.' },
              elementId: { type: 'number', description: 'For click/double_click/right_click — the id from the last element list.' },
              text: { type: 'string', description: 'For "type" — the text to type.' },
              combo: { type: 'string', description: 'For "key" — e.g. "Enter", "Ctrl+S", "Tab".' },
              direction: { type: 'string', enum: ['up', 'down'], description: 'For "scroll".' },
              amount: { type: 'number', description: 'For "scroll" — notches, default 3.' },
              seconds: { type: 'number', description: 'For "wait" — how long to pause, max 5 seconds.' },
            },
            required: ['kind'],
          },
        },
      },
      required: ['actions'],
    },
  },
  {
    name: 'report_done',
    description: 'Call once the goal has genuinely been accomplished.',
    parameters: { type: 'object', properties: { summary: { type: 'string' } }, required: ['summary'] },
  },
  {
    name: 'report_stuck',
    description: 'Call if you cannot proceed toward the goal.',
    parameters: { type: 'object', properties: { reason: { type: 'string' } }, required: ['reason'] },
  },
];

let activeSession = null; // { stopped, stopReason, pendingConfirmation: {resolve}|null, pendingSummary }

function newId() {
  return `cs${Date.now().toString(36)}${Math.random().toString(36).slice(2, 8)}`;
}

function pickModelEntry() {
  const prefs = getPrefs();
  const ranked = rankCandidates(controlTaskProfile(), { balance: prefs.balance });
  return ranked[0] || null;
}

function formatElements(elements) {
  return elements
    .map((e) => {
      const bits = [`#${e.id}`, e.role || 'element'];
      if (e.name) bits.push(`"${e.name}"`);
      if (e.value != null && e.value !== '') bits.push(`value="${String(e.value).slice(0, 200)}"`);
      if (e.enabled === false) bits.push('(disabled)');
      return bits.join(' ');
    })
    .join('\n');
}

function elementCenter(el) {
  const { left, right, top, bottom } = el.bounds;
  return { x: Math.round((left + right) / 2), y: Math.round((top + bottom) / 2) };
}

/** One line per open window — so the model knows what it CAN switch to, not just what's currently in front. */
function formatWindowList(windows) {
  return windows
    .map((w) => `handle=${w.handle}: "${w.title}" (${w.processName})${w.foreground ? ' — currently in front' : ''}`)
    .join('\n');
}

/** Waits for the user's yes/no on one risky action batch — resolved by requestConfirmation()/server.js's /api/control/confirm route, or immediately false if the session is stopped first. */
function awaitConfirmation(session, summary) {
  return new Promise((resolve) => {
    session.pendingConfirmation = { resolve };
    session.pendingSummary = summary;
    updateStep(`Waiting for your OK: ${summary}`);
    broadcast({ type: 'control_confirm_needed', summary });
  });
}

function clearConfirmation(session) {
  session.pendingConfirmation = null;
  session.pendingSummary = null;
}

async function perceive(session, entry) {
  const { windows } = await sendCommand('windows');
  const front = windows.find((w) => w.foreground) || windows[0];
  if (!front) return { front: null, windows, description: 'Nothing appears to be open right now.', elements: [] };

  const blocklist = checkBlocklist({ windowTitle: front.title, processName: front.processName }, getSafetyConfig());
  if (blocklist.blocked) return { front, windows, blocked: blocklist };

  await sendCommand('focus', { handle: front.handle });

  let elements = [];
  let description;
  try {
    const result = await sendCommand('read_window', { handle: front.handle });
    elements = result.elements || [];
  } catch {
    elements = [];
  }

  const windowListText = `Open windows:\n${formatWindowList(windows)}`;

  // Fewer than 2 elements (just the window root, or nothing) means the tree
  // didn't give us anything useful — fall back to a screenshot, but only if
  // the chosen model can actually see images; otherwise stay text-only
  // rather than silently sending a picture nobody can read.
  let media;
  if (elements.length < 2) {
    if (entry.caps?.vision) {
      try {
        const shot = await sendCommand('screenshot', { handle: front.handle });
        saveScreenshot(shot.base64, { label: front.processName });
        media = [{ kind: 'image', mimeType: shot.mimeType, dataBase64: shot.base64 }];
        description = `${windowListText}\n\nFront window: "${front.title}" (${front.processName}). Its contents couldn't be read as text — a screenshot is attached instead.`;
      } catch {
        description = `${windowListText}\n\nFront window: "${front.title}" (${front.processName}). No readable contents and no screenshot available.`;
      }
    } else {
      description = `${windowListText}\n\nFront window: "${front.title}" (${front.processName}). Its contents couldn't be read as text, and the current model can't view screenshots — working from the window title only.`;
    }
  } else {
    description = `${windowListText}\n\nFront window: "${front.title}" (${front.processName})\nElements:\n${formatElements(elements)}`;
  }

  return { front, windows, elements, description, media };
}

async function actOnBatch(session, entry, front, windows, elements, actions) {
  const results = [];
  for (const action of actions.slice(0, MAX_ACTIONS_PER_BATCH)) {
    if (session.stopped) {
      results.push({ action, ok: false, error: 'Stopped before this action ran.' });
      break;
    }

    let x, y;
    if (action.elementId != null) {
      const el = elements.find((e) => e.id === action.elementId);
      if (!el) {
        results.push({ action, ok: false, error: `No element #${action.elementId} in the last list — it may be stale.` });
        continue;
      }
      ({ x, y } = elementCenter(el));
    }

    // switch_window/minimize_window/restore_window/arrange_window/
    // close_window all act on a DIFFERENT window than whatever is currently
    // in front — the guard check (and the confirmation summary below) must
    // evaluate that target window, not `front`, or an action against an
    // off-limits window would sail straight past the blocklist check that
    // was written for "the window an action touches", back when every action
    // always touched the front window.
    let targetWindow = front;
    if (['switch_window', 'minimize_window', 'restore_window', 'arrange_window', 'close_window'].includes(action.kind)) {
      const match = windows.find((w) => w.handle === action.windowHandle);
      if (!match) {
        results.push({ action, ok: false, error: `No open window with handle "${action.windowHandle}" — it may have closed. Look at the current window list again.` });
        continue;
      }
      targetWindow = match;
    }

    const evaluation = evaluateAction(
      { kind: action.kind, label: action.text || action.combo || action.app || '', description: action.reasoning || '' },
      { windowTitle: targetWindow.title, processName: targetWindow.processName },
      getSafetyConfig()
    );

    if (!evaluation.allowed) {
      session.stopped = true;
      session.stopReason = evaluation.reason;
      results.push({ action, ok: false, error: evaluation.reason });
      break;
    }

    if (evaluation.needsConfirm) {
      const summary =
        action.kind === 'close_window'
          ? `close "${targetWindow.title}"`
          : `${action.kind}${action.text ? ` "${action.text}"` : ''}${action.combo ? ` (${action.combo})` : ''} in "${targetWindow.title}"`;
      const approved = await awaitConfirmation(session, summary);
      clearConfirmation(session);
      if (session.stopped) {
        results.push({ action, ok: false, error: 'Stopped.' });
        break;
      }
      if (!approved) {
        results.push({ action, ok: false, error: 'The user declined this action.' });
        continue;
      }
    }

    try {
      updateStep(`${action.kind}${action.text ? `: "${action.text}"` : action.combo ? `: ${action.combo}` : action.app ? `: ${action.app}` : ''}`);

      if (action.kind === 'launch_app') {
        const opened = await openApp.run({ app: action.app || '' });
        if (!opened.ok) {
          results.push({ action, ok: false, error: opened.error || `Could not open "${action.app}".` });
          continue;
        }
        // Apps take a moment to actually create their window — the next
        // PERCEIVE would otherwise still see whatever was in front before.
        await new Promise((resolve) => setTimeout(resolve, 900));
        // Temporary task context (see runControlSession's cleanup step):
        // whatever window handle(s) show up now that weren't in the list
        // this batch started with are Jarvis's own — remembered so they can
        // be auto-tidied at the end if they turn out to be scratch. Only
        // launch_app triggers this (not every perceive), matching the
        // requirement's own "windows it created for the task", not
        // incidental dialogs from clicking within an already-tracked window.
        try {
          const { windows: afterLaunch } = await sendCommand('windows');
          const priorHandles = new Set(windows.map((w) => w.handle));
          for (const w of afterLaunch) {
            if (!priorHandles.has(w.handle)) session.createdHandles.add(w.handle);
          }
        } catch {
          // Best-effort — worst case this one window doesn't get auto-tidied
          // later and the user sees one extra scratch window, not a failure.
        }
        results.push({ action, ok: true });
        break; // the window landscape just changed — re-perceive before anything else
      } else if (action.kind === 'switch_window') {
        await sendCommand('focus', { handle: targetWindow.handle });
        results.push({ action, ok: true });
        break; // front window changed — everything after this needs a fresh PERCEIVE
      } else if (action.kind === 'minimize_window') {
        await sendCommand('minimize_window', { handle: targetWindow.handle });
        results.push({ action, ok: true });
        break; // same reasoning — the window landscape changed
      } else if (action.kind === 'restore_window') {
        await sendCommand('restore_window', { handle: targetWindow.handle });
        results.push({ action, ok: true });
        break;
      } else if (action.kind === 'arrange_window') {
        await sendCommand('arrange_window', { handle: targetWindow.handle, x: action.x, y: action.y, width: action.width, height: action.height });
        results.push({ action, ok: true });
        break;
      } else if (action.kind === 'close_window') {
        // Guard already routed this through the confirm gate above (risky —
        // see guard.js) before we ever get here, whether the target is
        // something the user already had open or something Jarvis itself
        // created mid-task. Only the automatic end-of-session cleanup in
        // runControlSession bypasses that gate, and it calls close_window
        // directly rather than through this model-driven path.
        await sendCommand('close_window', { handle: targetWindow.handle });
        results.push({ action, ok: true });
        break; // the window landscape changed (or the app may now be showing its own prompt) — re-perceive
      } else if (action.kind === 'wait') {
        const ms = Math.min(Math.max(Number(action.seconds) || 1, 0), 5) * 1000;
        await new Promise((resolve) => setTimeout(resolve, ms));
      } else if (['click', 'double_click', 'right_click'].includes(action.kind)) {
        if (x == null || y == null) {
          results.push({ action, ok: false, error: 'No target position for this click.' });
          continue;
        }
        // A real user-driven mouse move between our last commanded position
        // and now means someone took the mouse back — stop rather than
        // click out from under them. Skipped on the very first mouse action
        // of a session (no baseline yet).
        if (session.lastCursor) {
          const cursor = await sendCommand('cursor');
          const drift = Math.hypot(cursor.x - session.lastCursor.x, cursor.y - session.lastCursor.y);
          if (drift > CURSOR_DRIFT_TOLERANCE) {
            session.stopped = true;
            session.stopReason = 'I noticed you moved the mouse yourself, so I stopped.';
            results.push({ action, ok: false, error: session.stopReason });
            break;
          }
        }
        await sendCommand(action.kind, { x, y });
        session.lastCursor = { x, y };
      } else if (action.kind === 'type') {
        await sendCommand('type', { text: action.text || '' });
      } else if (action.kind === 'key') {
        await sendCommand('key', { combo: action.combo || 'Enter' });
      } else if (action.kind === 'scroll') {
        await sendCommand('scroll', { direction: action.direction || 'down', amount: action.amount || 3 });
      } else {
        // An unrecognized or missing `kind` must be reported back as a
        // failure, not silently treated as a successful no-op — otherwise
        // a model producing malformed actions gets no signal that nothing
        // actually happened and can loop indefinitely repeating the mistake.
        results.push({ action, ok: false, error: `Unknown action kind: "${action.kind}". Use one of launch_app/switch_window/minimize_window/restore_window/arrange_window/close_window/click/double_click/right_click/type/key/scroll/wait.` });
        continue;
      }

      results.push({ action, ok: true });
    } catch (err) {
      results.push({ action, ok: false, error: err?.message || 'Action failed.' });
    }
  }
  return results;
}

// A window titled with a leading "*" or the word "unsaved" is never
// auto-closed, full stop — not even offered to the model as a candidate.
// Confirmed live against a real app: Windows 11's own Notepad titles an
// unsaved tab exactly this way ("*hello - Notepad"). A heuristic, not a
// guarantee every app follows this convention, but erring toward leaving a
// window open costs nothing; erring toward closing one could lose work.
function looksUnsaved(title) {
  return /^\*/.test(title) || /unsaved/i.test(title);
}

/**
 * Temporary task context cleanup (3.2) — runs once, right before a session
 * reports done. Closes whichever of the session's OWN created windows are
 * still open, EXCEPT ones the model says hold the actual result and ones
 * that look unsaved — with no confirmation prompt, since these are Jarvis's
 * own scratch, not anything the user had open. Calls close_window directly
 * rather than going through actOnBatch/guard.js — that confirm path exists
 * for the user's own windows and for anything the model decides mid-task to
 * close, not for this deterministic end-of-session tidy-up.
 */
async function cleanUpOwnScratch(session, entry, goalSummary) {
  let windows;
  try {
    ({ windows } = await sendCommand('windows'));
  } catch {
    return; // can't see what's open — safest to leave everything alone
  }

  const stillOpenCreated = windows.filter((w) => session.createdHandles.has(w.handle));
  if (!stillOpenCreated.length) return;

  const keepAlways = stillOpenCreated.filter((w) => looksUnsaved(w.title));
  const candidates = stillOpenCreated.filter((w) => !looksUnsaved(w.title));
  if (!candidates.length) return;

  updateStep('Tidying up…');

  // Default to keeping everything if we can't ask (no model available, or
  // it fails) — a leftover scratch window is a minor annoyance; wrongly
  // closing one that held the actual result is not.
  let keepHandles = new Set(candidates.map((w) => w.handle));
  const listText = candidates.map((w) => `handle=${w.handle}: "${w.title}" (${w.processName})`).join('\n');
  const result = await askModel({
    prompt:
      `This task just finished: "${goalSummary}"\n\nJarvis opened these windows itself while working on it, still open now:\n${listText}\n\n` +
      `Which of these, if any, actually hold the result the user needs to see and should stay open? Everything else was scratch and will be closed automatically.`,
    system: 'Reply with JSON only: {"keep": ["<handle>", ...]}. An empty array means none of them need to stay open.',
    json: true,
    modelId: entry.id,
  });
  if (result.ok && Array.isArray(result.data?.keep)) {
    keepHandles = new Set(result.data.keep.map(String));
  }

  for (const w of candidates) {
    if (keepHandles.has(w.handle)) continue;
    try {
      await sendCommand('close_window', { handle: w.handle });
    } catch {
      // Not fatal — worst case one scratch window is left behind.
    }
  }
  if (keepAlways.length) {
    updateStep(`Left ${keepAlways.length} window(s) with unsaved work open.`);
  }
}

async function planTask(goal) {
  const entry = pickModelEntry();
  if (!entry) return { entry: null, planText: null };
  const adapter = getAdapter(entry.adapter);
  const messages = [
    {
      role: 'user',
      text: `The user asked for this to be done on their computer: "${goal}"\n\nGive a short plain-language plan (2-5 steps) for how you'll approach it. This is shown to the user for approval before anything happens — no tool calls, just the plan as plain text.`,
    },
  ];
  let text = '';
  for await (const ev of adapter.stream(entry, messages, { systemOverride: CONTROL_SYSTEM_INSTRUCTION })) {
    if (ev.type === 'chunk') text += ev.text;
    else if (ev.type === 'final') text = ev.text || text;
  }
  return { entry, planText: text || `I'll work toward: ${goal}` };
}

/** Generates and returns a short plan for approval — does not act. Used by skills/control_computer.js's confirm-gate summarize() step. */
export async function preparePlan(goal) {
  const { planText } = await planTask(goal);
  return planText;
}

/** Runs the full control loop for `goal`, using `planText` if already generated (avoids a second planning model call when the caller already has one from preparePlan()). Resolves with `{status, summary?, reason?}` where status is 'done'|'stuck'|'stopped'|'blocked'|'error'. */
export async function runControlSession(goal, planText) {
  if (activeSession) {
    return { status: 'error', reason: "Jarvis is already controlling your computer — stop that first." };
  }

  const session = {
    id: newId(),
    stopped: false,
    stopReason: null,
    pendingConfirmation: null,
    pendingSummary: null,
    lastCursor: null,
    originalFrontHandle: null,
    // Temporary task context (3.2): every window handle present before this
    // session touched anything, vs. every handle that appeared because of a
    // launch_app it performed — the two sets `cleanUpOwnScratch()` diffs
    // against at the end. See actOnBatch's launch_app branch for how
    // createdHandles actually gets populated.
    preExistingHandles: new Set(),
    createdHandles: new Set(),
  };
  activeSession = session;

  try {
    const entry = pickModelEntry();
    if (!entry) {
      return { status: 'error', reason: 'No model set up right now can do this — add or fix one in Model Settings.' };
    }
    const adapter = getAdapter(entry.adapter);
    const plan = planText || (await planTask(goal)).planText;

    // Politeness: remember whatever was in front before Jarvis touched
    // anything, so it can switch back once done — the desktop should look
    // like Jarvis was never there beyond the task it was actually asked to
    // do. Best-effort only; failing to read this must never block starting.
    try {
      const { windows: startWindows } = await sendCommand('windows');
      session.originalFrontHandle = startWindows.find((w) => w.foreground)?.handle || null;
      session.preExistingHandles = new Set(startWindows.map((w) => w.handle));
    } catch {
      session.originalFrontHandle = null;
    }

    showOverlay(`Starting: ${goal}`);

    // Connector tools merge into the fixed desktop-primitive set (3.4) —
    // built once per session, not re-fetched every decide-turn, since a
    // connector genuinely changing mid-session is rare enough not to be
    // worth the extra call, and every provider needs the identical `tools`
    // array on every turn regardless.
    const connectorDeclarations = getConnectorTools();
    const tools = [...CONTROL_TOOLS, ...connectorDeclarations];

    const messages = [
      {
        role: 'user',
        text: `Goal: ${goal}\n\nPlan you already shared and the user approved:\n${plan}\n\nBegin. You'll be shown the current window's contents after each step.`,
      },
    ];

    for (let step = 0; step < MAX_STEPS; step++) {
      if (session.stopped) break;

      const perceived = await perceive(session, entry);
      if (perceived.blocked) {
        session.stopped = true;
        session.stopReason = perceived.blocked.reason;
        break;
      }
      if (!perceived.front) {
        return { status: 'stuck', reason: 'Nothing appears to be open to work with.' };
      }

      messages.push({ role: 'user', text: perceived.description, media: perceived.media });

      const decideWork = (async () => {
        let callEvent = null;
        let finalText = '';
        for await (const ev of adapter.stream(entry, messages, { tools, systemOverride: CONTROL_SYSTEM_INSTRUCTION })) {
          if (ev.type === 'call') callEvent = ev;
          else if (ev.type === 'final') finalText = ev.text;
        }
        return { callEvent, finalText };
      })();

      let decideOutcome;
      try {
        decideOutcome = await raceAgainstStop(session, decideWork, DECIDE_TIMEOUT_MS);
      } catch (err) {
        return { status: 'error', reason: err?.message || 'The model failed mid-task.' };
      }
      if (decideOutcome.interrupted) {
        if (decideOutcome.reason === 'stopped') break; // fall through to the normal stopped-status handling below
        return { status: 'error', reason: 'The model took too long to respond.' };
      }

      const { callEvent, finalText } = decideOutcome.value;
      if (!callEvent) {
        // A well-behaved model always calls one of our three tools — a bare
        // text reply instead is treated as an implicit "stuck" rather than
        // looped on indefinitely.
        return { status: 'stuck', reason: finalText || "The model didn't take a clear next step." };
      }

      messages.push({ role: 'assistant', toolCalls: callEvent.calls, raw: callEvent.raw, modelId: entry.id });

      // The system instruction asks for exactly one tool call per turn, but
      // nothing stops a model from ignoring that — every call in
      // callEvent.calls needs a matching entry in the toolResults pushed
      // back, or the next adapter call can 400 (Anthropic in particular
      // requires one tool_result per tool_use in the preceding message).
      // Only the first meaningful call (report_done/report_stuck take
      // priority over perform_actions) is actually acted on; any others get
      // a harmless "ignored" result so the counts always line up.
      const call = callEvent.calls.find((c) => c.name === 'report_done') ||
        callEvent.calls.find((c) => c.name === 'report_stuck') ||
        callEvent.calls.find((c) => c.name === 'perform_actions') ||
        callEvent.calls[0];
      const extras = callEvent.calls.filter((c) => c !== call);
      const extraResults = extras.map((c) => ({
        id: c.id,
        name: c.name,
        result: { ok: false, ignored: true, note: 'Only one tool call is processed per turn.' },
      }));

      if (call.name === 'report_done') {
        const summary = call.args?.summary || 'Done.';
        await cleanUpOwnScratch(session, entry, summary);
        return { status: 'done', summary };
      }
      if (call.name === 'report_stuck') {
        return { status: 'stuck', reason: call.args?.reason || "The model couldn't proceed." };
      }

      if (call.name === 'perform_actions') {
        const actions = Array.isArray(call.args?.actions) ? call.args.actions : [];
        const results = await actOnBatch(session, entry, perceived.front, perceived.windows, perceived.elements, actions);
        messages.push({
          role: 'tool',
          toolResults: [
            { id: call.id, name: call.name, result: { ok: !session.stopped, actions: results } },
            ...extraResults,
          ],
        });
      } else {
        // A connector tool — reuses the exact same confirm mechanism as a
        // risky desktop action (awaitConfirmation), so there's one unified
        // "Jarvis is asking permission" experience during a control session
        // regardless of whether what it wants to do is on the desktop or in
        // a connected service.
        const decl = connectorDeclarations.find((d) => d.name === call.name);
        let toolResult;
        if (!decl) {
          toolResult = { ok: false, error: `Unknown tool: "${call.name}".` };
        } else if (decl.confirm === 'always') {
          const approved = await awaitConfirmation(session, `use "${decl.description || decl.name}"`);
          clearConfirmation(session);
          if (session.stopped) toolResult = { ok: false, error: 'Stopped.' };
          else if (!approved) toolResult = { ok: false, error: 'The user declined this action.' };
        }
        if (decl && toolResult === undefined) {
          try {
            toolResult = await runConnectorTool(decl.name, call.args || {});
          } catch (err) {
            toolResult = { ok: false, error: err?.message || 'That connector ran into a problem.' };
          }
        }
        messages.push({
          role: 'tool',
          toolResults: [{ id: call.id, name: call.name, result: toolResult }, ...extraResults],
        });
      }

      if (session.stopped) break;
    }

    if (session.stopped) {
      return { status: 'stopped', reason: session.stopReason || 'Stopped.' };
    }
    return { status: 'stuck', reason: "Ran out of steps without finishing — the task may be more involved than expected." };
  } finally {
    // Best-effort restore of whatever was in front when this session
    // started — runs on every exit path (done, stuck, stopped, error) since
    // `finally` always executes. A window that's since closed, or a
    // ps-bridge hiccup, must never turn a clean finish into a reported
    // error over something this cosmetic.
    if (session.originalFrontHandle) {
      try {
        await sendCommand('focus', { handle: session.originalFrontHandle });
      } catch {
        // Nothing to do — the original window may have closed since.
      }
    }
    activeSession = null;
    hideOverlay();
  }
}

/** True/false answer to a pending risky-action confirmation — server.js's POST /api/control/confirm. */
export function confirmPendingAction(approved) {
  if (!activeSession?.pendingConfirmation) {
    return { ok: false, error: 'Nothing is waiting for confirmation right now.' };
  }
  const { resolve } = activeSession.pendingConfirmation;
  activeSession.pendingConfirmation = null;
  resolve(Boolean(approved));
  return { ok: true };
}

/** Stops the active session immediately (or is a no-op if nothing is running) — every stop path (overlay button, hotkey, in-page button) ends up here via server.js's POST /api/control/stop. */
export function requestStop() {
  if (!activeSession) return { ok: false, error: 'Nothing is running.' };
  activeSession.stopped = true;
  activeSession.stopReason = 'Stopped by the user.';
  if (activeSession.pendingConfirmation) {
    activeSession.pendingConfirmation.resolve(false);
    activeSession.pendingConfirmation = null;
  }
  return { ok: true };
}

export function getSessionStatus() {
  if (!activeSession) return { active: false };
  return {
    active: true,
    awaitingConfirmation: Boolean(activeSession.pendingConfirmation),
    pendingSummary: activeSession.pendingSummary || null,
  };
}

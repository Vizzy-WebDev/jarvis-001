// Availability — whether Jarvis can actually reach the user live right now,
// the gate a Tier 1 finding needs to clear before speak.js attempts real
// proactive speech (unavailable never drops a finding; it just falls back
// to the existing persisted-notification + next-turn-injection path).
//
// Two separate signals, deliberately not folded into one function: the
// caller decides how to combine them, because they mean different things.
// `isReachable()` is the hard requirement — no plausible way to deliver a
// live message at all without it. `isBusy()` is the secondary dampener the
// build spec describes ("appears to be in a call or clearly busy") — real
// for ordinary flow, but a genuine emergency (decision.js's own verdict)
// should still be allowed to try to reach the user through it, the same way
// an emergency already breaks through quiet hours.

import { clientCount } from '../events.js';
import { getActiveId, getLastUserMessageAt } from '../chat-store.js';
import { send as sendCommand } from '../control/ps-bridge.js';

const IDLE_WINDOW_MS = 30 * 60 * 1000; // how recently the user must have actually said something for "available" to mean anything

// A small, starting-guess list of call-app process names — the accepted
// tradeoff of this option (see root CLAUDE.md's Heartbeat section): a call
// happening inside a browser tab is not detectable this way at all, and
// this will likely need real tuning after use. Advisory only — a false
// negative here just means a proactive message wasn't held back when maybe
// it should have been; never the thing standing between a finding and it
// being lost.
const BUSY_PROCESS_NAMES = ['zoom', 'teams', 'discord', 'skype', 'webex', 'gotomeeting', 'lync'];

/** A tab is connected AND the user was actually active recently — the hard requirement for any live delivery attempt. */
export function isReachable() {
  if (clientCount() === 0) return false;
  const activeId = getActiveId();
  const lastAt = activeId ? getLastUserMessageAt(activeId) : null;
  if (!lastAt) return false;
  const idleMs = Date.now() - new Date(lastAt).getTime();
  return Number.isFinite(idleMs) && idleMs <= IDLE_WINDOW_MS;
}

/** The secondary, skippable-for-emergencies dampener — best-effort; an unreachable control bridge (never set up, or genuinely down) is read as "can't tell, don't hold back on this alone," never as "busy." */
export async function isBusy() {
  try {
    const { processes } = await sendCommand('processes');
    const names = (processes || []).map((p) => String(p.name || '').toLowerCase());
    return BUSY_PROCESS_NAMES.some((busy) => names.some((n) => n.includes(busy)));
  } catch {
    return false;
  }
}

/** The combined, ordinary-flow check speak.js's caller uses for a non-emergency Tier 1 finding — reachable AND not apparently busy. An emergency path should call isReachable() alone instead (see this file's own header comment on why). */
export async function isAvailable() {
  if (!isReachable()) return false;
  return !(await isBusy());
}

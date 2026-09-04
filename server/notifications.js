// A general-purpose notification store (data/notifications.json), replacing
// the old #banner that lived directly in the app-screen layout — the reason
// that banner had to reserve permanent space near the mic/composer even when
// nothing was showing (see CLAUDE.md/handoff.md). Anything Jarvis needs to
// tell the user — an error, a warning, a completed scheduled task, a
// connector status change — becomes a notification instead: pushed live to
// any open tab over the existing SSE channel (events.js), and persisted here
// so it's still there if no tab was open when it happened (an overnight
// scheduled-task failure, for instance).
//
// A leaf module, same shape as scheduler/task-store.js: imports only
// store.js and events.js, so anything under server/skills/ (or the
// scheduler, or a connector) can call addNotification() directly without
// tripping the "nothing under server/skills/ may import the loader"
// circular-import invariant in CLAUDE.md.
//
// `kind` is a free string (task_run, monitor, connector, voice, system, ...)
// — deliberately not an enum. A new notification type needs no change here
// or in the client; this is what "reusable system" means in practice.

import { readJson, writeJson } from './store.js';
import { broadcast } from './events.js';

const FILE = 'notifications';
const MAX_KEPT = 200;

// A repeating fault (the confirmed live case this closes: a front-end "stuck"
// watchdog false-firing on nearly every turn while every model was
// quota-exhausted) used to unshift a brand-new, otherwise-identical row every
// single time — 102 of the store's 200 kept rows ended up being the exact
// same message, pushing real findings (self-diagnosis, security, job
// escalations) out of the 200-row cap entirely. Collapsing a genuine repeat
// into one row with a count is the fix; it must never merge two UNRELATED
// notifications that just happen to share wording by coincidence within the
// window, so the match is exact (kind + title) and only ever against the
// single newest row, never a scan back through history.
const DEDUP_WINDOW_MS = 10 * 60 * 1000;

function load() {
  return readJson(FILE, { notifications: [] });
}

function save(data) {
  writeJson(FILE, data);
}

function makeId() {
  return `n${Date.now().toString(36)}${Math.random().toString(36).slice(2, 8)}`;
}

/**
 * Records a notification and pushes it live to any open tab. `level` is
 * 'info' | 'success' | 'warning' | 'error' (defaults to 'info'). `action`,
 * if given, is `{label, section}` — a nav.js section id rather than a
 * callback, so it survives JSON and still works coming from a
 * server-generated event with no client code involved in choosing it.
 */
export function addNotification({ kind = 'system', level = 'info', title, body, action, meta } = {}) {
  if (!title) throw new Error('A notification needs a title.');
  const data = load();

  // Collapse into the newest row instead of adding a new one only when it's
  // genuinely the same fault repeating: same kind+title, still unread (once
  // the user has actually seen and dismissed it, a later repeat is worth a
  // fresh row again), and recent enough that this reads as "still going on"
  // rather than "happened again much later."
  const [mostRecent] = data.notifications;
  if (mostRecent && !mostRecent.read && mostRecent.kind === kind && mostRecent.title === title) {
    const age = Date.now() - new Date(mostRecent.ts).getTime();
    if (age >= 0 && age <= DEDUP_WINDOW_MS) {
      mostRecent.count = (mostRecent.count || 1) + 1;
      mostRecent.ts = new Date().toISOString();
      // A later repeat's own body/action/meta can differ in detail even
      // when the title is identical (e.g. a slightly different reason
      // string) — keep the freshest one visible rather than the first.
      mostRecent.body = body || mostRecent.body;
      mostRecent.action = action || mostRecent.action;
      mostRecent.meta = meta || mostRecent.meta;
      save(data);
      broadcast({ type: 'notification', notification: mostRecent });
      return mostRecent;
    }
  }

  const notification = {
    id: makeId(),
    kind,
    level,
    title,
    body: body || '',
    action: action || null,
    meta: meta || null,
    ts: new Date().toISOString(),
    read: false,
    count: 1,
  };
  data.notifications.unshift(notification);
  data.notifications = data.notifications.slice(0, MAX_KEPT);
  save(data);
  broadcast({ type: 'notification', notification });
  return notification;
}

export function listNotifications({ limit } = {}) {
  const all = load().notifications;
  return typeof limit === 'number' ? all.slice(0, limit) : all;
}

export function unreadCount() {
  return load().notifications.filter((n) => !n.read).length;
}

export function markRead(ids) {
  const idSet = new Set(Array.isArray(ids) ? ids : [ids]);
  const data = load();
  let changed = false;
  for (const n of data.notifications) {
    if (idSet.has(n.id) && !n.read) {
      n.read = true;
      changed = true;
    }
  }
  if (changed) save(data);
  return data.notifications;
}

export function markAllRead() {
  const data = load();
  let changed = false;
  for (const n of data.notifications) {
    if (!n.read) {
      n.read = true;
      changed = true;
    }
  }
  if (changed) save(data);
  return data.notifications;
}

export function removeNotification(id) {
  const data = load();
  data.notifications = data.notifications.filter((n) => n.id !== id);
  save(data);
}

export function clearAll() {
  save({ notifications: [] });
}

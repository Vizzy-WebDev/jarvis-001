// The general-purpose notification system that replaces the old in-layout
// #banner — see CLAUDE.md/handoff.md for why that banner had to reserve
// permanent space near the mic/composer even when nothing was showing.
// Three pieces, all backed by the same server store (server/notifications.js):
//   - a toast that appears briefly (#toast-host, fixed, out of layout flow —
//     it can never push/resize/reposition the orb, mic, composer, or rail)
//   - a bell icon + unread badge in the header, opening a small popover with
//     recent notifications (reuses screens/_ui.js's popover())
//   - a full history at #/notifications (screens/notifications.js)
//
// `kind` is a free string on purpose — any future notification type (a new
// skill, a new monitor kind, whatever) needs no change here at all; it just
// calls notify({kind: 'something-new', ...}).

import { navigate } from './router.js';
import { popover } from './screens/_ui.js';

const TOAST_MS = { info: 6000, success: 6000, warning: 7000, error: 9000 };
const MAX_TOASTS = 3;

let notifications = []; // most-recent-first, mirrors the server store
let seenIds = new Set();
let badgeEl = null;
let toastHost = null;

function timeAgo(iso) {
  const ms = Date.now() - new Date(iso).getTime();
  const min = Math.floor(ms / 60000);
  if (min < 1) return 'just now';
  if (min < 60) return `${min}m ago`;
  const hr = Math.floor(min / 60);
  if (hr < 24) return `${hr}h ago`;
  return `${Math.floor(hr / 24)}d ago`;
}

function unreadCount() {
  return notifications.filter((n) => !n.read).length;
}

function updateBadge() {
  if (!badgeEl) return;
  const count = unreadCount();
  badgeEl.textContent = count > 9 ? '9+' : String(count);
  badgeEl.classList.toggle('hidden', count === 0);
}

/** Runs a notification's `action` (a nav.js section id — see notifications.js's server side), if it has one. */
function runAction(n) {
  if (n.action?.section) navigate(n.action.section);
}

async function markReadRemote(ids) {
  try {
    await fetch('/api/notifications/read', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ids }),
    });
  } catch {
    // Local state already updated below regardless — a failed sync just
    // means the badge could be briefly wrong after a reload, not lost data.
  }
}

function markReadLocal(ids) {
  const idSet = new Set(ids);
  let changed = false;
  for (const n of notifications) {
    if (idSet.has(n.id) && !n.read) {
      n.read = true;
      changed = true;
    }
  }
  if (changed) updateBadge();
}

// ---------- toast ----------

function showToast(n) {
  if (!toastHost) return;
  while (toastHost.children.length >= MAX_TOASTS) {
    toastHost.firstChild.remove();
  }

  const el = document.createElement('div');
  el.className = `toast level-${n.level}`;

  const body = document.createElement('div');
  body.className = 'toast-body';
  const title = document.createElement('p');
  title.className = 'toast-title';
  title.textContent = n.title;
  body.appendChild(title);
  if (n.body) {
    const text = document.createElement('p');
    text.className = 'toast-text';
    text.textContent = n.body;
    body.appendChild(text);
  }
  if (n.action?.label) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'toast-action';
    btn.textContent = n.action.label;
    btn.addEventListener('click', () => {
      dismiss();
      markReadLocal([n.id]);
      markReadRemote([n.id]);
      runAction(n);
    });
    body.appendChild(btn);
  }

  const close = document.createElement('button');
  close.type = 'button';
  close.className = 'toast-close';
  close.setAttribute('aria-label', 'Dismiss');
  close.textContent = '✕';
  close.addEventListener('click', () => dismiss());

  el.append(body, close);
  toastHost.appendChild(el);

  let timer = null;
  function dismiss() {
    clearTimeout(timer);
    el.classList.add('leaving');
    setTimeout(() => el.remove(), 200);
  }
  function arm() {
    timer = setTimeout(dismiss, TOAST_MS[n.level] || TOAST_MS.info);
  }
  el.addEventListener('mouseenter', () => clearTimeout(timer));
  el.addEventListener('mouseleave', arm);
  arm();
}

// ---------- panel (bell popover) ----------

function buildPanel(body, close) {
  const list = document.createElement('div');
  list.className = 'notif-panel-list';

  const recent = notifications.slice(0, 8);
  if (!recent.length) {
    const empty = document.createElement('div');
    empty.className = 'notif-panel-empty';
    empty.textContent = 'No notifications yet.';
    list.appendChild(empty);
  } else {
    for (const n of recent) {
      const row = document.createElement('button');
      row.type = 'button';
      row.className = `notif-row${n.read ? '' : ' unread'}`;

      const dot = document.createElement('span');
      dot.className = `notif-dot level-${n.level}`;

      const rowBody = document.createElement('div');
      rowBody.className = 'notif-row-body';
      const title = document.createElement('div');
      title.className = 'notif-row-title';
      title.textContent = n.title;
      rowBody.appendChild(title);
      if (n.body) {
        const text = document.createElement('div');
        text.className = 'notif-row-text';
        text.textContent = n.body;
        rowBody.appendChild(text);
      }
      const time = document.createElement('div');
      time.className = 'notif-row-time';
      time.textContent = timeAgo(n.ts);
      rowBody.appendChild(time);

      row.append(dot, rowBody);
      row.addEventListener('click', () => {
        markReadLocal([n.id]);
        markReadRemote([n.id]);
        row.classList.remove('unread');
        // Navigating away doesn't itself count as an "outside click" of a
        // popover whose own content triggered it — close explicitly so it
        // doesn't stay floating over whatever screen navigate() lands on.
        if (n.action?.section) close();
        runAction(n);
      });
      list.appendChild(row);
    }
  }

  const footer = document.createElement('div');
  footer.className = 'notif-panel-footer';

  const markAll = document.createElement('button');
  markAll.type = 'button';
  markAll.textContent = 'Mark all read';
  markAll.addEventListener('click', async () => {
    const ids = notifications.filter((n) => !n.read).map((n) => n.id);
    if (!ids.length) return;
    markReadLocal(ids);
    try {
      await fetch('/api/notifications/read-all', { method: 'POST' });
    } catch {
      // local state already reflects it — see markReadRemote's comment
    }
    list.querySelectorAll('.notif-row.unread').forEach((el) => el.classList.remove('unread'));
  });

  const viewAll = document.createElement('button');
  viewAll.type = 'button';
  viewAll.textContent = 'View all →';
  viewAll.addEventListener('click', () => {
    close();
    navigate('notifications');
  });

  footer.append(markAll, viewAll);
  body.append(list, footer);
}

function openPanel(anchor) {
  popover({ anchor, build: buildPanel });
}

// ---------- ingest ----------

/** Adds a record (from the initial GET, or a live SSE push) into local state, de-duped by id. Does NOT toast — callers decide that (a fresh push toasts, seeding history on load doesn't). */
function ingestSilent(n) {
  if (seenIds.has(n.id)) return false;
  seenIds.add(n.id);
  notifications.unshift(n);
  notifications.sort((a, b) => new Date(b.ts) - new Date(a.ts));
  updateBadge();
  return true;
}

/** Called from app.js's SSE handler for a live 'notification' push — toasts it too, unlike the silent history seed. */
export function ingest(n) {
  if (ingestSilent(n)) showToast(n);
}

/**
 * Creates a notification from the browser itself (an error the server can't
 * originate, like "can't reach the server at all"). Falls back to a
 * local-only toast if the POST itself fails — losing the message would be
 * worse than losing persistence for it.
 */
export async function notify({ kind, level = 'info', title, body, action }) {
  try {
    const res = await fetch('/api/notifications', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ kind, level, title, body, action }),
    });
    const data = await res.json();
    if (data.ok && data.notification) {
      ingest(data.notification); // server also broadcasts this over SSE; ingestSilent's de-dupe by id makes a double-delivery harmless
      return;
    }
  } catch {
    // fall through to local-only toast below
  }
  showToast({ id: `local-${Date.now()}`, kind, level, title, body, action, ts: new Date().toISOString(), read: false });
}

export function getNotifications() {
  return notifications;
}

/**
 * Re-fetches the full list and REPLACES local state (unlike ingestSilent,
 * which only ever adds) — needed because a mutation can remove records
 * (delete, clear-all), not just add or flip `read`. Call after the SSE
 * 'notifications_changed' event (see app.js's connectEvents): a mutation
 * made through the full history screen (public/screens/notifications.js)
 * talks to the server routes directly, bypassing this module's own
 * markReadLocal/ingest entirely, so without this the header bell's badge
 * would silently drift out of sync with what the history screen just did.
 */
export async function resync() {
  try {
    const res = await fetch('/api/notifications');
    const data = await res.json();
    notifications = data.notifications || [];
    seenIds = new Set(notifications.map((n) => n.id));
    updateBadge();
  } catch {
    // Leave local state as-is — better a stale badge than a wiped one.
  }
}

/** Seeds history from the server and wires the bell/badge/panel. Call once at startup. */
export async function setupNotifications() {
  toastHost = document.getElementById('toast-host');
  badgeEl = document.getElementById('notifications-badge');
  const bell = document.getElementById('notifications-toggle');

  bell?.addEventListener('click', (e) => {
    e.stopPropagation();
    openPanel(bell);
  });

  await resync();
}

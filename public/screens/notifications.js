// Full notification history — the "View All" destination from the bell
// popover (public/notifications.js). Same server store, same records; this
// screen just shows more of them with a level/read filter instead of the
// popover's fixed 8-most-recent list. Clicking a row opens a detail view
// (_notification-detail.js) — same in-page-swap pattern app-control.js and
// skills.js already use for their own rows.

import { sectionCard, armedButton, postJson } from './_helpers.js';
import { segmented } from './_ui.js';
import { render as renderNotificationDetail } from './_notification-detail.js';

function timeAgo(iso) {
  const ms = Date.now() - new Date(iso).getTime();
  const min = Math.floor(ms / 60000);
  if (min < 1) return 'just now';
  if (min < 60) return `${min}m ago`;
  const hr = Math.floor(min / 60);
  if (hr < 24) return `${hr}h ago`;
  return `${Math.floor(hr / 24)}d ago`;
}

function buildRow(n, { onOpen, onChange }) {
  const row = document.createElement('div');
  row.className = 'list-row list-row-clickable';
  row.tabIndex = 0;
  row.setAttribute('role', 'button');

  const dot = document.createElement('span');
  dot.className = `notif-dot level-${n.level}`;
  dot.style.marginTop = '7px';

  const main = document.createElement('div');
  main.className = 'list-row-main';
  main.appendChild(Object.assign(document.createElement('span'), { className: 'list-row-title', textContent: n.title }));
  if (n.body) {
    main.appendChild(Object.assign(document.createElement('span'), { className: 'list-row-sub', textContent: n.body }));
  }
  main.appendChild(
    Object.assign(document.createElement('span'), {
      className: 'list-row-sub',
      textContent: `${timeAgo(n.ts)}${n.read ? '' : ' · unread'}`,
    })
  );

  const actions = document.createElement('div');
  actions.className = 'list-row-actions';
  const removeBtn = armedButton('Delete', 'Really delete?', async () => {
    await fetch(`/api/notifications/${encodeURIComponent(n.id)}`, { method: 'DELETE' });
    await onChange();
  });
  actions.appendChild(removeBtn);

  row.append(dot, main, actions);
  row.style.opacity = n.read ? '0.7' : '1';

  // Was previously guarded on `if (n.read) return` — an already-read row
  // did nothing at all when clicked, which was the literal bug behind
  // "notifications are static." Every row now opens the detail view
  // regardless of read state; onOpen() is responsible for marking it read
  // first if it wasn't already.
  row.addEventListener('click', () => onOpen(n));
  row.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      onOpen(n);
    }
  });

  return row;
}

export async function render(container) {
  container.innerHTML = '';

  const res = await fetch('/api/notifications');
  const { notifications } = await res.json();
  const onChange = () => render(container);

  let filter = 'all'; // 'all' | 'unread'

  const toolbar = document.createElement('div');
  toolbar.className = 'button-row';

  const filterTabs = segmented(
    [
      ['all', 'All'],
      ['unread', 'Unread'],
    ],
    { value: filter, onChange: (v) => { filter = v; renderList(); } }
  );
  toolbar.appendChild(filterTabs.wrapper);

  const markAllBtn = document.createElement('button');
  markAllBtn.type = 'button';
  markAllBtn.className = 'btn';
  markAllBtn.textContent = 'Mark all read';
  markAllBtn.addEventListener('click', async () => {
    await fetch('/api/notifications/read-all', { method: 'POST' });
    await onChange();
  });
  toolbar.appendChild(markAllBtn);

  const clearBtn = armedButton('Clear all', 'Really clear all?', async () => {
    await fetch('/api/notifications', { method: 'DELETE' });
    await onChange();
  });
  toolbar.appendChild(clearBtn);

  container.appendChild(toolbar);

  const card = sectionCard('Notifications');
  container.appendChild(card);

  async function onOpen(n) {
    if (!n.read) {
      n.read = true;
      await postJson('/api/notifications/read', { ids: [n.id] });
    }
    container.innerHTML = '';
    await renderNotificationDetail(container, { notification: n, onBack: onChange });
  }

  function renderList() {
    card.querySelectorAll('.list-row, .hint').forEach((el) => el.remove());
    const visible = filter === 'unread' ? notifications.filter((n) => !n.read) : notifications;
    if (!visible.length) {
      card.appendChild(
        Object.assign(document.createElement('p'), {
          className: 'hint',
          textContent: filter === 'unread' ? 'Nothing unread.' : 'No notifications yet.',
        })
      );
      return;
    }
    for (const n of visible) card.appendChild(buildRow(n, { onOpen, onChange }));
  }

  renderList();
}

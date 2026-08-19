// Chat History screen — browse, search, rename, pin, archive, and delete
// past conversations (server/chat-store.js). Opening a conversation makes it
// active on the server (POST /api/conversations/:id/activate) and fires a
// window event app.js listens for to rehydrate the transcript — the same
// loosely-coupled pattern the Skills screen's "Try in chat" already uses
// (public/screens/*.js are dynamically imported BY router.js, so a static
// import back into app.js risks a module-graph cycle).

import { sectionCard, fieldInput, armedButton } from './_helpers.js';
import { toggleSwitch } from './_ui.js';
import { openModal } from './_modal.js';

function formatDate(iso) {
  return new Date(iso).toLocaleString('en-US', { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
}

// app.js listens for this to rehydrate the transcript and switch back to
// the assistant screen — it owns the fetch of the full message list (this
// screen only needs to trigger the switch, not render the result).
async function openConversation(id) {
  const res = await fetch(`/api/conversations/${encodeURIComponent(id)}/activate`, { method: 'POST' });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.error || 'Could not open that conversation.');
  }
  window.dispatchEvent(new CustomEvent('jarvis:conversation-activated', { detail: { id } }));
}

async function renameFlow(conv, onChange) {
  let titleField;
  const result = await openModal({
    title: 'Rename conversation',
    submitLabel: 'Save',
    build(body, api) {
      titleField = fieldInput('Title', 'text');
      titleField.input.value = conv.title;
      body.appendChild(titleField.wrapper);
      titleField.input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') {
          e.preventDefault();
          api.close(titleField.input.value.trim());
        }
      });
    },
    async onSubmit(api) {
      const title = titleField.input.value.trim();
      if (!title) {
        api.setError('A conversation needs a title.');
        return false;
      }
      return title;
    },
  });
  if (!result) return;
  await fetch(`/api/conversations/${encodeURIComponent(conv.id)}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ title: result }),
  });
  await onChange();
}

function buildRow(conv, onChange) {
  const row = document.createElement('div');
  row.className = 'list-row list-row-clickable';
  row.tabIndex = 0;
  row.setAttribute('role', 'button');

  const open = async () => {
    try {
      await openConversation(conv.id);
    } catch (err) {
      // A best-effort toast would need notifications.js — a plain inline
      // error is enough here since this is a rare failure (the conversation
      // was deleted from another tab/session between list and click).
      alert(err.message || 'Could not open that conversation.');
      await onChange();
    }
  };
  row.addEventListener('click', open);
  row.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      open();
    }
  });

  const main = document.createElement('div');
  main.className = 'list-row-main';
  const titleLine = document.createElement('span');
  titleLine.className = 'list-row-title';
  titleLine.textContent = (conv.pinned ? '📌 ' : '') + conv.title;
  main.appendChild(titleLine);
  const sub = document.createElement('span');
  sub.className = 'list-row-sub';
  const count = conv.messageCount ?? 0;
  sub.textContent = `${formatDate(conv.updatedAt)} · ${count} message${count === 1 ? '' : 's'}${conv.archived ? ' · Archived' : ''}`;
  main.appendChild(sub);

  const actions = document.createElement('div');
  actions.className = 'list-row-actions';

  const pinBtn = document.createElement('button');
  pinBtn.type = 'button';
  pinBtn.className = 'btn';
  pinBtn.textContent = conv.pinned ? 'Unpin' : 'Pin';
  pinBtn.addEventListener('click', async (e) => {
    e.stopPropagation();
    await fetch(`/api/conversations/${encodeURIComponent(conv.id)}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ pinned: !conv.pinned }),
    });
    await onChange();
  });

  const renameBtn = document.createElement('button');
  renameBtn.type = 'button';
  renameBtn.className = 'btn';
  renameBtn.textContent = 'Rename';
  renameBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    renameFlow(conv, onChange);
  });

  const archiveBtn = document.createElement('button');
  archiveBtn.type = 'button';
  archiveBtn.className = 'btn';
  archiveBtn.textContent = conv.archived ? 'Unarchive' : 'Archive';
  archiveBtn.addEventListener('click', async (e) => {
    e.stopPropagation();
    await fetch(`/api/conversations/${encodeURIComponent(conv.id)}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ archived: !conv.archived }),
    });
    await onChange();
  });

  const deleteBtn = armedButton('Delete', 'Really delete?', async () => {
    await fetch(`/api/conversations/${encodeURIComponent(conv.id)}`, { method: 'DELETE' });
    await onChange();
  });

  actions.append(pinBtn, renameBtn, archiveBtn, deleteBtn);
  row.append(main, actions);
  return row;
}

export async function render(container) {
  container.innerHTML = '';

  let search = '';
  let showArchived = false;

  const toolbar = document.createElement('div');
  toolbar.className = 'button-row';
  const searchField = fieldInput('', 'search', 'Search conversations…');
  searchField.wrapper.style.flex = '1';
  toolbar.appendChild(searchField.wrapper);

  const archivedToggleWrap = document.createElement('label');
  archivedToggleWrap.className = 'settings-row';
  archivedToggleWrap.appendChild(Object.assign(document.createElement('span'), { textContent: 'Show archived' }));
  const archivedToggle = toggleSwitch({
    value: false,
    onChange: (v) => {
      showArchived = v;
      load();
    },
  });
  archivedToggleWrap.appendChild(archivedToggle.wrapper);

  container.append(toolbar, archivedToggleWrap);

  const listCard = sectionCard('Conversations');
  container.appendChild(listCard);

  let debounceTimer = null;
  searchField.input.addEventListener('input', () => {
    search = searchField.input.value;
    clearTimeout(debounceTimer);
    debounceTimer = setTimeout(load, 150);
  });

  async function load() {
    const params = new URLSearchParams();
    if (search.trim()) params.set('q', search.trim());
    if (showArchived) params.set('archived', '1');
    const res = await fetch(`/api/conversations?${params.toString()}`);
    const data = await res.json();

    listCard.querySelectorAll('.list-row').forEach((el) => el.remove());
    listCard.querySelectorAll('.hint').forEach((el) => el.remove());

    const conversations = data.conversations || [];
    if (!conversations.length) {
      listCard.appendChild(
        Object.assign(document.createElement('p'), {
          className: 'hint',
          textContent: search.trim() ? 'No conversations match that search.' : 'No conversations yet — just start talking to Jarvis.',
        })
      );
      return;
    }
    for (const conv of conversations) listCard.appendChild(buildRow(conv, load));
  }

  await load();
}

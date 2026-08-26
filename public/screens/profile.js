// Profile & Goals screen — the short notes Jarvis has been told about the
// user (via the remember_about_me skill, or added directly here). Feeds the
// morning briefing's personalization and proactive suggestions.
//
// Entries here are memory-store.js rows in the 'About You' category (see
// server/profile.js's header comment) — editing and version history already
// existed at the storage layer via updateMemory()/getVersionHistory(), just
// never exposed. PATCH /api/profile/:id and GET /api/profile/:id/versions
// are the thin routes that connect this screen to that existing capability.

import { sectionCard, fieldTextarea, armedButton, postJson } from './_helpers.js';
import { openModal } from './_modal.js';

function formatDate(iso) {
  return new Date(iso).toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
}

async function editFlow(entry, onChange) {
  const versionsRes = await fetch(`/api/profile/${encodeURIComponent(entry.id)}/versions`).catch(() => null);
  const versionsData = versionsRes ? await versionsRes.json().catch(() => ({ versions: [] })) : { versions: [] };
  const versions = versionsData.versions || [];

  let textField;
  const result = await openModal({
    title: 'Edit note',
    submitLabel: 'Save',
    build(body) {
      textField = fieldTextarea('What Jarvis remembers', '');
      textField.textarea.value = entry.text;
      body.appendChild(textField.wrapper);

      body.appendChild(
        Object.assign(document.createElement('p'), { className: 'hint', textContent: `Added ${formatDate(entry.addedAt)}` })
      );

      if (versions.length) {
        const historyCard = sectionCard('Earlier versions');
        for (const v of versions) {
          const row = document.createElement('div');
          row.className = 'list-row';
          const main = document.createElement('div');
          main.className = 'list-row-main';
          main.appendChild(Object.assign(document.createElement('span'), { className: 'list-row-title', textContent: v.text }));
          main.appendChild(
            Object.assign(document.createElement('span'), { className: 'list-row-sub', textContent: formatDate(v.changedAt) })
          );
          row.appendChild(main);
          historyCard.appendChild(row);
        }
        body.appendChild(historyCard);
      }
    },
    async onSubmit(api) {
      const text = textField.textarea.value.trim();
      if (!text) {
        api.setError('A note needs some text.');
        return null;
      }
      const data = await postJson(`/api/profile/${encodeURIComponent(entry.id)}`, { text }, 'PATCH');
      if (!data.ok) {
        api.setError(data.error || 'Could not save that note.');
        return null;
      }
      return data;
    },
  });
  if (result) await onChange();
}

function buildEntryRow(entry, onChange) {
  const row = document.createElement('div');
  row.className = 'list-row list-row-clickable';
  row.tabIndex = 0;
  row.setAttribute('role', 'button');

  const main = document.createElement('div');
  main.className = 'list-row-main';
  main.appendChild(Object.assign(document.createElement('span'), { className: 'list-row-title', textContent: entry.text }));
  const sub = document.createElement('span');
  sub.className = 'list-row-sub';
  sub.textContent = formatDate(entry.addedAt);
  main.appendChild(sub);

  const actions = document.createElement('div');
  actions.className = 'list-row-actions';
  const removeBtn = armedButton('Delete', 'Really delete?', async () => {
    await fetch(`/api/profile/${encodeURIComponent(entry.id)}`, { method: 'DELETE' });
    await onChange();
  });
  actions.appendChild(removeBtn);

  row.append(main, actions);
  const open = () => editFlow(entry, onChange);
  row.addEventListener('click', open);
  row.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      open();
    }
  });
  return row;
}

function buildListCard(entries, onChange) {
  const card = sectionCard('What Jarvis knows about you');
  if (!entries.length) {
    card.appendChild(
      Object.assign(document.createElement('p'), {
        className: 'hint',
        textContent: 'Nothing yet — add a note below, or just tell Jarvis "remember that..." in conversation.',
      })
    );
    return card;
  }
  for (const entry of [...entries].reverse()) card.appendChild(buildEntryRow(entry, onChange));
  return card;
}

function buildAddCard(onChange) {
  const card = sectionCard('Add a note');
  const textField = fieldTextarea(
    'What should Jarvis remember?',
    'e.g. "I\'m working on a photography portfolio site" or "I prefer short, direct answers."'
  );
  card.appendChild(textField.wrapper);

  const errorEl = document.createElement('p');
  errorEl.className = 'error hidden';
  card.appendChild(errorEl);

  const addBtn = document.createElement('button');
  addBtn.type = 'button';
  addBtn.className = 'btn btn-primary';
  addBtn.textContent = 'Save note';
  addBtn.addEventListener('click', async () => {
    errorEl.classList.add('hidden');
    const text = textField.textarea.value.trim();
    if (!text) return;
    addBtn.disabled = true;
    try {
      const res = await fetch('/api/profile', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text }),
      });
      const data = await res.json();
      if (!data.ok) {
        errorEl.textContent = data.error || 'Could not save that note.';
        errorEl.classList.remove('hidden');
        return;
      }
      textField.textarea.value = '';
      await onChange();
    } catch {
      errorEl.textContent = 'Could not reach the Jarvis server.';
      errorEl.classList.remove('hidden');
    } finally {
      addBtn.disabled = false;
    }
  });
  card.appendChild(addBtn);

  card.appendChild(
    Object.assign(document.createElement('p'), {
      className: 'hint',
      textContent: 'These notes personalize the morning briefing and its suggestions — Jarvis never sends them anywhere else.',
    })
  );

  return card;
}

export async function render(container) {
  container.innerHTML = '';
  const res = await fetch('/api/profile');
  const { entries } = await res.json();
  const onChange = () => render(container);

  container.appendChild(buildListCard(entries, onChange));
  container.appendChild(buildAddCard(onChange));
}

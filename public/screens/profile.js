// Profile & Goals screen — the short notes Jarvis has been told about the
// user (via the remember_about_me skill, or added directly here). Feeds the
// morning briefing's personalization and proactive suggestions.

import { sectionCard, fieldTextarea, armedButton } from './_helpers.js';

function buildEntryRow(entry, onChange) {
  const row = document.createElement('div');
  row.className = 'list-row';

  const main = document.createElement('div');
  main.className = 'list-row-main';
  main.appendChild(Object.assign(document.createElement('span'), { className: 'list-row-title', textContent: entry.text }));
  const sub = document.createElement('span');
  sub.className = 'list-row-sub';
  sub.textContent = new Date(entry.addedAt).toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
  main.appendChild(sub);

  const actions = document.createElement('div');
  actions.className = 'list-row-actions';
  const removeBtn = armedButton('Delete', 'Really delete?', async () => {
    await fetch(`/api/profile/${encodeURIComponent(entry.id)}`, { method: 'DELETE' });
    await onChange();
  });
  actions.appendChild(removeBtn);

  row.append(main, actions);
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

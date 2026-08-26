// Memory screen — every memory Jarvis has saved, whether approved by hand,
// stated explicitly ("remember that..."), or saved automatically (see
// server/memory/memory-policy.js's trust dial). server/memory/memory-store.js
// has been a complete engine (browse, search, edit, merge, archive, version
// history) since Memory first shipped, with deliberately no screen — that
// was defensible when every memory was individually approved one at a time.
// Once something can be saved without being asked, "see and undo what
// happened" stops being optional; this is that screen.
//
// Filtering (search/category/origin) is entirely client-side over one fetch
// — the same pattern models.js already uses for its own filter toolbar
// (modelMatchesFilter()) — since Memory is a small, curated list by design,
// not something that needs server-side paging.

import { sectionCard, fieldInput, fieldTextarea, fieldSelect, armedButton, postJson } from './_helpers.js';
import { toggleSwitch } from './_ui.js';
import { openModal } from './_modal.js';

const TRUST_OPTIONS = [
  ['ask', 'Ask me about everything (recommended)'],
  ['balanced', "Auto-save what Jarvis is confident about"],
  ['auto', 'Auto-save everything'],
];

const ORIGIN_FILTERS = [
  ['', 'Everything'],
  ['auto', 'Saved automatically'],
  ['confirmed', 'You confirmed'],
];

function formatDate(iso) {
  return new Date(iso).toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
}

function originBadgeText(m) {
  switch (m.origin) {
    case 'auto':
      return Number.isFinite(m.confidence) ? `Saved automatically · ${Math.round(m.confidence * 100)}% confident` : 'Saved automatically';
    case 'explicit':
      return 'You said this';
    case 'legacy':
      return 'Migrated';
    default:
      return 'You approved this';
  }
}

/** True when a memory should show under the current search/category/origin filter. */
function matchesFilter(m, filterState) {
  const q = filterState.query.trim().toLowerCase();
  if (q && !`${m.text} ${m.category}`.toLowerCase().includes(q)) return false;
  if (filterState.category && m.category !== filterState.category) return false;
  if (filterState.origin === 'auto' && m.origin !== 'auto') return false;
  if (filterState.origin === 'confirmed' && m.origin === 'auto') return false;
  return true;
}

function buildTrustCard(prefs) {
  const card = sectionCard('How much should Jarvis save on its own?');

  const row = document.createElement('label');
  row.className = 'settings-row';
  row.appendChild(Object.assign(document.createElement('span'), { textContent: 'When Jarvis notices something worth remembering' }));
  const select = document.createElement('select');
  for (const [value, text] of TRUST_OPTIONS) {
    select.appendChild(Object.assign(document.createElement('option'), { value, textContent: text }));
  }
  select.value = prefs.memoryTrust;
  row.appendChild(select);
  card.appendChild(row);
  select.addEventListener('change', () => postJson('/api/prefs', { memoryTrust: select.value }));

  card.appendChild(
    Object.assign(document.createElement('p'), {
      className: 'hint',
      textContent:
        'Anything that conflicts with something already remembered always waits for you to decide, no matter this setting. ' +
        'Anything saved automatically is marked "Saved automatically" below — edit, undo, or delete it any time.',
    })
  );

  return card;
}

async function editFlow(memory, categories, onChange) {
  let textField;
  let categorySelect;
  const result = await openModal({
    title: 'Edit memory',
    submitLabel: 'Save',
    build(body) {
      textField = fieldTextarea('What Jarvis remembers', '');
      textField.textarea.value = memory.text;
      body.appendChild(textField.wrapper);

      const names = categories.map((c) => c.name);
      if (!names.includes(memory.category)) names.push(memory.category);
      categorySelect = fieldSelect(
        'Category',
        names.map((n) => [n, n])
      );
      categorySelect.select.value = memory.category;
      body.appendChild(categorySelect.wrapper);
    },
    async onSubmit(api) {
      const text = textField.textarea.value.trim();
      if (!text) {
        api.setError('A memory needs some text.');
        return false;
      }
      return { text, category: categorySelect.select.value };
    },
  });
  if (!result) return;
  await fetch(`/api/memories/${encodeURIComponent(memory.id)}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(result),
  });
  await onChange();
}

/**
 * Was a plain read-only list — you could see what a memory used to say, but
 * never bring it back, despite memory-store.js's updateMemory() being all
 * "restore" needs (it's just another edit, versioned like any other; the
 * pre-restore text becomes its own new version, so restoring is never
 * lossy). `restoringId` guards against a version row referencing a memory
 * that's since been archived/deleted out from under this modal — Restore
 * would fail loudly via the PATCH's own error handling either way, but
 * disabling the row's buttons while a restore is in flight avoids a
 * double-click firing two overlapping PATCHes.
 */
async function historyFlow(memory, onChange) {
  const res = await fetch(`/api/memories/${encodeURIComponent(memory.id)}/versions`);
  const data = await res.json().catch(() => ({ versions: [] }));
  let restored = false;
  await openModal({
    title: `History — ${memory.category}`,
    submitLabel: 'Close',
    build(body) {
      const versions = data.versions || [];
      if (!versions.length) {
        body.appendChild(
          Object.assign(document.createElement('p'), { className: 'hint', textContent: 'No earlier versions — this memory has never been edited.' })
        );
        return;
      }
      for (const v of versions) {
        const row = document.createElement('div');
        row.className = 'list-row';
        const main = document.createElement('div');
        main.className = 'list-row-main';
        main.appendChild(Object.assign(document.createElement('span'), { className: 'list-row-title', textContent: v.text }));
        main.appendChild(
          Object.assign(document.createElement('span'), {
            className: 'list-row-sub',
            textContent: `${formatDate(v.changedAt)} · ${v.category}${v.reason ? ' · ' + v.reason : ''}`,
          })
        );
        row.appendChild(main);

        const actions = document.createElement('div');
        actions.className = 'list-row-actions';
        const restoreBtn = document.createElement('button');
        restoreBtn.type = 'button';
        restoreBtn.className = 'btn';
        restoreBtn.textContent = 'Restore this version';
        restoreBtn.addEventListener('click', async () => {
          restoreBtn.disabled = true;
          restoreBtn.textContent = 'Restoring…';
          try {
            const result = await postJson(
              `/api/memories/${encodeURIComponent(memory.id)}`,
              { text: v.text, category: v.category, reason: 'restore' },
              'PATCH'
            );
            if (!result.ok && result.error) throw new Error(result.error);
            restored = true;
          } catch {
            restoreBtn.disabled = false;
            restoreBtn.textContent = 'Restore this version';
            return;
          }
          restoreBtn.textContent = 'Restored';
        });
        actions.appendChild(restoreBtn);
        row.appendChild(actions);

        body.appendChild(row);
      }
    },
    async onSubmit() {
      return true; // read-only aside from Restore above — either button just closes it
    },
  });
  if (restored) await onChange();
}

/**
 * A pending-suggestion row — Approve/Edit/Reject, or Update/Keep both/
 * Discard for a conflict — hitting the same REST endpoints the in-chat
 * review card (public/app.js's buildCandidateRow()) already uses. This
 * screen is the DURABLE way to reach a pending suggestion: the in-chat
 * card only ever appears live (an SSE push, or asking "what's pending?")
 * and disappears the moment the transcript is cleared (a new chat, a
 * reload) — confirmed live, this is exactly what stranded six real pending
 * suggestions with no way back to them except asking out loud. This screen
 * has none of that dependency; it just fetches what's pending.
 *
 * list-row-stacked (style.css) applied here too, defensively — this screen
 * is wider than the docked conversation rail so the squeeze that broke the
 * in-chat conflict card is unlikely here, but a pending row carries the
 * exact same shape (a full sentence plus up to three wide buttons) and
 * there's no reason to leave it exposed to the same class of bug later.
 */
function buildPendingRow(candidate, onChange) {
  const row = document.createElement('div');
  row.className = 'list-row list-row-stacked';

  const main = document.createElement('div');
  main.className = 'list-row-main';
  main.appendChild(Object.assign(document.createElement('span'), { className: 'list-row-title', textContent: candidate.text }));
  main.appendChild(Object.assign(document.createElement('span'), { className: 'list-row-sub', textContent: candidate.category }));
  row.appendChild(main);

  const actions = document.createElement('div');
  actions.className = 'list-row-actions';

  if (candidate.conflictWith) {
    main.appendChild(
      Object.assign(document.createElement('p'), {
        className: 'hint',
        textContent: candidate.conflictText
          ? `This conflicts with what's already remembered: "${candidate.conflictText}"`
          : 'This may conflict with something already remembered.',
      })
    );
    const resolve = async (choice) => {
      row.querySelectorAll('button').forEach((b) => (b.disabled = true));
      await postJson(`/api/memories/candidates/${encodeURIComponent(candidate.id)}/resolve-conflict`, { choice });
      await onChange();
    };
    const updateBtn = document.createElement('button');
    updateBtn.type = 'button';
    updateBtn.className = 'btn btn-primary';
    updateBtn.textContent = 'Update the old one';
    updateBtn.addEventListener('click', () => resolve('update'));
    const bothBtn = document.createElement('button');
    bothBtn.type = 'button';
    bothBtn.className = 'btn';
    bothBtn.textContent = 'Keep both';
    bothBtn.addEventListener('click', () => resolve('keep_both'));
    const discardBtn = document.createElement('button');
    discardBtn.type = 'button';
    discardBtn.className = 'btn';
    discardBtn.textContent = 'Discard new';
    discardBtn.addEventListener('click', () => resolve('discard'));
    actions.append(updateBtn, bothBtn, discardBtn);
    row.appendChild(actions);
    return row;
  }

  const approveBtn = document.createElement('button');
  approveBtn.type = 'button';
  approveBtn.className = 'btn btn-primary';
  approveBtn.textContent = 'Approve';
  approveBtn.addEventListener('click', async () => {
    row.querySelectorAll('button').forEach((b) => (b.disabled = true));
    await postJson(`/api/memories/candidates/${encodeURIComponent(candidate.id)}/approve`, { edits: {} });
    await onChange();
  });
  actions.appendChild(approveBtn);

  const rejectBtn = armedButton('Reject', 'Really reject?', async () => {
    row.querySelectorAll('button').forEach((b) => (b.disabled = true));
    await fetch(`/api/memories/candidates/${encodeURIComponent(candidate.id)}/reject`, { method: 'POST' });
    await onChange();
  });
  actions.appendChild(rejectBtn);

  row.appendChild(actions);
  return row;
}

function buildMemoryRow(m, categories, onChange) {
  const row = document.createElement('div');
  row.className = 'list-row';

  const main = document.createElement('div');
  main.className = 'list-row-main';
  main.appendChild(Object.assign(document.createElement('span'), { className: 'list-row-title', textContent: m.text }));
  main.appendChild(
    Object.assign(document.createElement('span'), {
      className: 'list-row-sub',
      textContent: `${m.category} · ${formatDate(m.updatedAt)}${m.archived ? ' · Archived' : ''}`,
    })
  );
  const badge = document.createElement('span');
  badge.className = `badge ${m.origin === 'auto' ? 'warn' : 'good'}`;
  badge.textContent = originBadgeText(m);
  main.appendChild(badge);
  row.appendChild(main);

  const actions = document.createElement('div');
  actions.className = 'list-row-actions';

  const editBtn = document.createElement('button');
  editBtn.type = 'button';
  editBtn.className = 'btn';
  editBtn.textContent = 'Edit';
  editBtn.addEventListener('click', () => editFlow(m, categories, onChange));
  actions.appendChild(editBtn);

  const historyBtn = document.createElement('button');
  historyBtn.type = 'button';
  historyBtn.className = 'btn';
  historyBtn.textContent = 'History';
  historyBtn.addEventListener('click', () => historyFlow(m, onChange));
  actions.appendChild(historyBtn);

  if (m.archived) {
    const restoreBtn = document.createElement('button');
    restoreBtn.type = 'button';
    restoreBtn.className = 'btn';
    restoreBtn.textContent = 'Restore';
    restoreBtn.addEventListener('click', async () => {
      await fetch(`/api/memories/${encodeURIComponent(m.id)}/restore`, { method: 'POST' });
      await onChange();
    });
    actions.appendChild(restoreBtn);

    // Hard delete only reachable once a memory is already archived — the
    // soft, reversible action is the default everywhere else in this app
    // (see memory-store.js's own archiveMemory()/deleteMemory() comments).
    const deleteBtn = armedButton('Delete permanently', 'Really delete?', async () => {
      await fetch(`/api/memories/${encodeURIComponent(m.id)}`, { method: 'DELETE' });
      await onChange();
    });
    actions.appendChild(deleteBtn);
  } else {
    const archiveBtn = document.createElement('button');
    archiveBtn.type = 'button';
    archiveBtn.className = 'btn';
    archiveBtn.textContent = 'Archive';
    archiveBtn.addEventListener('click', async () => {
      await fetch(`/api/memories/${encodeURIComponent(m.id)}/archive`, { method: 'POST' });
      await onChange();
    });
    actions.appendChild(archiveBtn);
  }

  row.appendChild(actions);
  return row;
}

export async function render(container) {
  container.innerHTML = '';

  const [prefsRes, categoriesRes, candidatesRes] = await Promise.all([
    fetch('/api/prefs'),
    fetch('/api/memories/categories'),
    fetch('/api/memories/candidates'),
  ]);
  const prefs = await prefsRes.json();
  const categoriesData = await categoriesRes.json();
  const categories = (categoriesData.categories || []).filter((c) => c.status === 'approved');
  const candidatesData = await candidatesRes.json().catch(() => ({ candidates: [] }));
  const pending = candidatesData.candidates || [];

  // Only shown when there's actually something waiting — quiet when empty,
  // same as review_memories' own "nothing pending" behavior in chat.
  if (pending.length) {
    const pendingCard = sectionCard(pending.length === 1 ? 'A memory suggestion' : `${pending.length} memory suggestions`);
    pendingCard.appendChild(
      Object.assign(document.createElement('p'), {
        className: 'hint',
        textContent: 'Noticed quietly in the background — nothing here is remembered until you decide.',
      })
    );
    for (const candidate of pending) pendingCard.appendChild(buildPendingRow(candidate, () => render(container)));
    container.appendChild(pendingCard);
  }

  container.appendChild(buildTrustCard(prefs));

  const filterState = { query: '', category: '', origin: '' };

  const toolbar = document.createElement('div');
  toolbar.className = 'button-row';
  const searchField = fieldInput('', 'search', 'Search what Jarvis remembers…');
  searchField.wrapper.style.flex = '1';
  toolbar.appendChild(searchField.wrapper);

  const categoryFilter = fieldSelect('', [['', 'All categories'], ...categories.map((c) => [c.name, c.name])]);
  toolbar.appendChild(categoryFilter.wrapper);

  const originFilter = fieldSelect('', ORIGIN_FILTERS);
  toolbar.appendChild(originFilter.wrapper);

  container.appendChild(toolbar);

  const archivedRow = document.createElement('label');
  archivedRow.className = 'settings-row';
  archivedRow.appendChild(Object.assign(document.createElement('span'), { textContent: 'Show archived' }));
  let showArchived = false;
  const archivedToggle = toggleSwitch({
    value: false,
    onChange: (v) => {
      showArchived = v;
      load();
    },
  });
  archivedRow.appendChild(archivedToggle.wrapper);
  container.appendChild(archivedRow);

  const listWrap = document.createElement('div');
  container.appendChild(listWrap);

  const onChange = () => load();

  let debounceTimer = null;
  searchField.input.addEventListener('input', () => {
    filterState.query = searchField.input.value;
    clearTimeout(debounceTimer);
    debounceTimer = setTimeout(renderList, 150);
  });
  categoryFilter.select.addEventListener('change', () => {
    filterState.category = categoryFilter.select.value;
    renderList();
  });
  originFilter.select.addEventListener('change', () => {
    filterState.origin = originFilter.select.value;
    renderList();
  });

  let allMemories = [];

  function renderList() {
    listWrap.innerHTML = '';
    const matched = allMemories.filter((m) => matchesFilter(m, filterState));
    if (!matched.length) {
      listWrap.appendChild(
        Object.assign(document.createElement('p'), {
          className: 'hint',
          textContent: allMemories.length ? 'Nothing matches that filter.' : "Nothing yet — Jarvis will save things here as you talk, once you've approved them (or sooner, per the setting above).",
        })
      );
      return;
    }

    const byCategory = new Map();
    for (const m of matched) {
      if (!byCategory.has(m.category)) byCategory.set(m.category, []);
      byCategory.get(m.category).push(m);
    }
    for (const [category, memories] of byCategory) {
      const card = sectionCard(`${category} (${memories.length})`);
      for (const m of memories) card.appendChild(buildMemoryRow(m, categories, onChange));
      listWrap.appendChild(card);
    }
  }

  async function load() {
    const params = new URLSearchParams();
    if (showArchived) params.set('includeArchived', 'true');
    const res = await fetch(`/api/memories?${params.toString()}`);
    const data = await res.json();
    allMemories = data.memories || [];
    renderList();
  }

  await load();
}

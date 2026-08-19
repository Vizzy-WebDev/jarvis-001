// Model Settings screen — the model registry's full CRUD, plus the
// auto-select preferences (balance dial, confirm sensitivity). Mounted by
// public/router.js. Built with createElement/textContent throughout (no
// innerHTML on anything containing server data) since model labels, error
// text, etc. are user-supplied.
//
// Models are grouped by the connection (saved address+key) they came from
// — several models discovered together share one connection instead of
// each duplicating the same saved key. "Add a model" is a popup (see
// _modal.js) rather than a permanently-inline form.

import { fieldInput, fieldSelect, sectionCard, armedButton, postJson } from './_helpers.js';
import { openModal } from './_modal.js';
import { dropdownControl, toggleSwitch } from './_ui.js';

// Local servers rarely publish more than a handful of models, but a host
// like OpenRouter's /v1/models can return hundreds — defaulting those all
// checked would mean one click adding hundreds of registry entries.
const AUTO_CHECK_LIMIT = 20;

const BILLING_VALUES = ['free', 'paid', 'local', 'unknown'];

const AVAILABILITY_LABELS = {
  working: 'ready',
  quota: 'quota used up',
  no_access: 'no access',
  auth: 'key rejected',
  unreachable: 'unreachable',
};

function buildPrefsCard(prefs) {
  const card = sectionCard('How Jarvis picks a model');

  const autoRow = document.createElement('label');
  autoRow.className = 'settings-row';
  const autoSpan = document.createElement('span');
  autoSpan.textContent = 'Pick automatically';
  const autoCheck = document.createElement('input');
  autoCheck.type = 'checkbox';
  autoCheck.checked = prefs.autoSelect;
  autoRow.append(autoSpan, autoCheck);
  card.appendChild(autoRow);

  const balanceRow = document.createElement('label');
  balanceRow.className = 'settings-row';
  balanceRow.appendChild(Object.assign(document.createElement('span'), { textContent: 'Balance' }));
  const balanceSelect = document.createElement('select');
  for (const [value, text] of [
    ['fast', 'Prefer fastest'],
    ['balanced', 'Balanced (recommended)'],
    ['quality', 'Prefer best quality'],
  ]) {
    balanceSelect.appendChild(Object.assign(document.createElement('option'), { value, textContent: text }));
  }
  balanceSelect.value = prefs.balance;
  balanceRow.appendChild(balanceSelect);
  card.appendChild(balanceRow);

  const clarityRow = document.createElement('label');
  clarityRow.className = 'settings-row';
  const claritySpan = document.createElement('span');
  claritySpan.textContent = 'Confirm unclear voice actions';
  claritySpan.title = 'How often Jarvis double-checks quick actions (like opening an app) when your voice was hard to make out.';
  clarityRow.appendChild(claritySpan);
  const claritySelect = document.createElement('select');
  for (const [value, text] of [
    ['more', 'Check more'],
    ['balanced', 'Balanced (recommended)'],
    ['less', 'Check less'],
  ]) {
    claritySelect.appendChild(Object.assign(document.createElement('option'), { value, textContent: text }));
  }
  claritySelect.value = prefs.clarifySensitivity;
  clarityRow.appendChild(claritySelect);
  card.appendChild(clarityRow);

  const hint = document.createElement('p');
  hint.className = 'hint';
  hint.textContent =
    "Auto-select weighs each model's speed, quality, and cost for the task at hand, and switches " +
    'models automatically if one breaks — Jarvis will tell you when that happens. Turn it off to pin ' +
    "a specific model instead, from the main screen's settings panel.";
  card.appendChild(hint);

  const save = (patch) => postJson('/api/prefs', patch);
  autoCheck.addEventListener('change', () => save({ autoSelect: autoCheck.checked }));
  balanceSelect.addEventListener('change', () => save({ balance: balanceSelect.value }));
  claritySelect.addEventListener('change', () => save({ clarifySensitivity: claritySelect.value }));

  return card;
}

/** True when a model's billing/availability should count as matching a given status filter. */
function modelMatchesFilter(m, filterState) {
  const q = filterState.query.trim().toLowerCase();
  if (q && !`${m.label} ${m.model}`.toLowerCase().includes(q)) return false;

  const status = filterState.status;
  if (status === 'all') return true;
  if (status === 'free') return m.billing === 'free';
  if (status === 'paid') return m.billing === 'paid';
  const state = m.availability?.state;
  if (status === 'working') return state === 'working';
  if (status === 'not_working') return Boolean(state) && state !== 'working' && state !== 'unknown';
  return true;
}

/** The small always-visible billing select, styled to read as a badge — clicking it lets the user override the best-effort billing guess. */
function buildBillingBadge(m, onChange) {
  const select = document.createElement('select');
  const billing = m.billing || 'unknown';
  select.className = `badge-select badge-${billing}`;
  for (const value of BILLING_VALUES) {
    select.appendChild(Object.assign(document.createElement('option'), { value, textContent: value }));
  }
  select.value = billing;
  select.addEventListener('click', (e) => e.stopPropagation());
  select.addEventListener('change', async () => {
    await postJson(`/api/models/${encodeURIComponent(m.id)}`, { billing: select.value }, 'PATCH');
    await onChange();
  });
  return select;
}

function buildModelRow(m, healthInfo, onChange) {
  const row = document.createElement('div');
  row.className = 'list-row';

  const main = document.createElement('div');
  main.className = 'list-row-main';
  main.appendChild(Object.assign(document.createElement('span'), { className: 'list-row-title', textContent: m.label }));
  const sub = document.createElement('span');
  sub.className = 'list-row-sub';
  sub.textContent = m.model;
  main.appendChild(sub);

  const badge = document.createElement('span');
  const state = m.availability?.state;
  const specificLabel = state && state !== 'unknown' ? AVAILABILITY_LABELS[state] : null;
  if (specificLabel) {
    badge.className = `badge ${state === 'working' ? 'good' : 'bad'}`;
    badge.textContent = specificLabel;
  } else {
    // No specific availability signal yet — fall back to the original health/ready logic.
    badge.className = `badge ${healthInfo ? 'bad' : m.ready ? 'good' : 'bad'}`;
    badge.textContent = healthInfo ? 'temporarily unavailable' : m.ready ? 'ready' : 'needs a key';
  }
  main.appendChild(badge);
  main.appendChild(buildBillingBadge(m, onChange));

  const actions = document.createElement('div');
  actions.className = 'list-row-actions';

  const toggle = toggleSwitch({
    value: m.enabled,
    onChange: async (next) => {
      await postJson(`/api/models/${encodeURIComponent(m.id)}`, { enabled: next }, 'PATCH');
      await onChange();
    },
  });

  const testBtn = document.createElement('button');
  testBtn.type = 'button';
  testBtn.className = 'btn';
  testBtn.textContent = 'Test';
  testBtn.addEventListener('click', async () => {
    testBtn.disabled = true;
    testBtn.textContent = 'Testing…';
    try {
      const res = await fetch(`/api/models/${encodeURIComponent(m.id)}/test`, { method: 'POST' });
      const result = await res.json();
      sub.textContent = result.ok ? 'Connection works.' : result.error || 'Connection failed.';
    } catch {
      sub.textContent = 'Could not reach the Jarvis server.';
    } finally {
      testBtn.disabled = false;
      testBtn.textContent = 'Test';
    }
  });

  const removeBtn = armedButton('Remove', 'Really remove?', async () => {
    await fetch(`/api/models/${encodeURIComponent(m.id)}`, { method: 'DELETE' });
    await onChange();
  });

  actions.append(toggle.wrapper, testBtn, removeBtn);
  row.append(main, actions);
  return row;
}

// ---------- shared model-picker body (discovery + checklist / direct name) ----------

/**
 * Builds the "which model(s)" part of a form: a model-name field with
 * suggestions, plus a "Find models" button that reveals a checklist in its
 * place. Shared by both the new-connection modal and the add-models-to-an-
 * existing-connection modal.
 *
 * `discover()` must resolve to `{models, error}` (see registry.js's
 * discoverModels) where each model is `{model, label, contextTokens,
 * billing}`. `canDiscover` gates whether the button/checklist path is
 * offered at all — always true today (every adapter can discover), kept as
 * a parameter in case a future adapter can't.
 */
function buildModelPicker({ suggestions = [], canDiscover, discover }) {
  const wrapper = document.createElement('div');

  const datalist = document.createElement('datalist');
  datalist.id = `model-suggestions-${Math.random().toString(36).slice(2)}`;
  wrapper.appendChild(datalist);
  const fillSuggestions = (names) => {
    datalist.innerHTML = '';
    for (const name of names) datalist.appendChild(Object.assign(document.createElement('option'), { value: name }));
  };
  fillSuggestions(suggestions);

  const modelField = fieldInput('Model', 'text', canDiscover ? 'Leave blank to discover models, or type one' : 'e.g. gemini-3.5-flash');
  modelField.input.setAttribute('list', datalist.id);
  wrapper.appendChild(modelField.wrapper);

  const checklistWrap = document.createElement('div');
  checklistWrap.className = 'hidden';
  const actionsRow = document.createElement('div');
  actionsRow.className = 'checklist-actions';
  const selectAllBtn = document.createElement('button');
  selectAllBtn.type = 'button';
  selectAllBtn.textContent = 'Select all';
  const selectNoneBtn = document.createElement('button');
  selectNoneBtn.type = 'button';
  selectNoneBtn.textContent = 'Select none';
  // A billing filter next to Select all/none, not just a display badge —
  // with a provider like OpenRouter returning hundreds of models, this is
  // what lets "Select all" actually mean "all the free ones" instead of
  // requiring one-by-one picking. Reuses .day-check (an existing small
  // checkbox+label pattern) rather than adding new CSS.
  const freeLabel = document.createElement('label');
  freeLabel.className = 'day-check';
  const freeCb = document.createElement('input');
  freeCb.type = 'checkbox';
  freeLabel.append(freeCb, document.createTextNode('Free'));
  const paidLabel = document.createElement('label');
  paidLabel.className = 'day-check';
  const paidCb = document.createElement('input');
  paidCb.type = 'checkbox';
  paidLabel.append(paidCb, document.createTextNode('Paid'));
  // Live "N selected" count, pushed to the row's right edge — always
  // reflects every checked box regardless of the current text/billing
  // filter, matching exactly what resolveSelection() below actually
  // submits, so this number never disagrees with what clicking Add sends.
  const selectedCountEl = document.createElement('span');
  selectedCountEl.className = 'hint checklist-selected-count';
  actionsRow.append(selectAllBtn, selectNoneBtn, freeLabel, paidLabel, selectedCountEl);
  const filterField = document.createElement('input');
  filterField.type = 'text';
  filterField.className = 'filter-search checklist-filter';
  filterField.placeholder = 'Filter models…';
  const checklist = document.createElement('div');
  checklist.className = 'checklist';
  checklistWrap.append(actionsRow, filterField, checklist);
  wrapper.appendChild(checklistWrap);

  const statusEl = document.createElement('p');
  statusEl.className = 'hint hidden';
  wrapper.appendChild(statusEl);

  let checkboxes = [];
  let discoveredItems = []; // full {model, label, contextTokens, billing} objects, indexed alongside checkboxes

  function updateSelectedCount() {
    const n = checkboxes.filter((cb) => cb.checked).length;
    selectedCountEl.textContent = `${n} selected`;
  }

  function renderChecklist(items) {
    discoveredItems = items;
    checklist.innerHTML = '';
    filterField.value = '';
    // A fresh discovery run starts with no billing filter applied — leaving
    // Free/Paid checked across two different addresses would silently hide
    // models the user has no reason to expect are being filtered.
    freeCb.checked = false;
    paidCb.checked = false;
    checkboxes = items.map((item) => {
      const row = document.createElement('label');
      row.className = 'check-row';
      row.dataset.search = `${item.label || ''} ${item.model}`.toLowerCase();
      row.dataset.billing = item.billing || 'unknown';

      const cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.checked = items.length <= AUTO_CHECK_LIMIT;
      cb.value = item.model;
      row.appendChild(cb);

      const main = document.createElement('span');
      main.className = 'check-row-main';
      main.appendChild(Object.assign(document.createElement('span'), { textContent: item.label || item.model }));
      if (item.label && item.label !== item.model) {
        main.appendChild(Object.assign(document.createElement('span'), { className: 'list-row-sub', textContent: item.model }));
      }
      row.appendChild(main);

      const billing = item.billing || 'unknown';
      row.appendChild(Object.assign(document.createElement('span'), { className: `badge badge-${billing}`, textContent: billing }));

      checklist.appendChild(row);
      return cb;
    });
    applyFilters();
    updateSelectedCount();
    checklistWrap.classList.toggle('hidden', items.length === 0);
    modelField.wrapper.classList.toggle('hidden', items.length > 0);
  }

  // Hides a row unless it matches BOTH the free-text search and the billing
  // checkboxes. Neither Free nor Paid checked means billing doesn't filter
  // anything (today's plain-search behavior); one or both checked narrows
  // to rows matching a checked value.
  function applyFilters() {
    const q = filterField.value.trim().toLowerCase();
    const billingOn = freeCb.checked || paidCb.checked;
    for (const row of checklist.children) {
      const matchesText = q === '' || row.dataset.search.includes(q);
      const matchesBilling = !billingOn || (freeCb.checked && row.dataset.billing === 'free') || (paidCb.checked && row.dataset.billing === 'paid');
      row.classList.toggle('hidden', !(matchesText && matchesBilling));
    }
  }

  filterField.addEventListener('input', applyFilters);
  freeCb.addEventListener('change', applyFilters);
  paidCb.addEventListener('change', applyFilters);
  // One delegated listener covers every checkbox renderChecklist() ever
  // creates (they're rebuilt per discovery run) — a native `change` event
  // on an individual checkbox bubbles up to `checklist`.
  checklist.addEventListener('change', updateSelectedCount);

  // Scoped to visible rows — otherwise "Select all" after filtering to
  // Free would silently re-add every hidden paid model too, defeating the
  // point of filtering before picking.
  selectAllBtn.addEventListener('click', () => {
    for (const cb of checkboxes) if (!cb.closest('.check-row').classList.contains('hidden')) cb.checked = true;
    updateSelectedCount(); // setting .checked directly doesn't fire 'change'
  });
  selectNoneBtn.addEventListener('click', () => {
    for (const cb of checkboxes) if (!cb.closest('.check-row').classList.contains('hidden')) cb.checked = false;
    updateSelectedCount();
  });

  const discoverBtn = document.createElement('button');
  discoverBtn.type = 'button';
  discoverBtn.className = 'btn';
  discoverBtn.textContent = 'Find models at this address';
  if (canDiscover) wrapper.appendChild(discoverBtn);

  async function runDiscovery() {
    discoverBtn.disabled = true;
    discoverBtn.textContent = 'Looking…';
    statusEl.classList.add('hidden');
    try {
      const data = await discover();
      const items = data.models || [];
      fillSuggestions(items.map((m) => m.model));
      if (data.error) {
        statusEl.className = 'error';
        statusEl.textContent = data.error;
        statusEl.classList.remove('hidden');
        renderChecklist([]);
      } else if (items.length) {
        renderChecklist(items);
        statusEl.className = 'hint';
        statusEl.textContent = `Found ${items.length} model(s) — pick the ones to add.`;
        statusEl.classList.remove('hidden');
      } else {
        statusEl.className = 'hint';
        statusEl.textContent = "No models found at that address — double-check it's running and reachable.";
        statusEl.classList.remove('hidden');
        renderChecklist([]);
      }
    } catch {
      statusEl.className = 'error';
      statusEl.textContent = 'Could not reach that address.';
      statusEl.classList.remove('hidden');
    } finally {
      discoverBtn.disabled = false;
      discoverBtn.textContent = 'Find models at this address';
    }
  }
  discoverBtn.addEventListener('click', runDiscovery);

  /**
   * Called by the modal's submit handler. One of three outcomes:
   *   {models}  — ready to submit: an array of {model, label?, contextTokens?,
   *               billing?} objects (a typed name becomes {model: typed}, with
   *               no billing/contextTokens known — the server infers billing
   *               as a fallback). Checked boxes resolve to the FULL discovered
   *               object for each, so billing/contextTokens survive into the
   *               POST body instead of being dropped down to bare id strings.
   *   {pending} — discovery just ran and revealed a checklist for review;
   *               the modal should relabel its button and wait for a
   *               SECOND click rather than committing immediately — a
   *               one-click auto-add could otherwise silently register a
   *               dozen models the user never got to look at first.
   *   {error}   — a validation message to show instead
   */
  async function resolveSelection() {
    const typed = modelField.input.value.trim();
    if (typed) return { models: [{ model: typed }] };

    if (!checklistWrap.classList.contains('hidden')) {
      // The checklist is already showing — this click IS the "Add selected" step.
      const checkedValues = new Set(checkboxes.filter((cb) => cb.checked).map((cb) => cb.value));
      if (checkedValues.size) {
        const models = discoveredItems.filter((item) => checkedValues.has(item.model));
        return { models };
      }
      return { error: 'Pick at least one model, or use "Select all".' };
    }

    if (!canDiscover) return { error: 'Please type a model name.' };

    // Nothing typed, nothing discovered yet — run discovery as part of this
    // same click (no separate "Find models" click required first), then
    // stop and let the checklist be reviewed before anything is added.
    await runDiscovery();
    if (checkboxes.length) return { pending: true };
    return { error: 'No models found there — type one manually, or check the address.' };
  }

  return { wrapper, resolveSelection };
}

function buildAddConnectionModal(adapters, suggestions, onChange) {
  let adapterField, labelField, baseUrlField, secretField, picker;

  return openModal({
    title: 'Add a model',
    submitLabel: 'Test and add',
    busyLabel: 'Testing…',
    size: 'wide',
    build(body) {
      adapterField = fieldSelect(
        'Type',
        adapters.map((a) => [a.name, a.label])
      );
      body.appendChild(adapterField.wrapper);

      labelField = fieldInput('Name to show you', 'text', 'e.g. "My fast model"');
      body.appendChild(labelField.wrapper);

      baseUrlField = fieldInput('Address (local/self-hosted servers only)', 'text', 'e.g. http://localhost:11434/v1');
      body.appendChild(baseUrlField.wrapper);

      secretField = fieldInput('API key (leave blank for local servers)', 'password', 'Paste key here…');
      body.appendChild(secretField.wrapper);

      const pickerHolder = document.createElement('div');
      body.appendChild(pickerHolder);

      function rebuildPicker() {
        pickerHolder.innerHTML = '';
        const adapter = adapterField.select.value;
        picker = buildModelPicker({
          suggestions: suggestions[adapter] || [],
          canDiscover: true,
          discover: () =>
            fetch('/api/connections/discover', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ adapter, baseUrl: baseUrlField.input.value.trim(), secret: secretField.input.value }),
            }).then((r) => r.json()),
        });
        pickerHolder.appendChild(picker.wrapper);
        baseUrlField.wrapper.classList.toggle('hidden', adapter !== 'openai-compatible');
      }
      adapterField.select.addEventListener('change', rebuildPicker);
      baseUrlField.input.addEventListener('change', rebuildPicker);
      rebuildPicker();
    },
    async onSubmit(api) {
      const selection = await picker.resolveSelection();
      if (selection.pending) {
        api.setSubmitLabel('Add selected');
        return null; // discovery revealed a checklist — wait for a second, deliberate click
      }
      if (selection.error) {
        api.setError(selection.error);
        return null;
      }
      const data = await postJson('/api/connections', {
        adapter: adapterField.select.value,
        baseUrl: baseUrlField.input.value.trim() || undefined,
        label: labelField.input.value.trim() || undefined,
        secret: secretField.input.value || undefined,
        models: selection.models,
      });
      if (!data.ok) {
        api.setError(data.error || 'Could not add that model.');
        return null;
      }
      return data;
    },
  }).then((result) => {
    if (result) onChange();
  });
}

function buildAddModelsModal(connection, onChange) {
  let picker;

  return openModal({
    title: `Add models to "${connection.label}"`,
    submitLabel: 'Add',
    busyLabel: 'Adding…',
    size: 'wide',
    build(body) {
      picker = buildModelPicker({
        canDiscover: true,
        discover: () =>
          fetch('/api/connections/discover', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ connectionId: connection.id }),
          }).then((r) => r.json()),
      });
      body.appendChild(picker.wrapper);
    },
    async onSubmit(api) {
      const selection = await picker.resolveSelection();
      if (selection.pending) {
        api.setSubmitLabel('Add selected');
        return null;
      }
      if (selection.error) {
        api.setError(selection.error);
        return null;
      }
      const data = await postJson('/api/models', { connectionId: connection.id, models: selection.models });
      if (!data.ok) {
        api.setError(data.error || 'Could not add those models.');
        return null;
      }
      // Discovery already excludes models this connection has (registry.js's
      // discoverModels()) — this only catches the manual-typed-name path,
      // which bypasses that filter and hits addModel()'s guard instead.
      if (!data.added?.length && data.failed?.length) {
        api.setError(data.failed[0]?.error || 'That model is already added under this connection.');
        return null;
      }
      return data;
    },
  }).then((result) => {
    if (result) onChange();
  });
}

function buildConnectionGroup(connection, models, health, onChange) {
  const group = document.createElement('div');
  group.className = 'connection-group';

  const head = document.createElement('div');
  head.className = 'connection-head';
  const headMain = document.createElement('div');
  headMain.className = 'connection-head-main';
  const titleRow = document.createElement('div');
  titleRow.className = 'connection-title-row';
  titleRow.appendChild(Object.assign(document.createElement('span'), { className: 'connection-title', textContent: connection.label }));
  // The connection's TRUE total model count — deliberately connection.modelCount
  // (server-computed) rather than this group's own `models` array length,
  // since that array is post-filter whenever the main screen's search/status
  // filter is active; this badge must stay accurate regardless of what's
  // currently filtered into view.
  titleRow.appendChild(
    Object.assign(document.createElement('span'), {
      className: 'badge',
      textContent: `${connection.modelCount} model${connection.modelCount === 1 ? '' : 's'}`,
    })
  );
  headMain.appendChild(titleRow);
  const sub = [connection.baseUrl, connection.hasSecret ? 'key saved' : 'no key needed'].filter(Boolean).join(' · ');
  headMain.appendChild(Object.assign(document.createElement('span'), { className: 'connection-sub', textContent: sub }));
  head.appendChild(headMain);

  const headActions = document.createElement('div');
  headActions.className = 'list-row-actions';
  const addModelsBtn = document.createElement('button');
  addModelsBtn.type = 'button';
  addModelsBtn.className = 'btn';
  addModelsBtn.textContent = 'Add models';
  addModelsBtn.addEventListener('click', () => buildAddModelsModal(connection, onChange));
  const removeConnBtn = armedButton(
    'Remove connection',
    `Remove it and ${connection.modelCount} model${connection.modelCount === 1 ? '' : 's'}?`,
    async () => {
      await fetch(`/api/connections/${encodeURIComponent(connection.id)}`, { method: 'DELETE' });
      await onChange();
    }
  );
  headActions.append(addModelsBtn, removeConnBtn);
  head.appendChild(headActions);

  group.appendChild(head);
  for (const m of models) group.appendChild(buildModelRow(m, health[m.id], onChange));
  return group;
}

function buildModelsCard(connections, models, health, onChange, filterState) {
  const card = sectionCard('Your models');

  if (!models.length) {
    card.appendChild(
      Object.assign(document.createElement('p'), { className: 'hint', textContent: 'No models added yet — add one above.' })
    );
    return card;
  }

  const visible = models.filter((m) => modelMatchesFilter(m, filterState));

  const byConnection = new Map();
  for (const m of visible) {
    const key = m.connectionId || '';
    if (!byConnection.has(key)) byConnection.set(key, []);
    byConnection.get(key).push(m);
  }

  let anyVisible = false;
  for (const connection of connections) {
    const group = byConnection.get(connection.id);
    if (group && group.length) {
      anyVisible = true;
      card.appendChild(buildConnectionGroup(connection, group, health, onChange));
    }
  }

  // Defensive: a model whose connection went missing (shouldn't happen —
  // deleteConnection cascades — but never hide a model silently).
  const orphans = byConnection.get('') || [];
  if (orphans.length) {
    anyVisible = true;
    const group = document.createElement('div');
    group.className = 'connection-group';
    group.appendChild(
      Object.assign(document.createElement('p'), { className: 'hint', textContent: 'Not linked to a saved connection:' })
    );
    for (const m of orphans) group.appendChild(buildModelRow(m, health[m.id], onChange));
    card.appendChild(group);
  }

  if (!anyVisible) {
    card.appendChild(
      Object.assign(document.createElement('p'), { className: 'hint', textContent: 'No models match the current filter.' })
    );
  }

  return card;
}

/**
 * The primary-view filter bar: a search box plus status segments (all /
 * free / paid / working / not-working), and the "Check all models" button
 * that hits the new POST /api/models/recheck route. `onFilterChange` re-runs
 * a local (no-fetch) re-render of just the models card; `onRecheck` runs
 * after a successful recheck and does a full page refresh so the new
 * availability/billing data shows up everywhere (badges, filter counts).
 */
function buildFilterBar(filterState, onFilterChange, onRecheck) {
  const bar = document.createElement('div');
  bar.className = 'filter-bar';

  const search = document.createElement('input');
  search.type = 'text';
  search.className = 'filter-search';
  search.placeholder = 'Search models…';
  search.value = filterState.query;
  search.addEventListener('input', () => {
    filterState.query = search.value;
    onFilterChange();
  });
  bar.appendChild(search);

  const statusFilter = dropdownControl(
    [
      { value: 'all', label: 'All' },
      { value: 'free', label: 'Free' },
      { value: 'paid', label: 'Paid' },
      { value: 'working', label: 'Working now' },
      { value: 'not_working', label: 'Not working' },
    ],
    {
      value: filterState.status,
      onChange: (value) => {
        filterState.status = value;
        onFilterChange();
      },
    }
  );
  bar.appendChild(statusFilter.wrapper);

  const recheckBtn = document.createElement('button');
  recheckBtn.type = 'button';
  recheckBtn.className = 'btn';
  recheckBtn.textContent = 'Check all models';
  bar.appendChild(recheckBtn);

  const status = document.createElement('p');
  status.className = 'hint filter-bar-status hidden';
  bar.appendChild(status);

  recheckBtn.addEventListener('click', async () => {
    recheckBtn.disabled = true;
    recheckBtn.textContent = 'Checking…';
    status.classList.add('hidden');
    try {
      const data = await postJson('/api/models/recheck', {});
      if (!data.ok) {
        status.className = 'error filter-bar-status';
        status.textContent = data.error || 'Could not check models.';
        status.classList.remove('hidden');
        recheckBtn.disabled = false;
        recheckBtn.textContent = 'Check all models';
        return;
      }
      onRecheck();
    } catch {
      status.className = 'error filter-bar-status';
      status.textContent = 'Could not reach the Jarvis server.';
      status.classList.remove('hidden');
      recheckBtn.disabled = false;
      recheckBtn.textContent = 'Check all models';
    }
  });

  return bar;
}

export async function render(container) {
  container.innerHTML = '';

  const [modelsRes, prefsRes, adaptersRes] = await Promise.all([
    fetch('/api/models'),
    fetch('/api/prefs'),
    fetch('/api/models/adapters'),
  ]);
  const { connections, models, health } = await modelsRes.json();
  const prefs = await prefsRes.json();
  const { adapters, suggestions } = await adaptersRes.json();

  const onChange = () => render(container);

  container.appendChild(buildPrefsCard(prefs));

  // Moved above the filter bar/models list (was appended after everything,
  // at the very bottom, before this) — with a long models list that meant
  // scrolling past every connection and every model just to add one more.
  const addBtn = document.createElement('button');
  addBtn.type = 'button';
  addBtn.className = 'btn btn-primary';
  addBtn.textContent = '+ Add a model';
  addBtn.addEventListener('click', () => buildAddConnectionModal(adapters, suggestions, onChange));
  container.appendChild(addBtn);

  const filterState = { query: '', status: 'all' };
  const modelsSection = document.createElement('div');
  container.appendChild(modelsSection);

  function renderModelsSection() {
    modelsSection.innerHTML = '';
    if (models.length) {
      modelsSection.appendChild(buildFilterBar(filterState, renderModelsSection, onChange));
    }
    modelsSection.appendChild(buildModelsCard(connections, models, health, onChange, filterState));
  }
  renderModelsSection();
}

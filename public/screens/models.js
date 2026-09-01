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

import { fieldInput, sectionCard, armedButton, postJson } from './_helpers.js';
import { openModal } from './_modal.js';
import { dropdownControl, toggleSwitch, iconTile } from './_ui.js';

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

const CAP_LABELS = { vision: 'Images', video: 'Video', audio: 'Audio', webSearch: 'Web search' };

/**
 * A collapsible "Technical details" block for a failed test/discovery —
 * the raw (redacted) adapter error text server/models/registry.js's
 * `detail` field now carries alongside the friendly one-sentence message.
 * Previously that friendly sentence ("That connection didn't work.") was
 * the ENTIRE signal a user had to diagnose a failure with; this is what
 * makes an OmniRoute-class failure (wrong path, no key sent, wrong model
 * name, server down — four failures that all read identically before this)
 * actually distinguishable. Plain textContent throughout — `detail` is raw
 * provider/server text. Returns null when there's nothing to show.
 */
function technicalDetails(detail) {
  if (!detail) return null;
  const details = document.createElement('details');
  details.className = 'technical-details';
  const summary = document.createElement('summary');
  summary.textContent = 'Technical details';
  const pre = document.createElement('p');
  pre.className = 'hint';
  pre.textContent = detail;
  details.append(summary, pre);
  return details;
}

/** Renders a probe's `steps[]` (server/models/probe.js — what it tried, in plain language) as a small list. Absent/empty on every non-Custom provider's plain discoverModels() result, so this is a no-op there. */
function probeStepsList(steps) {
  if (!steps?.length) return null;
  const list = document.createElement('ul');
  list.className = 'probe-steps hint';
  for (const step of steps) {
    list.appendChild(Object.assign(document.createElement('li'), { textContent: step }));
  }
  return list;
}

function formatDateTime(iso) {
  if (!iso) return null;
  return new Date(iso).toLocaleString('en-US', { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
}

/** Read-only "what is this model, really" view — id, connection, adapter, capabilities, tier, and health detail were previously visible nowhere at all; clicking a row used to do nothing. */
function openModelDetail(m, connection) {
  openModal({
    title: m.label,
    submitLabel: 'Close',
    build(body) {
      const info = sectionCard('Details');
      info.appendChild(Object.assign(document.createElement('p'), { className: 'hint', textContent: `Model id: ${m.model}` }));
      info.appendChild(Object.assign(document.createElement('p'), { className: 'hint', textContent: `Connection: ${connection?.label || 'unknown'}` }));
      info.appendChild(Object.assign(document.createElement('p'), { className: 'hint', textContent: `Billing: ${m.billing || 'unknown'}` }));
      body.appendChild(info);

      const caps = Object.entries(CAP_LABELS)
        .filter(([key]) => m.caps?.[key])
        .map(([, label]) => label);
      const capsCard = sectionCard('Can handle');
      capsCard.appendChild(
        Object.assign(document.createElement('p'), { textContent: caps.length ? caps.join(', ') : 'Text only' })
      );
      body.appendChild(capsCard);

      if (m.tier) {
        const tierCard = sectionCard('Tier (1-5)');
        tierCard.appendChild(
          Object.assign(document.createElement('p'), {
            className: 'hint',
            textContent: `Speed: ${m.tier.speed ?? '—'} · Quality: ${m.tier.quality ?? '—'} · Cost: ${m.tier.cost ?? '—'}`,
          })
        );
        body.appendChild(tierCard);
      }

      const healthCard = sectionCard('Health');
      const state = m.availability?.state;
      healthCard.appendChild(
        Object.assign(document.createElement('p'), { textContent: state ? AVAILABILITY_LABELS[state] || state : m.ready ? 'ready' : 'needs a key' })
      );
      if (m.availability?.detail) {
        healthCard.appendChild(Object.assign(document.createElement('p'), { className: 'hint', textContent: m.availability.detail }));
      }
      const checked = formatDateTime(m.availability?.checkedAt);
      if (checked) healthCard.appendChild(Object.assign(document.createElement('p'), { className: 'hint', textContent: `Last checked: ${checked}` }));
      body.appendChild(healthCard);
    },
    async onSubmit() {
      return true;
    },
  });
}

function buildPrefsCard(prefs, models) {
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

  // Separate from the global manual pin (autoSelect/manualModelId, set from
  // the main screen's settings panel) — that one applies to every turn, text
  // and voice alike. This pins a model specifically for spoken turns
  // (source: 'voice'), and outranks the global pick only there — a typed
  // message is unaffected either way. See runner.js's preferredModelId()
  // for the exact precedence. Exists because auto-ranking's speed/cost
  // scoring produces wide ties on a real model list, and a spoken
  // conversation landing on whatever wins that tie (by quality, then id —
  // see router.js) is worse than just letting the user say which model
  // should actually be talking to them.
  const voiceModelRow = document.createElement('label');
  voiceModelRow.className = 'settings-row';
  voiceModelRow.appendChild(Object.assign(document.createElement('span'), { textContent: 'Pin a model for voice' }));
  const voiceModelSelect = document.createElement('select');
  voiceModelSelect.appendChild(Object.assign(document.createElement('option'), { value: '', textContent: 'None — use the pick above' }));
  for (const m of models || []) {
    voiceModelSelect.appendChild(
      Object.assign(document.createElement('option'), {
        value: m.id,
        textContent: m.enabled && m.ready ? m.label : `${m.label} (not ready)`,
      })
    );
  }
  voiceModelSelect.value = prefs.voiceModelId || '';
  voiceModelRow.appendChild(voiceModelSelect);
  card.appendChild(voiceModelRow);

  const hint = document.createElement('p');
  hint.className = 'hint';
  hint.textContent =
    "Auto-select weighs each model's speed, quality, and cost for the task at hand, and switches " +
    'models automatically if one breaks — Jarvis will tell you when that happens. Turn it off to pin ' +
    "a specific model instead, from the main screen's settings panel. Pinning a model for voice above " +
    "only affects spoken conversation — typed messages still follow the pick above it.";
  card.appendChild(hint);

  const save = (patch) => postJson('/api/prefs', patch);
  autoCheck.addEventListener('change', () => save({ autoSelect: autoCheck.checked }));
  balanceSelect.addEventListener('change', () => save({ balance: balanceSelect.value }));
  claritySelect.addEventListener('change', () => save({ clarifySensitivity: claritySelect.value }));
  voiceModelSelect.addEventListener('change', () => save({ voiceModelId: voiceModelSelect.value || null }));

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
  if (status === 'local') return m.billing === 'local';
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

function buildModelRow(m, healthInfo, onChange, connection) {
  const row = document.createElement('div');
  row.className = 'list-row list-row-clickable';
  row.tabIndex = 0;
  row.setAttribute('role', 'button');

  const main = document.createElement('div');
  main.className = 'list-row-main';
  main.appendChild(Object.assign(document.createElement('span'), { className: 'list-row-title', textContent: m.label }));
  const sub = document.createElement('span');
  sub.className = 'list-row-sub';
  sub.textContent = m.model;
  main.appendChild(sub);
  // A separate line for Test's result — it used to overwrite `sub` above
  // (destroying the model id until the next full re-render), which also
  // meant a failed test permanently hid which model it was even about.
  const testResult = document.createElement('span');
  testResult.className = 'list-row-sub hidden';
  main.appendChild(testResult);

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
  // Every action here lives inside a row that's now clickable end-to-end —
  // without this, clicking any of them would also open the detail modal
  // underneath.
  actions.addEventListener('click', (e) => e.stopPropagation());

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
  // A separate block below the row (not inline in `testResult`, which is a
  // short single-line label) for the raw error text on a failed test — see
  // technicalDetails() above.
  const testDetailWrap = document.createElement('div');
  testBtn.addEventListener('click', async () => {
    testBtn.disabled = true;
    testBtn.textContent = 'Testing…';
    testDetailWrap.innerHTML = '';
    try {
      const res = await fetch(`/api/models/${encodeURIComponent(m.id)}/test`, { method: 'POST' });
      const result = await res.json();
      testResult.textContent = result.ok ? 'Connection works.' : result.error || 'Connection failed.';
      testResult.classList.remove('hidden');
      if (!result.ok) {
        const detail = technicalDetails(result.detail);
        if (detail) testDetailWrap.appendChild(detail);
      }
    } catch {
      testResult.textContent = 'Could not reach the Jarvis server.';
      testResult.classList.remove('hidden');
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
  // Same guard as `actions` above — a click inside the "Technical details"
  // block (opening it, selecting its text) must not also open the row's
  // own detail modal underneath it.
  testDetailWrap.addEventListener('click', (e) => e.stopPropagation());
  row.append(main, actions, testDetailWrap);
  const open = () => openModelDetail(m, connection);
  row.addEventListener('click', open);
  row.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      open();
    }
  });
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
  // Same pattern as Free/Paid — narrows a large discovered list (an
  // OpenRouter- or OmniRoute-scale host can return hundreds of entries) down
  // to just the self-hosted, no-cost ones.
  const localLabel = document.createElement('label');
  localLabel.className = 'day-check';
  const localCb = document.createElement('input');
  localCb.type = 'checkbox';
  localLabel.append(localCb, document.createTextNode('Local'));
  // Live "N selected" count, pushed to the row's right edge — always
  // reflects every checked box regardless of the current text/billing
  // filter, matching exactly what resolveSelection() below actually
  // submits, so this number never disagrees with what clicking Add sends.
  const selectedCountEl = document.createElement('span');
  selectedCountEl.className = 'hint checklist-selected-count';
  actionsRow.append(selectAllBtn, selectNoneBtn, freeLabel, paidLabel, localLabel, selectedCountEl);
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

  // Only ever populated for a Custom connection's probe (server/models/
  // probe.js returns `steps`; the plain per-provider discoverModels() never
  // does) — empty and inert for every other provider's discovery.
  const stepsWrap = document.createElement('div');
  wrapper.appendChild(stepsWrap);
  const detailWrap = document.createElement('div');
  wrapper.appendChild(detailWrap);

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
    // Free/Paid/Local checked across two different addresses would silently
    // hide models the user has no reason to expect are being filtered.
    freeCb.checked = false;
    paidCb.checked = false;
    localCb.checked = false;
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
  // checkboxes. None of Free/Paid/Local checked means billing doesn't
  // filter anything (today's plain-search behavior); one or more checked
  // narrows to rows matching any checked value.
  function applyFilters() {
    const q = filterField.value.trim().toLowerCase();
    const billingOn = freeCb.checked || paidCb.checked || localCb.checked;
    for (const row of checklist.children) {
      const matchesText = q === '' || row.dataset.search.includes(q);
      const matchesBilling =
        !billingOn ||
        (freeCb.checked && row.dataset.billing === 'free') ||
        (paidCb.checked && row.dataset.billing === 'paid') ||
        (localCb.checked && row.dataset.billing === 'local');
      row.classList.toggle('hidden', !(matchesText && matchesBilling));
    }
  }

  filterField.addEventListener('input', applyFilters);
  freeCb.addEventListener('change', applyFilters);
  paidCb.addEventListener('change', applyFilters);
  localCb.addEventListener('change', applyFilters);
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
    stepsWrap.innerHTML = '';
    detailWrap.innerHTML = '';
    try {
      const data = await discover();
      const steps = probeStepsList(data.steps);
      if (steps) stepsWrap.appendChild(steps);
      const items = data.models || [];
      fillSuggestions(items.map((m) => m.model));
      if (data.error) {
        statusEl.className = 'error';
        statusEl.textContent = data.error;
        statusEl.classList.remove('hidden');
        const detail = technicalDetails(data.detail);
        if (detail) detailWrap.appendChild(detail);
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

function buildAddConnectionModal(providers, onChange) {
  // `providers` is server/models/providers.js's PROVIDERS list — the user
  // picks one of these five tiles (OpenAI/Anthropic/Gemini/Local server/
  // Custom) instead of a wire-protocol dropdown. Replaces the old "Type"
  // <select> (an adapter name like "openai-compatible") entirely — see the
  // Provider System Refactor design note in CLAUDE.md for why.
  let selectedId = providers[0]?.id;
  let labelField, baseUrlField, secretField, picker, extraWrap;
  let tileButtons = [];
  // The last successful Custom probe's resolved shape, so the final submit
  // can reuse it instead of probing the address all over again — a second,
  // independent network round-trip to the user's own server for
  // information already in hand, confirmed live to sometimes fail on its
  // own (a real, working 115-model OmniRoute connection was rejected this
  // way) even when the connection itself is fine. Keyed to the exact
  // address+key it was resolved against — used at submit time only if
  // neither has changed since; otherwise the server falls back to probing
  // fresh, exactly as before this existed.
  let resolvedCustom = null;

  return openModal({
    title: 'Add a model',
    submitLabel: 'Test and add',
    busyLabel: 'Testing…',
    size: 'wide',
    build(body) {
      const tileRow = document.createElement('div');
      tileRow.className = 'connector-card-row';
      tileButtons = providers.map((p) => {
        const card = document.createElement('div');
        card.className = 'connector-card';
        card.tabIndex = 0;
        card.setAttribute('role', 'button');
        card.appendChild(iconTile({ content: p.icon, bg: p.iconBg }));
        card.appendChild(Object.assign(document.createElement('span'), { className: 'connector-card-label', textContent: p.label }));
        const select = () => {
          selectedId = p.id;
          for (const btn of tileButtons) btn.el.classList.toggle('selected', btn.id === p.id);
          rebuildFields();
        };
        card.addEventListener('click', select);
        card.addEventListener('keydown', (e) => {
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault();
            select();
          }
        });
        tileRow.appendChild(card);
        return { id: p.id, el: card };
      });
      body.appendChild(tileRow);

      labelField = fieldInput('Name to show you', 'text', 'e.g. "My fast model"');
      body.appendChild(labelField.wrapper);

      baseUrlField = fieldInput('Address', 'text', 'e.g. http://localhost:11434/v1');
      body.appendChild(baseUrlField.wrapper);

      secretField = fieldInput('API key', 'password', 'Paste key here…');
      body.appendChild(secretField.wrapper);

      const pickerHolder = document.createElement('div');
      body.appendChild(pickerHolder);

      function currentProvider() {
        return providers.find((p) => p.id === selectedId) || providers[0];
      }

      function rebuildFields() {
        const p = currentProvider();
        if (!p) return;

        baseUrlField.wrapper.classList.toggle('hidden', !p.urlEditable);
        if (p.urlEditable) {
          baseUrlField.input.placeholder = p.baseUrl || 'e.g. https://your-server/v1';
        }
        secretField.wrapper.querySelector('label').textContent =
          p.keyRequired === false ? 'API key (usually not needed)' : p.keyRequired === true ? 'API key' : 'API key (optional)';
        secretField.input.placeholder = p.keyHint || 'Paste key here…';
        labelField.input.placeholder = `e.g. "${p.label}"`;

        rebuildPicker();
      }

      function rebuildPicker() {
        const p = currentProvider();
        pickerHolder.innerHTML = '';
        picker = buildModelPicker({
          suggestions: p.suggestions || [],
          canDiscover: true,
          discover: () => {
            const baseUrl = baseUrlField.input.value.trim();
            const secret = secretField.input.value;
            // Custom has no known adapter yet — the probe (server/models/
            // probe.js) is what works that out, trying each wire shape in
            // turn and reporting what it tried. Every other tile already
            // knows its adapter+baseUrl, so it goes through the plain
            // per-provider discovery route unchanged.
            const url = p.id === 'custom' ? '/api/connections/probe' : '/api/connections/discover';
            // Named payload, not `body` — this closure is nested inside
            // build(body), and shadowing that parameter here would be
            // confusing even though nothing inside this function needs it.
            const payload =
              p.id === 'custom'
                ? { baseUrl, secret }
                : { adapter: p.adapter, baseUrl: p.urlEditable ? baseUrl || p.baseUrl : p.baseUrl, secret };
            // A fresh discovery run invalidates any earlier resolution
            // outright — if this one fails, submit must not silently reuse
            // a stale success from before the user changed something.
            resolvedCustom = null;
            return fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) })
              .then((r) => r.json())
              .then((data) => {
                if (p.id === 'custom' && data.ok && data.adapter && typeof data.keyRequired === 'boolean') {
                  resolvedCustom = { baseUrl, secret, adapter: data.adapter, baseUrlResolved: data.baseUrl, kind: data.kind, keyRequired: data.keyRequired };
                }
                return data;
              });
          },
        });
        pickerHolder.appendChild(picker.wrapper);
      }

      baseUrlField.input.addEventListener('change', rebuildPicker);
      tileButtons[0]?.el.classList.add('selected');
      rebuildFields();

      // Populated only on a failed final "Test and add" — separate from
      // the picker's own stepsWrap/detailWrap (those cover a failed
      // discovery run; a typed model name skips discovery entirely and
      // hits this submit path directly, so a Custom probe failure needs
      // somewhere to show its steps/detail here too).
      extraWrap = document.createElement('div');
      body.appendChild(extraWrap);
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
      extraWrap.innerHTML = '';
      const currentBaseUrl = baseUrlField.input.value.trim();
      const currentSecret = secretField.input.value;
      // Only reused if neither the address nor the key has changed since
      // that probe ran — otherwise this is stale and the server re-probes
      // fresh on its own (the safe default, unchanged from before).
      const stillValid =
        selectedId === 'custom' && resolvedCustom && resolvedCustom.baseUrl === currentBaseUrl && resolvedCustom.secret === currentSecret;
      const data = await postJson('/api/connections', {
        provider: selectedId,
        baseUrl: currentBaseUrl || undefined,
        label: labelField.input.value.trim() || undefined,
        secret: currentSecret || undefined,
        models: selection.models,
        resolved: stillValid
          ? { adapter: resolvedCustom.adapter, baseUrl: resolvedCustom.baseUrlResolved, kind: resolvedCustom.kind, keyRequired: resolvedCustom.keyRequired }
          : undefined,
      });
      if (!data.ok) {
        api.setError(data.error || 'Could not add that model.');
        const steps = probeStepsList(data.steps);
        if (steps) extraWrap.appendChild(steps);
        const detail = technicalDetails(data.detail);
        if (detail) extraWrap.appendChild(detail);
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

/** PATCH /api/connections/:id existed since connections shipped — nothing in the UI ever called it with more than the discovery/add flow's own initial values. Renaming a connection or fixing a typo'd address meant deleting and re-adding it (losing every model under it) until this. */
function buildEditConnectionModal(connection, providers, onChange) {
  let labelField, baseUrlField, secretField;
  // `connection.provider` is always present by now (publicConnection()
  // backfills it via providerForLegacy() for anything saved before the
  // provider catalog existed) — falls back to 'custom' only if somehow
  // absent, so the Address field defaults to editable rather than
  // silently locked.
  const providerRow = providers.find((p) => p.id === connection.provider);
  const urlEditable = providerRow ? providerRow.urlEditable : true;
  return openModal({
    title: `Edit "${connection.label}"`,
    submitLabel: 'Save',
    busyLabel: 'Saving…',
    build(body) {
      const providerLine = document.createElement('p');
      providerLine.className = 'hint';
      providerLine.textContent = `Provider: ${providerRow?.label || 'Custom'}`;
      body.appendChild(providerLine);

      labelField = fieldInput('Name to show you', 'text');
      labelField.input.value = connection.label || '';
      body.appendChild(labelField.wrapper);

      baseUrlField = fieldInput('Address', 'text', 'e.g. http://localhost:11434/v1');
      baseUrlField.input.value = connection.baseUrl || '';
      baseUrlField.wrapper.classList.toggle('hidden', !urlEditable);
      body.appendChild(baseUrlField.wrapper);

      secretField = fieldInput(connection.hasSecret ? 'API key (leave blank to keep the current one)' : 'API key', 'password', 'Paste key here…');
      body.appendChild(secretField.wrapper);
    },
    async onSubmit(api) {
      if (!labelField.input.value.trim()) {
        api.setError('Please give this connection a name.');
        return null;
      }
      const data = await postJson(
        `/api/connections/${encodeURIComponent(connection.id)}`,
        {
          label: labelField.input.value.trim(),
          baseUrl: baseUrlField.input.value.trim() || undefined,
          secret: secretField.input.value || undefined,
        },
        'PATCH'
      );
      if (!data.ok) {
        api.setError(data.error || 'Could not save that connection.');
        return null;
      }
      return data;
    },
  }).then((result) => {
    if (result) onChange();
  });
}

function buildConnectionGroup(connection, models, health, providers, onChange) {
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
  const editConnBtn = document.createElement('button');
  editConnBtn.type = 'button';
  editConnBtn.className = 'btn';
  editConnBtn.textContent = 'Edit';
  editConnBtn.addEventListener('click', () => buildEditConnectionModal(connection, providers, onChange));
  const removeConnBtn = armedButton(
    'Remove connection',
    `Remove it and ${connection.modelCount} model${connection.modelCount === 1 ? '' : 's'}?`,
    async () => {
      await fetch(`/api/connections/${encodeURIComponent(connection.id)}`, { method: 'DELETE' });
      await onChange();
    }
  );
  headActions.append(addModelsBtn, editConnBtn, removeConnBtn);
  head.appendChild(headActions);

  group.appendChild(head);
  for (const m of models) group.appendChild(buildModelRow(m, health[m.id], onChange, connection));
  return group;
}

function buildModelsCard(connections, models, health, providers, onChange, filterState) {
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
      card.appendChild(buildConnectionGroup(connection, group, health, providers, onChange));
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
    for (const m of orphans) group.appendChild(buildModelRow(m, health[m.id], onChange, null));
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
      { value: 'local', label: 'Local' },
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

  const [modelsRes, prefsRes, providersRes] = await Promise.all([
    fetch('/api/models'),
    fetch('/api/prefs'),
    fetch('/api/models/providers'),
  ]);
  const { connections, models, health } = await modelsRes.json();
  const prefs = await prefsRes.json();
  const { providers } = await providersRes.json();

  const onChange = () => render(container);

  container.appendChild(buildPrefsCard(prefs, models));

  // Moved above the filter bar/models list (was appended after everything,
  // at the very bottom, before this) — with a long models list that meant
  // scrolling past every connection and every model just to add one more.
  const addBtn = document.createElement('button');
  addBtn.type = 'button';
  addBtn.className = 'btn btn-primary';
  addBtn.textContent = '+ Add a model';
  addBtn.addEventListener('click', () => buildAddConnectionModal(providers, onChange));
  container.appendChild(addBtn);

  const filterState = { query: '', status: 'all' };
  const modelsSection = document.createElement('div');
  container.appendChild(modelsSection);

  function renderModelsSection() {
    modelsSection.innerHTML = '';
    if (models.length) {
      modelsSection.appendChild(buildFilterBar(filterState, renderModelsSection, onChange));
    }
    modelsSection.appendChild(buildModelsCard(connections, models, health, providers, onChange, filterState));
  }
  renderModelsSection();
}

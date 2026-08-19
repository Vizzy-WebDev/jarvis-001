// App Control screen — full rebuild against the user's original prompt, not
// an incremental patch of the earlier "Connectors" screen. Two surfaces, one
// unchanged from before and one genuinely new:
//
// - **Three tabs — MCP / API / CLI** — the "organize MCP, API, and CLI as
//   separate sub-views... selecting one should show only that category's
//   connections" requirement. Each tab lists only connectors of that type.
// - **The single `+ Add` button** sits above the tabs (not duplicated per
//   tab) and, per the user's explicit instruction, keeps its exact existing
//   shape: one button, a small menu with exactly two choices — "Browse
//   connectors" and "Add custom connector".
//
// "Browse connectors" (the Official Connectors directory) is unchanged
// behavior: one searchable list, name + description + Connect, mechanism
// never shown or asked — every catalog entry already knows which mechanism
// it needs. "Add custom connector" is the one place a user picks a
// mechanism directly, since a custom connector's mechanism can't be
// inferred the way a catalog entry's can.
//
// Desktop control, Browser, and Files have no card here at all — they're
// Jarvis's own built-in abilities, not user-added connectors (see
// CLAUDE.md's "Desktop control / Browser / Files have no settings screen").

import { fieldInput, fieldTextarea, fieldSelect, sectionCard, postJson } from './_helpers.js';
import { openModal } from './_modal.js';
import { iconTile, popover, segmented } from './_ui.js';
import { iconForConnector } from './_connector-icons.js';
import { render as renderConnectorDetail } from './_connector-detail.js';

const TABS = [
  ['mcp', 'MCP'],
  ['api', 'API'],
  ['cli', 'CLI'],
];

// 'status-connected' is its own explicit green, not the shared `.badge.good`
// (a blue accent used elsewhere for "working" states like model
// availability) — the user specifically asked for Connected to read as
// green, which would otherwise mean re-theming .badge.good app-wide.
const STATUS_META = {
  working: { label: 'Connected', className: 'status-connected' },
  error: { label: 'Reconnect', className: 'bad' },
};

function statusBadge(status) {
  const meta = STATUS_META[status] || { label: 'Connect', className: '' };
  const badge = document.createElement('span');
  badge.className = `badge ${meta.className}`;
  badge.textContent = meta.label;
  return badge;
}

// A connector row: real logo tile + name only, left-to-right — no
// description (matches the reference exactly; a raw catalog/MCP description
// is documentation, not a row label — see _connector-detail.js's own
// "Available tools" section for the fuller story on that). `.connector-row`
// overrides the base `.list-row-main`'s column stacking (built for a
// title-over-subtitle row elsewhere) with a plain horizontal icon+name pair
// — the old code put the icon ABOVE the name rather than beside it, a
// pre-existing layout bug this fixes along with dropping the description.
function buildRow({ icon, iconDataUri, label, status }, onOpen) {
  const el = document.createElement('div');
  el.className = 'list-row list-row-clickable';
  el.tabIndex = 0;
  el.setAttribute('role', 'button');

  const main = document.createElement('div');
  main.className = 'list-row-main connector-row';
  main.appendChild(iconTile(iconForConnector({ catalogIcon: icon, iconDataUri })));
  main.appendChild(Object.assign(document.createElement('span'), { className: 'list-row-title', textContent: label }));

  const actions = document.createElement('div');
  actions.className = 'list-row-actions';
  actions.appendChild(statusBadge(status));

  el.append(main, actions);
  el.addEventListener('click', onOpen);
  el.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      onOpen();
    }
  });
  return el;
}

// ---------- Browse connectors (the full Official Connectors directory) ----------
// Restyled against a user-supplied Claude Desktop reference screenshot: a
// short strip of bordered "Popular" cards up top (icon + name + inline
// Connect), then the remaining catalog entries as plain table rows — rather
// than the single flat searchable list this used to be. Mechanism is still
// never shown while browsing (every catalog entry already knows which one it
// needs); that part of the behavior is unchanged from the pre-restyle code.

function matchesSearch(entry, query) {
  if (!query) return true;
  return entry.label.toLowerCase().includes(query) || entry.description.toLowerCase().includes(query);
}

// A catalog entry's own inline action: a status badge if already connected
// (reuses the same statusBadge() the main tab list uses, so "Connected"/
// "Reconnect" always reads identically everywhere), otherwise a plain
// Connect button. Either way, clicking it (or the row/card it sits in) opens
// the same detail page — this is a label change, not a second code path.
function buildCatalogAction(entry, onPick) {
  if (entry.status) return statusBadge(entry.status);
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'btn';
  btn.textContent = 'Connect';
  btn.addEventListener('click', (e) => {
    e.stopPropagation(); // the card/row itself is also clickable; avoid double-firing onPick
    onPick(entry);
  });
  return btn;
}

function buildPopularCard(entry, onPick) {
  const card = document.createElement('div');
  card.className = 'connector-card';
  card.tabIndex = 0;
  card.setAttribute('role', 'button');
  card.appendChild(iconTile(iconForConnector({ catalogIcon: entry.icon, iconDataUri: entry.iconDataUri })));
  card.appendChild(Object.assign(document.createElement('span'), { className: 'connector-card-label', textContent: entry.label }));
  card.appendChild(buildCatalogAction(entry, onPick));
  card.addEventListener('click', () => onPick(entry));
  card.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      onPick(entry);
    }
  });
  return card;
}

function buildCatalogTableRow(entry, onPick) {
  const row = document.createElement('div');
  row.className = 'list-row list-row-clickable';
  row.tabIndex = 0;
  row.setAttribute('role', 'button');

  const main = document.createElement('div');
  main.className = 'list-row-main connector-row';
  main.appendChild(iconTile(iconForConnector({ catalogIcon: entry.icon, iconDataUri: entry.iconDataUri })));
  main.appendChild(Object.assign(document.createElement('span'), { className: 'list-row-title', textContent: entry.label }));

  const actions = document.createElement('div');
  actions.className = 'list-row-actions';
  actions.appendChild(buildCatalogAction(entry, onPick));

  row.append(main, actions);
  row.addEventListener('click', () => onPick(entry));
  row.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      onPick(entry);
    }
  });
  return row;
}

// Up to the first 3 entries (catalog.json's own order) are pulled out as
// "Popular" cards, same shape as the reference; the rest render as table
// rows below with no duplication between the two sections. A search in
// progress drops the Popular/table split entirely and shows one flat table
// of matches instead — splitting a 1-2 result search into two half-empty
// sections would read as broken, not faithful to the reference.
function renderCatalogList(listWrap, entries, query, onPick) {
  listWrap.innerHTML = '';
  const matches = entries.filter((entry) => matchesSearch(entry, query));

  if (!matches.length) {
    const message = entries.length ? 'Nothing matches that search.' : 'No official connectors yet — try "Add custom connector" instead.';
    listWrap.appendChild(Object.assign(document.createElement('p'), { className: 'hint', textContent: message }));
    return;
  }

  if (!query) {
    const popular = matches.slice(0, 3);
    const rest = matches.slice(3);
    if (popular.length) {
      listWrap.appendChild(Object.assign(document.createElement('p'), { className: 'catalog-section-label', textContent: 'Popular' }));
      const cardRow = document.createElement('div');
      cardRow.className = 'connector-card-row';
      for (const entry of popular) cardRow.appendChild(buildPopularCard(entry, onPick));
      listWrap.appendChild(cardRow);
    }
    for (const entry of rest) listWrap.appendChild(buildCatalogTableRow(entry, onPick));
    return;
  }

  for (const entry of matches) listWrap.appendChild(buildCatalogTableRow(entry, onPick));
}

async function buildDirectoryModal(onOpenDetail) {
  // Fetched once, filtered client-side on every keystroke rather than
  // re-fetched per keystroke — re-fetching per keystroke previously caused a
  // real race (an earlier, slower fetch resolving and rendering AFTER a
  // newer one already had, leaving stale/duplicate rows). Filtering an
  // already-loaded array is synchronous, so there's nothing left to race.
  const { catalog } = await fetch('/api/connectors/catalog').then((res) => res.json());

  let modalApi;
  const pickEntry = (entry) => {
    modalApi.close();
    // Every catalog entry today ends up type 'mcp' once connected —
    // server.js's /ensure route always creates the mcp record — but this
    // isn't hardcoded as a broader assumption here: the detail page reports
    // back its real type once known, so a future non-mcp catalog entry
    // needs no change at all in this file. Every entry — guide or not —
    // goes straight to the detail page; there is no Client ID/Secret UI
    // anywhere in this app any more (the user's explicit choice: Gmail/
    // Drive/GitHub/Slack stay in the catalog and stay clickable, but Jarvis
    // never asks for credentials on-screen — see CLAUDE.md).
    onOpenDetail({ catalogEntry: entry, connectorId: entry.connectorId, type: 'mcp' });
  };

  await openModal({
    title: 'Browse connectors',
    submitLabel: 'Close',
    size: 'wide', // room for the Popular card strip to actually read as cards, not a squeeze
    build(body, api) {
      modalApi = api;
      const searchField = fieldInput('', 'text', 'Search connectors…');
      body.appendChild(searchField.wrapper);
      const listWrap = document.createElement('div');
      body.appendChild(listWrap);

      renderCatalogList(listWrap, catalog, '', pickEntry);
      searchField.input.addEventListener('input', () => {
        renderCatalogList(listWrap, catalog, searchField.input.value.trim().toLowerCase(), pickEntry);
      });
    },
    async onSubmit() {
      return true;
    },
  });
}

// ---------- Add custom connector ----------
// Genuinely new: asks which mechanism up front, then shows the matching
// small form. This is the one place a user chooses MCP/API/CLI directly.

function buildMechanismFields(body, mechanism) {
  if (mechanism === 'mcp') {
    const urlField = fieldInput('Remote MCP Server URL', 'text', 'https://example.com/mcp');
    body.appendChild(urlField.wrapper);
    // Deliberately no Client ID/Secret field of any kind here, collapsed or
    // not — the user's explicit choice, made after direct comparison against
    // Claude's own "Advanced settings" UI. Most custom MCP servers register
    // Jarvis as a client on the spot (Dynamic Client Registration) and never
    // need one; a server that doesn't just won't connect from this screen —
    // see CLAUDE.md's "No Client ID/Secret UI anywhere" rule for what that
    // trades away and why it was still the user's call to make.
    return { urlField };
  }
  if (mechanism === 'api') {
    const baseUrlField = fieldInput('Base web address', 'text', 'https://api.example.com');
    const authKindField = fieldSelect('How the key is sent', [
      ['bearer', 'Authorization: Bearer header (most common)'],
      ['header', 'A custom header'],
      ['query', 'A web address parameter'],
    ]);
    const authNameField = fieldInput('Header or parameter name (only if not "Bearer")', 'text', 'e.g. "X-API-Key"');
    const apiKeyField = fieldInput('API key (optional — can add later)', 'password');
    body.append(baseUrlField.wrapper, authKindField.wrapper, authNameField.wrapper, apiKeyField.wrapper);
    return { baseUrlField, authKindField, authNameField, apiKeyField };
  }
  // 'cli'
  const commandField = fieldInput('Command', 'text', 'e.g. "git" or "npm"');
  body.appendChild(
    Object.assign(document.createElement('p'), {
      className: 'hint',
      textContent: 'After adding this, Jarvis runs its --help and proposes a list of subcommands for you to review before anything is saved.',
    })
  );
  body.appendChild(commandField.wrapper);
  return { commandField };
}

async function buildAddConnectorModal(onDone) {
  let labelField, descriptionField, mechanismFields;
  let mechanism = 'mcp';
  let fieldsWrap;

  const result = await openModal({
    title: 'Add custom connector',
    submitLabel: 'Add',
    build(body) {
      const seg = segmented(
        [
          ['mcp', 'MCP server'],
          ['api', 'API key'],
          ['cli', 'CLI command'],
        ],
        {
          value: mechanism,
          onChange(v) {
            mechanism = v;
            fieldsWrap.innerHTML = '';
            mechanismFields = buildMechanismFields(fieldsWrap, mechanism);
          },
        }
      );
      body.appendChild(seg.wrapper);

      labelField = fieldInput('Name', 'text', 'e.g. "My team\'s server"');
      body.appendChild(labelField.wrapper);

      fieldsWrap = document.createElement('div');
      body.appendChild(fieldsWrap);
      mechanismFields = buildMechanismFields(fieldsWrap, mechanism);

      descriptionField = fieldTextarea('Description (optional)');
      body.appendChild(descriptionField.wrapper);
    },
    async onSubmit(api) {
      const label = labelField.input.value.trim();
      if (!label) {
        api.setError('Please enter a name.');
        return null;
      }
      const description = descriptionField.textarea.value.trim() || undefined;
      let body = { type: mechanism, label, description };

      if (mechanism === 'mcp') {
        const url = mechanismFields.urlField.input.value.trim();
        if (!url) {
          api.setError('Please enter a server URL.');
          return null;
        }
        body.url = url;
      } else if (mechanism === 'api') {
        const baseUrl = mechanismFields.baseUrlField.input.value.trim();
        if (!baseUrl) {
          api.setError('Please enter a base web address.');
          return null;
        }
        body.baseUrl = baseUrl;
        body.authKind = mechanismFields.authKindField.select.value;
        body.authName = mechanismFields.authNameField.input.value.trim() || undefined;
        body.apiKey = mechanismFields.apiKeyField.input.value.trim() || undefined;
      } else {
        const command = mechanismFields.commandField.input.value.trim();
        if (!command) {
          api.setError('Please enter a command.');
          return null;
        }
        body.command = command;
      }

      const data = await postJson('/api/connectors/custom', body);
      if (!data.ok) {
        api.setError(data.error || 'Could not add that connector.');
        return null;
      }
      return data;
    },
  });

  if (result) onDone({ connectorId: result.connectorId, type: mechanism });
}

// ---------- screen ----------

export async function render(container) {
  // Remembered across a list<->detail round trip within one visit to this
  // screen so "which tab am I on" survives opening and closing a connector
  // — reset to 'mcp' on a fresh render() (e.g. navigating away and back).
  let activeTab = 'mcp';

  async function showList() {
    container.innerHTML = '';
    const [connectorsRes, catalogRes] = await Promise.all([fetch('/api/connectors'), fetch('/api/connectors/catalog')]);
    const { connectors } = await connectorsRes.json();
    const { catalog } = await catalogRes.json();
    const catalogById = new Map(catalog.map((e) => [e.id, e]));
    // files/browser are Jarvis's own built-in abilities, never shown as
    // connectors here — only the three user-facing mechanisms ever appear.
    const userConnectors = connectors.filter((c) => c.type === 'mcp' || c.type === 'api' || c.type === 'cli');

    // "+ Add" — one button, unchanged shape, above the tabs.
    const buttonRow = document.createElement('div');
    buttonRow.className = 'button-row';
    const addBtn = document.createElement('button');
    addBtn.type = 'button';
    addBtn.className = 'btn btn-primary';
    addBtn.textContent = '+ Add';
    addBtn.addEventListener('click', () => {
      const pop = popover({
        anchor: addBtn,
        build(body) {
          const list = document.createElement('div');
          list.className = 'popover-list';
          const browseOpt = document.createElement('button');
          browseOpt.type = 'button';
          browseOpt.className = 'popover-option';
          browseOpt.textContent = 'Browse connectors';
          browseOpt.addEventListener('click', () => {
            pop.close();
            buildDirectoryModal(showDetail);
          });
          const customOpt = document.createElement('button');
          customOpt.type = 'button';
          customOpt.className = 'popover-option';
          customOpt.textContent = 'Add custom connector';
          customOpt.addEventListener('click', () => {
            pop.close();
            buildAddConnectorModal(({ connectorId, type }) => showDetail({ catalogEntry: null, connectorId, type }));
          });
          list.append(browseOpt, customOpt);
          body.appendChild(list);
        },
      });
    });
    buttonRow.appendChild(addBtn);
    container.appendChild(buttonRow);

    const seg = segmented(TABS, {
      value: activeTab,
      onChange(v) {
        activeTab = v;
        renderTabContent();
      },
    });
    container.appendChild(seg.wrapper);

    const tabContentWrap = document.createElement('div');
    container.appendChild(tabContentWrap);

    function renderTabContent() {
      tabContentWrap.innerHTML = '';
      const tabLabel = TABS.find(([v]) => v === activeTab)[1];
      const card = sectionCard(`${tabLabel} connectors`);
      const filtered = userConnectors.filter((c) => c.type === activeTab);

      if (!filtered.length) {
        card.appendChild(
          Object.assign(document.createElement('p'), {
            className: 'hint',
            textContent: 'Nothing here yet — browse official connectors or add a custom one.',
          })
        );
      }
      for (const c of filtered) {
        // A catalog-sourced connector still needs its full catalog entry
        // (icon, and — for a guided-flow service in an 'error' state — its
        // guide steps) even when opened from this list rather than the
        // directory.
        const catalogEntry = c.source?.type === 'catalog' ? catalogById.get(c.source.id) : null;
        card.appendChild(
          buildRow({ icon: catalogEntry?.icon, iconDataUri: c.iconDataUri, label: c.label, status: c.status?.state }, () =>
            showDetail({ catalogEntry, connectorId: c.id, type: c.type })
          )
        );
      }
      tabContentWrap.appendChild(card);
    }
    renderTabContent();
    seg.setValue(activeTab);
  }

  async function showDetail({ catalogEntry, connectorId, type }) {
    if (type) activeTab = type; // land back on the right tab after Back
    container.innerHTML = '';
    await renderConnectorDetail(container, { catalogEntry, connectorId, type, onBack: showList });
  }

  await showList();
}

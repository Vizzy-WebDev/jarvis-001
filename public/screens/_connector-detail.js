// The per-connector detail page — opened from a row in app-control.js's
// tabbed connector list (already-added) or its Browse-connectors directory
// (official, not yet added), whether that row is a service never connected
// before, one mid-setup, or an already-connected custom one. Same page
// either way. Not a hash route — app-control.js swaps its own container's
// content between its view(s) and this view internally, the same "wipe and
// rebuild" pattern every screen already uses, just at a finer grain within
// one section (see that file's render()).
//
// Works identically for all three mechanisms below the header: the
// "Available tools" section reads `connector.tools` — a uniform shape
// server.js's publicConnector() computes regardless of whether a tool came
// from an mcp server, an api operation, or a cli command — so nothing here
// needs to know or care which mechanism it's looking at once something is
// actually configured. What differs per mechanism is only how a connector
// GETS configured in the first place (buildMcpConnectSection /
// buildApiSetupSection / buildCliSetupSection below).

import { fieldInput, fieldSelect, sectionCard, armedButton, postJson } from './_helpers.js';
import { iconTile, segmented, dropdownControl } from './_ui.js';
import { iconForConnector } from './_connector-icons.js';

// Zero-cost, zero-model-call heuristic grouping — a tool/operation/command
// list carries no server-declared category, so this buckets by any verb
// found among a name's words, not just a leading one: real services vary in
// convention (e.g. Notion's own tools are "notion-search"/"notion-fetch"/
// "notion-update-page" — the service name leads, not the verb), so the name
// is split into words first and every word is checked. An honest
// simplification: documented as a heuristic, not a claim that it mirrors any
// given service's own real information architecture.
const GROUP_RULES = [
  { words: ['search', 'list', 'find', 'query'], label: 'Search & browse' },
  { words: ['read', 'get', 'fetch'], label: 'Read' },
  { words: ['create', 'write', 'update', 'edit', 'set', 'upload'], label: 'Create & edit' },
  { words: ['delete', 'remove', 'move'], label: 'Delete & move' },
];
function groupFor(toolName) {
  const words = toolName
    .replace(/([a-z])([A-Z])/g, '$1 $2') // split camelCase boundaries too
    .toLowerCase()
    .split(/[^a-z]+/)
    .filter(Boolean);
  return GROUP_RULES.find((r) => r.words.some((w) => words.includes(w)))?.label || 'Other';
}

// A raw tool/operation/command name (e.g. "notion-get-comments",
// "list_files", "getPageComments") is an identifier, not prose — this turns
// it into something a non-technical reader can scan. Heuristic, not a claim
// of matching any given service's own hand-written display names: split on
// hyphens/underscores/camelCase boundaries, drop a leading word that just
// repeats the connector's own name (most real MCP servers prefix every tool
// with their service name, which is redundant once it's already grouped
// under that connector's own page), then sentence-case the rest.
function friendlyToolName(name, connectorLabel) {
  let words = String(name)
    .replace(/([a-z0-9])([A-Z])/g, '$1 $2')
    .split(/[-_\s]+/)
    .filter(Boolean);
  const labelWord = String(connectorLabel || '').toLowerCase();
  if (words.length > 1 && words[0].toLowerCase() === labelWord) words = words.slice(1);
  if (!words.length) return name;
  const sentence = words.join(' ').toLowerCase();
  return sentence.charAt(0).toUpperCase() + sentence.slice(1);
}

// Three states, not the old two: 'allowed' (default — a key's absence still
// means allowed, unchanged) | 'ask' (confirm every time, purely a user
// preference) | 'blocked'/legacy 'never' (never allowed at all). This is
// still only the standing-permission half of the picture — a `risky`
// tool's runtime confirmation (server.js's publicConnector() `risk` field)
// is Jarvis's own automatic judgment and can't be silenced by setting
// 'allowed' here; see the "Always confirms" badge below.
function permissionValue(permissions, toolName) {
  const v = permissions[toolName];
  if (v === 'blocked' || v === 'never') return 'blocked';
  if (v === 'ask') return 'ask';
  return 'allowed';
}

// "Needs approval" (renamed from this project's earlier "Ask every time" —
// same underlying 'ask' value, wording only) to match the user's Claude
// Desktop reference exactly.
const PERMISSION_TITLES = { allowed: 'Always allow', ask: 'Needs approval', blocked: 'Blocked' };

// The group-level dropdown's four options, in the reference's own order —
// glyphs match the per-tool switch's icons below so the same state always
// reads the same way whether it's shown on a row or the group control.
// 'custom' is a fourth, DISPLAY-ONLY state: it's what the dropdown's own
// button shows when a group's tools don't all agree (computed by
// aggregateValue() below, never stored), not a real settable value — there
// is no single concrete thing "set everything in this group to custom"
// could mean. It's listed here only so the open panel matches the
// reference visually; dropdownControl's onChange handler (in
// buildToolsSection() below) treats picking it as a no-op.
const GROUP_OPTIONS = [
  { value: 'allowed', label: 'Always allow', glyph: '✓' },
  { value: 'ask', label: 'Needs approval', glyph: '✋' },
  { value: 'blocked', label: 'Blocked', glyph: '🚫' },
  { value: 'custom', label: 'Custom', glyph: '⋯' },
];

/** 'custom' when a group's tools don't all share one permission value, otherwise that shared value — the group dropdown's own displayed state is always this computed from the real per-tool data, never tracked as separate state that could drift out of sync (the exact bug the user reported: the old button never updated at all). */
function aggregateValue(groupTools, permissions) {
  const values = groupTools.map((t) => permissionValue(permissions, t.name));
  return values.every((v) => v === values[0]) ? values[0] : 'custom';
}

/** The three-icon Always-allow/Ask/Deny control, styled after the user's Claude Desktop reference screenshot — built from the existing `segmented()` picker (same pill/active-highlight styling already used elsewhere) rather than a new component, with glyph labels and a11y titles added on top. */
function permissionSwitch(value, onChange) {
  const picker = segmented(
    [
      ['allowed', '✓'],
      ['ask', '✋'],
      ['blocked', '🚫'],
    ],
    { value, onChange }
  );
  picker.wrapper.classList.add('permission-switch');
  picker.wrapper.querySelectorAll('.segmented-item').forEach((btn) => {
    const title = PERMISSION_TITLES[btn.dataset.value];
    btn.title = title;
    btn.setAttribute('aria-label', title);
  });
  return picker;
}

/** A brief red line appended under `card`, auto-removed after a few seconds — used when a permission save fails so the user isn't left staring at a control that silently didn't take. */
function flashSaveError(card, message) {
  const el = document.createElement('p');
  el.className = 'hint save-error';
  el.textContent = message;
  card.appendChild(el);
  setTimeout(() => el.remove(), 4000);
}

/** Same shape as _helpers.js's postJson, but rejects on a non-2xx response instead of silently resolving with whatever error JSON the server sent — the permission control needs to know a save actually failed so it can tell the user, not just log it. */
async function patchJsonStrict(url, body) {
  const res = await fetch(url, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  if (!res.ok) throw new Error(`Save failed (${res.status})`);
  return res.json();
}

// ---------- MCP mechanism — rewritten fresh as part of the MCP-mechanism
// rebuild (see CLAUDE.md's "The full connector-catalog-and-permissions
// rebuild"). This is new code, not code carried over from before that
// rebuild.
//
// **No Client ID/Secret UI anywhere in this file, on purpose — the user's
// explicit decision, made after repeated direct comparison against their own
// screenshots of Claude's real "Advanced settings" UI.** Earlier versions of
// this file put credential fields inline unconditionally, then conditionally
// once a failure or guide justified it, then behind an always-optional
// "Have a Client ID?" link shown only when a real attempt had already failed
// — each version a real improvement, but the user ultimately decided even
// the last, correctly-gated version was more than they wanted to see, ever.
// See CLAUDE.md's "No Client ID/Secret UI anywhere" for the full account and
// what this trades away: Gmail/GitHub/Slack/Google Drive stay in the catalog
// and stay clickable, but if the automatic chain (CIMD/DCR) can't complete —
// which, verified live, it never can for these four — the connector just
// shows the real reason as plain text with no way to act on it from this
// screen. A credential can still be registered, invisibly, by calling
// `POST /api/connectors/catalog/:catalogId/register-client` directly (see
// catalog-credentials.js) — that route was NOT removed; it's simply no
// longer reachable from any button or field a user can see.

/**
 * The "Available tools" section — grouped into collapsible sections with a
 * count badge and a group-level bulk-permission control (Claude Desktop's
 * own shape, per the user's reference screenshot), each tool row showing a
 * de-slugged name, its three-state permission switch, and (when genuinely
 * risky) a read-only badge. No per-tool description any more — Claude's own
 * reference shows none, and a real MCP tool's raw description is internal,
 * model-facing documentation running to a couple thousand characters, not
 * something a human reads on this screen (see server/connectors/index.js's
 * shortDescription() comment for the full story). Identical for mcp/api/cli;
 * only `connector.tools` differs per mechanism, already computed uniformly
 * server-side.
 */
function buildToolsSection(connector, onRefresh) {
  const card = sectionCard('Available tools');
  const tools = connector.tools || [];
  if (!tools.length) {
    card.appendChild(
      Object.assign(document.createElement('p'), { className: 'hint', textContent: 'Nothing set up yet — use the section above to add some.' })
    );
    return card;
  }

  const byGroup = new Map();
  for (const tool of tools) {
    const group = groupFor(tool.name);
    if (!byGroup.has(group)) byGroup.set(group, []);
    byGroup.get(group).push(tool);
  }

  // Mutated in place as saves succeed, mirroring the pre-rebuild code's own
  // approach — this section deliberately never re-renders on a permission
  // change (a 29-tool list re-rendering per click would scroll-jump), so the
  // in-memory copy has to be the thing kept in sync, not the DOM tree.
  const permissions = { ...(connector.config?.toolPermissions || {}) };

  /** Saves a full permission map in one PATCH — used by both the per-tool switch and the group bulk control, since the group control is really just "set every tool in this group to X" in one request rather than N. Reverts `applyLocal`'s already-applied UI change and flashes an error on failure, rather than leaving the control showing a value that didn't actually save. */
  async function save(next, applyLocal, revertLocal) {
    try {
      await patchJsonStrict(`/api/connectors/${connector.id}`, { config: { toolPermissions: next } });
      applyLocal();
    } catch {
      revertLocal();
      flashSaveError(card, "Couldn't save that — check your connection and try again.");
    }
  }

  for (const [group, groupTools] of byGroup) {
    const header = document.createElement('div');
    header.className = 'tool-group-header';

    const toggleBtn = document.createElement('button');
    toggleBtn.type = 'button';
    toggleBtn.className = 'tool-group-toggle';
    toggleBtn.setAttribute('aria-expanded', 'true');
    toggleBtn.textContent = '▾';

    const label = document.createElement('span');
    label.className = 'tool-group-label';
    label.textContent = group;
    const count = document.createElement('span');
    count.className = 'badge';
    count.textContent = String(groupTools.length);

    // Populated below, before the dropdown's onChange can ever actually
    // fire (a real click happens later, asynchronously) — safe for the
    // dropdown's own closure to reference despite being declared first.
    const rowSwitches = [];

    const groupDropdown = dropdownControl(GROUP_OPTIONS, {
      value: aggregateValue(groupTools, permissions),
      onChange(next) {
        if (next === 'custom') {
          // No concrete bulk action "Custom" could mean — it's a read-only
          // aggregate reflecting real disagreement among the group's tools,
          // not a settable value (see GROUP_OPTIONS' own comment above).
          // Snap the label back to the true current state rather than let
          // it show a "Custom" that isn't backed by anything real.
          groupDropdown.setValue(aggregateValue(groupTools, permissions));
          return;
        }
        const prevValues = groupTools.map((t) => permissionValue(permissions, t.name));
        const map = { ...permissions };
        for (const t of groupTools) {
          if (next === 'allowed') delete map[t.name];
          else map[t.name] = next;
        }
        rowSwitches.forEach((s) => s.setValue(next));
        save(
          map,
          () => {
            for (const t of groupTools) {
              if (next === 'allowed') delete permissions[t.name];
              else permissions[t.name] = next;
            }
          },
          () => {
            rowSwitches.forEach((s, i) => s.setValue(prevValues[i]));
            groupDropdown.setValue(aggregateValue(groupTools, permissions));
          }
        );
      },
    });
    groupDropdown.wrapper.title = `Set every tool in "${group}" at once`;
    // .tool-group-bulk gives this its margin-left:auto right-alignment
    // within .tool-group-header — dropdownControl()'s own wrapper class
    // (counter-btn dropdown-control) doesn't carry that on its own, which
    // regressed the group control to the left edge, bunched against the
    // count badge, when the old plain popover button (which DID have this
    // class) was replaced with the real dropdown.
    groupDropdown.wrapper.classList.add('tool-group-bulk');

    header.append(toggleBtn, label, count, groupDropdown.wrapper);
    card.appendChild(header);

    const body = document.createElement('div');
    body.className = 'tool-group-body';
    card.appendChild(body);

    toggleBtn.addEventListener('click', () => {
      const collapsed = body.classList.toggle('collapsed');
      toggleBtn.textContent = collapsed ? '▸' : '▾';
      toggleBtn.setAttribute('aria-expanded', String(!collapsed));
    });

    for (const tool of groupTools) {
      const row = document.createElement('div');
      row.className = 'list-row';
      const main = document.createElement('div');
      main.className = 'list-row-main';
      const titleRow = document.createElement('div');
      titleRow.appendChild(
        Object.assign(document.createElement('span'), { className: 'list-row-title', textContent: friendlyToolName(tool.name, connector.label) })
      );
      // Purely informational — Jarvis's own automatic judgment, not
      // something this page lets the user change. Shown so it's never a
      // surprise that an "always allow" tool still pauses to confirm.
      if (tool.risk === 'risky') {
        const badge = document.createElement('span');
        badge.className = 'badge';
        badge.textContent = 'Always confirms';
        badge.title = 'Jarvis always asks before using this — a standing permission here can\'t change that.';
        titleRow.appendChild(badge);
      }
      main.appendChild(titleRow);

      const current = permissionValue(permissions, tool.name);
      const switchEl = permissionSwitch(current, (next) => {
        const prev = permissionValue(permissions, tool.name);
        const map = { ...permissions };
        if (next === 'allowed') delete map[tool.name];
        else map[tool.name] = next;
        save(
          map,
          () => {
            if (next === 'allowed') delete permissions[tool.name];
            else permissions[tool.name] = next;
            // A single tool changing independently is exactly what can make
            // the group stop agreeing (or, just as real, start agreeing
            // again) — keep the group dropdown's own label truthful rather
            // than static, fixing the user's reported bug #2/#3 directly.
            groupDropdown.setValue(aggregateValue(groupTools, permissions));
          },
          () => switchEl.setValue(prev)
        );
      });
      rowSwitches.push(switchEl);

      row.append(main, switchEl.wrapper);
      body.appendChild(row);
    }
  }

  if (connector.type === 'mcp') {
    const refreshBtn = document.createElement('button');
    refreshBtn.type = 'button';
    refreshBtn.className = 'btn';
    refreshBtn.textContent = 'Refresh tools';
    refreshBtn.addEventListener('click', async () => {
      refreshBtn.disabled = true;
      await fetch(`/api/connectors/${connector.id}/test`, { method: 'POST' });
      await onRefresh();
    });
    card.appendChild(refreshBtn);
  }

  return card;
}

/** A checklist of proposed tools (from api/cli discovery), all ticked by default, with a Save button. `onSave(ticked)` receives just the array of ticked items. */
function buildProposedChecklist(proposed, saveLabel, onSave) {
  const wrap = document.createElement('div');
  const checkboxes = [];
  for (const item of proposed) {
    const row = document.createElement('label');
    row.className = 'settings-row';
    const checkbox = document.createElement('input');
    checkbox.type = 'checkbox';
    checkbox.checked = true;
    row.appendChild(checkbox);
    const textWrap = document.createElement('div');
    textWrap.appendChild(Object.assign(document.createElement('span'), { className: 'list-row-title', textContent: item.name }));
    textWrap.appendChild(Object.assign(document.createElement('span'), { className: 'list-row-sub', textContent: item.description || '' }));
    row.appendChild(textWrap);
    wrap.appendChild(row);
    checkboxes.push({ checkbox, item });
  }
  const saveBtn = document.createElement('button');
  saveBtn.type = 'button';
  saveBtn.className = 'btn btn-primary';
  saveBtn.textContent = saveLabel;
  saveBtn.addEventListener('click', async () => {
    saveBtn.disabled = true;
    const ticked = checkboxes.filter((c) => c.checkbox.checked).map((c) => c.item);
    await onSave(ticked);
  });
  wrap.appendChild(saveBtn);
  return wrap;
}

/** API mechanism — spec discovery, or one endpoint added by hand. Shown whenever a connector is type 'api', alongside the tools list once anything's configured (not an either/or the way mcp's OAuth connect is). */
function buildApiSetupSection(connector, onRefresh) {
  const card = sectionCard('Set up endpoints');
  const resultWrap = document.createElement('div');

  const specField = fieldInput('OpenAPI/Swagger document address', 'text', 'https://api.example.com/openapi.json');
  const discoverBtn = document.createElement('button');
  discoverBtn.type = 'button';
  discoverBtn.className = 'btn';
  discoverBtn.textContent = 'Discover from a spec';
  const errEl = document.createElement('p');
  errEl.className = 'error hidden';

  discoverBtn.addEventListener('click', async () => {
    discoverBtn.disabled = true;
    errEl.classList.add('hidden');
    resultWrap.innerHTML = '';
    const specUrl = specField.input.value.trim();
    if (!specUrl) {
      errEl.textContent = 'Enter a spec address first.';
      errEl.classList.remove('hidden');
      discoverBtn.disabled = false;
      return;
    }
    const data = await postJson(`/api/connectors/${connector.id}/api/discover`, { specUrl });
    discoverBtn.disabled = false;
    if (!data.ok) {
      errEl.textContent = data.error || "Couldn't read that document.";
      errEl.classList.remove('hidden');
      return;
    }
    resultWrap.appendChild(
      Object.assign(document.createElement('p'), { className: 'hint', textContent: `Found ${data.operations.length} endpoints — pick which ones Jarvis can use.` })
    );
    resultWrap.appendChild(
      buildProposedChecklist(data.operations, `Save ${data.operations.length ? '' : ''}selected endpoints`, async (ticked) => {
        await postJson(`/api/connectors/${connector.id}`, { config: { operations: ticked, baseUrl: data.baseUrl, specUrl } }, 'PATCH');
        await fetch(`/api/connectors/${connector.id}/test`, { method: 'POST' });
        await onRefresh();
      })
    );
  });

  card.append(specField.wrapper, discoverBtn, errEl, resultWrap);

  // Add one endpoint by hand — always available alongside spec discovery,
  // per "Custom Connectors... configured using whichever of the supported
  // mechanisms is appropriate" not assuming every API has a published spec.
  const manualCard = sectionCard('Or add one endpoint by hand');
  const nameField = fieldInput('Name', 'text', 'e.g. "list_customers"');
  const descField = fieldInput('What it does', 'text');
  const methodField = fieldSelect('Method', [
    ['GET', 'GET'],
    ['POST', 'POST'],
    ['PUT', 'PUT'],
    ['PATCH', 'PATCH'],
    ['DELETE', 'DELETE'],
  ]);
  const pathField = fieldInput('Path', 'text', '/customers/{id}');
  const addBtn = document.createElement('button');
  addBtn.type = 'button';
  addBtn.className = 'btn';
  addBtn.textContent = 'Add endpoint';
  const manualErr = document.createElement('p');
  manualErr.className = 'error hidden';
  addBtn.addEventListener('click', async () => {
    const name = nameField.input.value.trim();
    const path = pathField.input.value.trim();
    if (!name || !path) {
      manualErr.textContent = 'A name and a path are both required.';
      manualErr.classList.remove('hidden');
      return;
    }
    addBtn.disabled = true;
    const placeholders = [...path.matchAll(/\{([^}]+)\}/g)].map((m) => m[1]);
    const properties = {};
    for (const p of placeholders) properties[p] = { type: 'string' };
    const op = { name, description: descField.input.value.trim() || undefined, method: methodField.select.value, path, parameters: { type: 'object', properties, required: placeholders } };
    const existing = connector.config?.operations || [];
    await postJson(`/api/connectors/${connector.id}`, { config: { operations: [...existing, op] } }, 'PATCH');
    await fetch(`/api/connectors/${connector.id}/test`, { method: 'POST' });
    await onRefresh();
  });
  manualCard.append(nameField.wrapper, descField.wrapper, methodField.wrapper, pathField.wrapper, addBtn, manualErr);

  const outer = document.createElement('div');
  outer.append(card, manualCard);
  return outer;
}

/** CLI mechanism — runs the connector's own saved command's --help and proposes a subcommand list to review. */
function buildCliSetupSection(connector, onRefresh) {
  const card = sectionCard('Discover commands');
  card.appendChild(
    Object.assign(document.createElement('p'), {
      className: 'hint',
      textContent: `Runs "${connector.config?.command} --help" and proposes a list of subcommands for you to review — nothing is saved until you pick which ones and click Save.`,
    })
  );
  const discoverBtn = document.createElement('button');
  discoverBtn.type = 'button';
  discoverBtn.className = 'btn';
  discoverBtn.textContent = 'Discover commands';
  const errEl = document.createElement('p');
  errEl.className = 'error hidden';
  const resultWrap = document.createElement('div');

  discoverBtn.addEventListener('click', async () => {
    discoverBtn.disabled = true;
    errEl.classList.add('hidden');
    resultWrap.innerHTML = '';
    const data = await postJson(`/api/connectors/${connector.id}/cli/discover`, {});
    discoverBtn.disabled = false;
    if (!data.ok) {
      errEl.textContent = data.error || 'Could not discover commands.';
      errEl.classList.remove('hidden');
      return;
    }
    if (!data.proposed?.length) {
      resultWrap.appendChild(Object.assign(document.createElement('p'), { className: 'hint', textContent: "Nothing usable was found in that command's help text." }));
      return;
    }
    resultWrap.appendChild(
      Object.assign(document.createElement('p'), { className: 'hint', textContent: `Proposed ${data.proposed.length} commands — pick which ones Jarvis can use.` })
    );
    resultWrap.appendChild(
      buildProposedChecklist(data.proposed, 'Save selected commands', async (ticked) => {
        await postJson(`/api/connectors/${connector.id}`, { config: { commands: ticked } }, 'PATCH');
        await fetch(`/api/connectors/${connector.id}/test`, { method: 'POST' });
        await onRefresh();
      })
    );
  });

  card.append(discoverBtn, errEl, resultWrap);
  return card;
}


/** Polls until the connector's status settles (working/error) after the user finishes signing in elsewhere, since there's no way to be notified the moment they do. */
function pollUntilConnected(connectorId, { onConnected, onError }) {
  return setInterval(async () => {
    const { connectors } = await fetch('/api/connectors').then((res) => res.json());
    const updated = connectors.find((c) => c.id === connectorId);
    if (updated?.status?.state === 'working') onConnected();
    else if (updated?.status?.state === 'error') onError(updated.status.detail);
  }, 1500);
}

/**
 * MCP mechanism — the OAuth connect flow. A plain status line and one
 * Connect/Reconnect button. **No Client ID/Secret field, link, or modal
 * anywhere on this page, ever, for any connector — the user's own explicit,
 * final decision**, made after three successive corrections to this exact
 * area (unconditional inline fields → conditional inline fields → a
 * correctly-gated "Have a Client ID?" link) still left a credential surface
 * showing up somewhere. See CLAUDE.md's "No Client ID/Secret UI anywhere"
 * for the full account.
 *
 * When the automatic chain (CIMD/DCR) can't complete — Google/GitHub/Slack's
 * real OAuth servers never support it, verified live — the real,
 * service-specific reason (`oauth.js`'s `manualClientHint()`, persisted on
 * `connectFlow.manualClient`) is shown as plain status text, same as any
 * other failure. There is nothing to click on it. A credential can still be
 * registered for a catalog entry, invisibly, via
 * `POST /api/connectors/catalog/:catalogId/register-client` — that route
 * still exists; it's just not wired to any button or field a user can see.
 */
async function buildMcpConnectSection(connector, catalogEntry, label, onRefresh, setPollTimer) {
  const wasErrored = connector?.status?.state === 'error';
  // A real, service-specific reason a past attempt on THIS connector
  // couldn't register automatically — shown as plain status text below,
  // never as anything interactive.
  const manualHint = connector?.config?.connectFlow?.manualClient || null;

  const card = sectionCard(wasErrored ? 'Reconnect' : 'Connect');
  const statusEl = document.createElement('p');
  statusEl.className = 'hint';
  // A recorded manual-client reason (this connector's own past failure)
  // outranks a generic "could not connect" — it's the more specific,
  // more useful thing to tell the user first.
  statusEl.textContent = manualHint?.message
    ? manualHint.message
    : wasErrored
      ? connector?.status?.detail || 'Could not connect.'
      : `You are not connected to ${label} yet.`;
  card.appendChild(statusEl);

  const connectBtn = document.createElement('button');
  connectBtn.type = 'button';
  connectBtn.className = 'btn btn-primary';
  connectBtn.textContent = wasErrored ? 'Reconnect' : 'Connect';
  card.appendChild(connectBtn);

  const showStatus = (text) => {
    statusEl.textContent = text;
    statusEl.classList.remove('hidden');
  };

  async function ensureConnectorRecord() {
    if (connector?.id) return connector.id;
    const ensured = await postJson(`/api/connectors/catalog/${catalogEntry.id}/ensure`, {});
    if (!ensured.ok) throw new Error(ensured.error || 'Could not set up that connector.');
    return ensured.connectorId;
  }

  connectBtn.addEventListener('click', async () => {
    connectBtn.disabled = true;
    try {
      const connectorId = await ensureConnectorRecord();
      // Never sends credentials from here — this button only ever attempts
      // the automatic chain (CIMD, then DCR), or succeeds immediately if a
      // catalog-level credential was already registered server-side (see
      // catalog-credentials.js) — there is no UI path that ever sends one
      // from this click.
      const result = await postJson(`/api/connectors/${connectorId}/connect`, {});
      if (!result.ok) throw new Error(result.error || 'Could not start connecting that service.');

      if (result.needsManualClient) {
        // A real, service-specific explanation from oauth.js's
        // manualClientHint() — why automatic registration didn't work —
        // shown as plain text. Nothing on this page can act on it; see this
        // function's own doc comment for why.
        connectBtn.disabled = false;
        showStatus(result.message || 'This service needs a Client ID before Jarvis can connect.');
        return;
      }

      if (result.noAuthNeeded) {
        // A real, unauthenticated request against the server itself already
        // succeeded server-side (see oauth.js's probeAuthorization) — this
        // connector needs no OAuth at all and is already marked working, no
        // browser tab involved.
        await onRefresh(connectorId);
        return;
      }

      window.open(result.authUrl, '_blank', 'noopener');
      showStatus(`Waiting for you to finish signing in to ${label} in the new tab…`);
      connectBtn.textContent = 'Waiting…';

      const timer = pollUntilConnected(connectorId, {
        onConnected: async () => {
          clearInterval(timer);
          await fetch(`/api/connectors/${connectorId}/test`, { method: 'POST' }); // populate the real tool list right away
          await onRefresh(connectorId);
        },
        onError: (detail) => {
          clearInterval(timer);
          showStatus(`Could not connect: ${detail || 'unknown error'}`);
          connectBtn.disabled = false;
          connectBtn.textContent = 'Try again';
        },
      });
      setPollTimer(timer);
    } catch (err) {
      // A thrown error means this specific attempt failed for a real,
      // specific reason (a rejected DCR request, a network failure — see
      // oauth.js) — the provider's own explanation, shown as-is.
      showStatus(err?.message || 'Could not start connecting that service.');
      card.querySelector('h2').textContent = 'Reconnect';
      connectBtn.textContent = 'Reconnect';
      connectBtn.disabled = false;
    }
  });

  return card;
}

export async function render(container, { catalogEntry, connectorId, type, onBack }) {
  container.innerHTML = '';
  let pollTimer = null;
  const back = () => {
    if (pollTimer) clearInterval(pollTimer);
    onBack();
  };
  const refresh = (nextConnectorId = connectorId) => {
    // The poll-timer belongs to THIS render's closure — a re-render creates
    // a brand-new one via setPollTimer, so without this the old interval
    // just keeps running forever with nothing left referencing it.
    if (pollTimer) clearInterval(pollTimer);
    return render(container, { catalogEntry, connectorId: nextConnectorId, type, onBack });
  };

  const header = document.createElement('div');
  header.className = 'detail-header';
  const backBtn = document.createElement('button');
  backBtn.type = 'button';
  backBtn.className = 'btn';
  backBtn.textContent = '← Back';
  backBtn.addEventListener('click', back);
  header.appendChild(backBtn);
  container.appendChild(header);

  let connector = null;
  if (connectorId) {
    const res = await fetch('/api/connectors');
    const { connectors } = await res.json();
    connector = connectors.find((c) => c.id === connectorId) || null;
  }
  const connectorType = type || connector?.type || 'mcp';

  const iconKey = catalogEntry?.icon ?? null;
  const label = catalogEntry?.label || connector?.label || 'Connector';

  // icon + name on the left, the connector's action(s) — Disconnect and/or
  // Remove for mcp, Remove alone for api/cli — pinned to the far right,
  // matching the user's Claude Desktop reference, where these sit beside the
  // connector name instead of at the page's end. api/cli use
  // .detail-action-btn directly (a single button, margin-left:auto on
  // itself); mcp can show two at once, wrapped in .detail-actions instead
  // (margin-left:auto on the wrapper, so the pair moves as one unit).
  const titleRow = document.createElement('div');
  titleRow.className = 'detail-title-row';
  titleRow.appendChild(iconTile(iconForConnector({ catalogIcon: iconKey, iconDataUri: connector?.iconDataUri })));
  titleRow.appendChild(Object.assign(document.createElement('h2'), { textContent: label }));
  container.appendChild(titleRow);

  const description = catalogEntry?.description || connector?.description || `A custom ${connectorType.toUpperCase()} connector.`;
  container.appendChild(Object.assign(document.createElement('p'), { className: 'hint', textContent: description }));

  const isConfigured = connector?.status?.state === 'working';

  if (connectorType === 'mcp') {
    // Disconnect (signs out, keeps the connector and its tool permissions —
    // POST .../disconnect) and Remove (deletes it entirely, including any
    // saved OAuth client credentials — DELETE) are two different actions.
    // Remove shows for any connector record, connected or not — previously
    // there was no destructive action at all for one stuck short of
    // 'working', which meant a half-authenticated connector could never be
    // deleted from the UI (see CLAUDE.md's reconnection-glitch fix).
    if (connector) {
      const actions = document.createElement('div');
      actions.className = 'detail-actions';
      if (isConfigured) {
        const disconnectBtn = armedButton('Disconnect', 'Really disconnect?', async () => {
          await postJson(`/api/connectors/${connector.id}/disconnect`, {});
          await refresh();
        });
        actions.appendChild(disconnectBtn);
      }
      const removeBtn = armedButton('Remove', 'Really remove this connector?', async () => {
        await fetch(`/api/connectors/${connector.id}`, { method: 'DELETE' });
        back();
      });
      actions.appendChild(removeBtn);
      titleRow.appendChild(actions);
    }
    if (isConfigured) {
      container.appendChild(buildToolsSection(connector, refresh));
      return;
    }
    container.appendChild(await buildMcpConnectSection(connector, catalogEntry, label, refresh, (t) => (pollTimer = t)));
    return;
  }

  // api/cli — setup and the tools list can both be visible at once (you can
  // always discover/add more), unlike mcp's binary connect/connected split
  // which follows OAuth's own inherently binary nature.
  if (!connector) {
    container.appendChild(Object.assign(document.createElement('p'), { className: 'hint', textContent: 'Could not load this connector.' }));
    return;
  }
  const removeBtn = armedButton('Remove', 'Really remove this connector?', async () => {
    await fetch(`/api/connectors/${connector.id}`, { method: 'DELETE' });
    back();
  });
  removeBtn.classList.add('detail-action-btn');
  titleRow.appendChild(removeBtn);
  container.appendChild(connectorType === 'api' ? buildApiSetupSection(connector, refresh) : buildCliSetupSection(connector, refresh));
  if (connector.tools?.length) container.appendChild(buildToolsSection(connector, refresh));
}

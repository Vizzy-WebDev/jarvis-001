'use client';

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { AppIcon } from '@/components/ui/AppIcon';
import { Button } from '@/components/ui/Button';
import { Card } from '@/components/ui/Card';
import { EmptyState } from '@/components/ui/EmptyState';
import { Field, inputClass } from '@/components/ui/Field';
import { AskIcon, BlockIcon, CheckIcon, PlusIcon, SearchIcon } from '@/components/ui/Icons';
import { Modal } from '@/components/ui/Modal';
import { Popover } from '@/components/ui/Popover';
import { Toggle } from '@/components/ui/Toggle';
import { api, ApiRequestError } from '@/lib/api';
import type { CatalogEntry, Connector, ConnectorTool, ToolPermission } from '@/lib/api-types';

/**
 * The apps and services Jarvis is connected to.
 *
 * Three mechanisms, one uniform picture: MCP servers, plain API keys, and
 * local CLI commands are all "a connector" here, and a connector's real tools
 * — whichever mechanism they came from — render with the identical
 * name/permission control once something is actually configured. What differs
 * is only how a connector GETS configured (an OAuth round trip for MCP, a
 * pasted key for API, a saved command template for CLI).
 *
 * **One generic Advanced-settings form for every MCP connector, catalogue or
 * custom, not a per-service guided form with numbered steps.** The automatic
 * chain (Dynamic Client Registration) always runs first regardless of what
 * sits in the optional Client ID/Secret fields — they exist only for the rare
 * server that has genuinely told Jarvis, via a real failed attempt, that
 * automatic registration isn't possible.
 */

type Mechanism = 'mcp' | 'api' | 'cli';
const TABS: [Mechanism, string][] = [['mcp', 'MCP'], ['api', 'API'], ['cli', 'CLI']];

const STATUS_LABEL: Record<string, string> = {
  working: 'Connected',
  error: 'Reconnect',
  untested: 'Not tested yet',
};

function statusText(connector: Connector): string {
  const state = connector.status?.state;
  return (state && STATUS_LABEL[state]) || connector.status?.detail || 'Not connected';
}

/** A real signal, not just dimmer text: green once it actually works, amber on
 *  a real failure, and a plain dot for "added but never tried" — the same
 *  three-way `status.state` every row already carries, just made visible. */
function connectionDotClass(connector: Connector): string {
  const state = connector.status?.state;
  if (state === 'working') return 'bg-state-ok';
  if (state === 'error') return 'bg-state-danger';
  return 'bg-ink-faint';
}

interface DetailTarget {
  connectorId: string | null;
  catalogEntry: CatalogEntry | null;
  type: Mechanism;
}

export function AppControlScreen() {
  const [connectors, setConnectors] = useState<Connector[] | null>(null);
  const [catalog, setCatalog] = useState<CatalogEntry[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [tab, setTab] = useState<Mechanism>('mcp');
  const [addOpen, setAddOpen] = useState(false);
  const [browsing, setBrowsing] = useState(false);
  const [addingCustom, setAddingCustom] = useState(false);
  const [detail, setDetail] = useState<DetailTarget | null>(null);
  const addAnchor = useRef<HTMLSpanElement>(null);

  const load = useCallback(async () => {
    try {
      const [c, cat] = await Promise.all([api.connectors.list(), api.connectors.catalog()]);
      setConnectors(c.connectors);
      setCatalog(cat.catalog);
      setError(null);
    } catch (err) {
      setError(err instanceof ApiRequestError ? err.message : 'Could not read your connectors.');
      setConnectors([]);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const catalogById = useMemo(() => new Map(catalog.map((entry) => [entry.id, entry])), [catalog]);

  // Jarvis's own built-in abilities (files, browser) are never shown here —
  // only the three user-facing mechanisms are real "apps" to connect.
  const userConnectors = (connectors ?? []).filter((c) => c.type === 'mcp' || c.type === 'api' || c.type === 'cli');
  const shown = userConnectors.filter((c) => c.type === tab);

  function openExisting(connector: Connector) {
    const entry = connector.source?.type === 'catalog' && connector.source.id
      ? catalogById.get(connector.source.id) ?? null
      : null;
    setDetail({ connectorId: connector.id, catalogEntry: entry, type: connector.type as Mechanism });
  }

  async function afterChange() {
    setDetail(null);
    await load();
  }

  return (
    <>
      <div className="relative mb-4">
        <span ref={addAnchor} className="inline-block">
          <Button tone="primary" data-testid="add-connector-menu"
                  onClick={() => setAddOpen((was) => !was)}>
            <PlusIcon className="h-3.5 w-3.5" />
            Add
          </Button>
        </span>
        <Popover open={addOpen} anchorRef={addAnchor} onClose={() => setAddOpen(false)} width={220}>
          <div className="py-1">
            <button type="button" data-testid="browse-connectors"
                    className="block w-full rounded px-3 py-2 text-left text-[13px] text-ink hover:bg-white/[0.06]"
                    onClick={() => { setAddOpen(false); setBrowsing(true); }}>
              Browse connectors
            </button>
            <button type="button" data-testid="add-custom-connector"
                    className="block w-full rounded px-3 py-2 text-left text-[13px] text-ink hover:bg-white/[0.06]"
                    onClick={() => { setAddOpen(false); setAddingCustom(true); }}>
              Add custom connector
            </button>
          </div>
        </Popover>
      </div>

      {error && <p className="mb-4 text-[13px] text-state-danger">{error}</p>}

      <div className="mb-4 inline-flex rounded-pill border border-surface-border p-0.5">
        {TABS.map(([id, label]) => (
          <button
            key={id}
            type="button"
            data-testid={`tab-${id}`}
            aria-pressed={tab === id}
            onClick={() => setTab(id)}
            className={[
              'rounded-pill px-3 py-1.5 text-[13px] transition duration-150 ease-out',
              tab === id ? 'bg-accent/15 text-accent' : 'text-ink-muted hover:text-ink',
            ].join(' ')}
          >
            {label}
          </button>
        ))}
      </div>

      {connectors === null ? null : shown.length === 0 ? (
        <EmptyState
          title="Nothing here yet"
          body="Browse official connectors or add a custom one to get started."
          action={<Button tone="primary" onClick={() => setBrowsing(true)}>Browse connectors</Button>}
        />
      ) : (
        <div className="space-y-2" data-testid="connector-list">
          {shown.map((connector) => {
            const entry = connector.source?.type === 'catalog' && connector.source.id
              ? catalogById.get(connector.source.id) ?? null
              : null;
            const connected = connector.status?.state === 'working';
            return (
              <Card key={connector.id} interactive data-testid="connector-row"
                    className="flex items-center gap-3" onClick={() => openExisting(connector)}>
                <AppIcon label={entry?.label || connector.label} iconDataUri={entry?.iconDataUri ?? connector.iconDataUri} />
                <div className="min-w-0 flex-1">
                  <p className={`truncate text-[14px] ${connector.enabled ? 'text-ink' : 'text-ink-faint'}`}>
                    {connector.label}
                  </p>
                  <p className="mt-0.5 flex items-center gap-1.5 truncate text-[12px] text-ink-faint">
                    <span aria-hidden data-testid="connection-dot"
                          className={`h-1.5 w-1.5 shrink-0 rounded-full ${connectionDotClass(connector)}`} />
                    {statusText(connector)}
                    {!connector.enabled && ' · off'}
                  </p>
                </div>
                {connector.type === 'mcp' && !connected ? (
                  <Button
                    tone="primary"
                    data-testid="connect-row"
                    onClick={(event) => { event.stopPropagation(); openExisting(connector); }}
                  >
                    Connect
                  </Button>
                ) : (
                  <Toggle
                    label={`${connector.enabled ? 'Turn off' : 'Turn on'} ${connector.label}`}
                    checked={connector.enabled}
                    onChange={(next) => {
                      void api.connectors.update(connector.id, { enabled: next }).then(load);
                    }}
                  />
                )}
              </Card>
            );
          })}
        </div>
      )}

      {browsing && (
        <BrowseCatalog
          catalog={catalog}
          onClose={() => setBrowsing(false)}
          onPick={(entry) => {
            setBrowsing(false);
            setDetail({ connectorId: entry.connectorId, catalogEntry: entry, type: 'mcp' });
          }}
        />
      )}

      {addingCustom && (
        <AddCustomConnector
          onClose={() => setAddingCustom(false)}
          onAdded={(connector) => {
            setAddingCustom(false);
            setTab(connector.type as Mechanism);
            void load();
            setDetail({ connectorId: connector.id, catalogEntry: null, type: connector.type as Mechanism });
          }}
        />
      )}

      {detail && (
        <ConnectorDetail
          target={detail}
          onClose={() => {
            setDetail(null);
            void load();
          }}
          onChanged={afterChange}
        />
      )}
    </>
  );
}

// ---------- Browse connectors ----------

function BrowseCatalog({ catalog, onClose, onPick }: {
  catalog: CatalogEntry[];
  onClose: () => void;
  onPick: (entry: CatalogEntry) => void;
}) {
  const [query, setQuery] = useState('');
  const matching = catalog.filter((entry) =>
    !query.trim() || entry.label.toLowerCase().includes(query.trim().toLowerCase())
    || entry.description.toLowerCase().includes(query.trim().toLowerCase()));

  return (
    <Modal open title="Browse connectors" onClose={onClose}>
      <div className="relative mb-3">
        <SearchIcon className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-ink-faint" />
        <input
          className={`${inputClass} pl-9`}
          placeholder="Search connectors…"
          data-testid="catalog-search"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
        />
      </div>
      <div className="space-y-2" data-testid="catalog-list">
        {matching.map((entry) => (
          <Card key={entry.id} interactive data-testid="catalog-row"
                className="flex items-center gap-3" onClick={() => onPick(entry)}>
            <AppIcon label={entry.label} iconDataUri={entry.iconDataUri} />
            <div className="min-w-0 flex-1">
              <p className="truncate text-[14px] text-ink">{entry.label}</p>
              <p className="mt-0.5 truncate text-[12px] text-ink-faint">{entry.description}</p>
            </div>
          </Card>
        ))}
        {matching.length === 0 && <p className="py-6 text-center text-[13px] text-ink-faint">No matches.</p>}
      </div>
    </Modal>
  );
}

// ---------- Add custom connector ----------

function AddCustomConnector({ onClose, onAdded }: {
  onClose: () => void;
  onAdded: (connector: Connector) => void;
}) {
  const [mechanism, setMechanism] = useState<Mechanism>('mcp');
  const [label, setLabel] = useState('');
  const [description, setDescription] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // mcp
  const [url, setUrl] = useState('');
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [clientId, setClientId] = useState('');
  const [clientSecret, setClientSecret] = useState('');
  // api
  const [baseUrl, setBaseUrl] = useState('');
  const [authKind, setAuthKind] = useState('bearer');
  const [authName, setAuthName] = useState('');
  const [apiKey, setApiKey] = useState('');
  // cli
  const [command, setCommand] = useState('');

  const ready = Boolean(label.trim())
    && (mechanism === 'mcp' ? Boolean(url.trim())
      : mechanism === 'api' ? Boolean(baseUrl.trim())
        : Boolean(command.trim()));

  async function add() {
    setBusy(true);
    setError(null);
    try {
      let config: Record<string, unknown>;
      if (mechanism === 'mcp') {
        const flow: Record<string, unknown> = { kind: clientId.trim() ? 'oauth_guided' : 'oauth_dcr', url: url.trim() };
        if (clientId.trim()) flow.clientId = clientId.trim();
        config = { connectFlow: flow };
      } else if (mechanism === 'api') {
        config = {
          baseUrl: baseUrl.trim(),
          auth: { kind: authKind, name: authName.trim() || undefined },
          operations: [],
        };
      } else {
        config = { command: command.trim(), commands: [] };
      }
      const created = await api.connectors.create({
        type: mechanism, label: label.trim(), description: description.trim() || undefined, config,
        apiKey: mechanism === 'api' && apiKey.trim() ? apiKey.trim() : undefined,
      });
      if (mechanism === 'mcp' && clientId.trim() && clientSecret.trim()) {
        // The secret has nowhere to land in a plain create call — a real
        // Connect attempt with the same fields is what actually files it,
        // via the guided-setup path this connector is already marked for.
        await api.connectors.connect(created.connector.id, {
          clientId: clientId.trim(), clientSecret: clientSecret.trim(),
        }).catch(() => undefined);
      }
      onAdded(created.connector);
    } catch (err) {
      setError(err instanceof ApiRequestError ? err.message : 'That did not work.');
      setBusy(false);
    }
  }

  return (
    <Modal
      open
      title="Add custom connector"
      onClose={onClose}
      footer={
        <Button tone="primary" data-testid="save-custom-connector" disabled={busy || !ready}
                onClick={() => void add()}>
          Add
        </Button>
      }
    >
      <div className="mb-3 inline-flex rounded-pill border border-surface-border p-0.5">
        {([['mcp', 'MCP server'], ['api', 'API key'], ['cli', 'CLI command']] as [Mechanism, string][]).map(
          ([id, mLabel]) => (
            <button
              key={id}
              type="button"
              data-testid={`mechanism-${id}`}
              aria-pressed={mechanism === id}
              onClick={() => setMechanism(id)}
              className={[
                'rounded-pill px-3 py-1.5 text-[13px] transition duration-150 ease-out',
                mechanism === id ? 'bg-accent/15 text-accent' : 'text-ink-muted hover:text-ink',
              ].join(' ')}
            >
              {mLabel}
            </button>
          ),
        )}
      </div>

      <Field label="Name">
        <input className={inputClass} data-testid="custom-label" placeholder="e.g. My internal server"
               value={label} onChange={(event) => setLabel(event.target.value)} />
      </Field>
      <Field label="What it's for (optional)">
        <input className={inputClass} data-testid="custom-description"
               value={description} onChange={(event) => setDescription(event.target.value)} />
      </Field>

      {mechanism === 'mcp' && (
        <>
          <Field label="Remote MCP server URL">
            <input className={inputClass} data-testid="custom-mcp-url" placeholder="https://example.com/mcp"
                   value={url} onChange={(event) => setUrl(event.target.value)} />
          </Field>
          <button type="button" data-testid="advanced-toggle"
                  className="mt-1 text-[12px] text-ink-faint hover:text-ink"
                  onClick={() => setShowAdvanced((was) => !was)}>
            {showAdvanced ? '▾' : '▸'} Advanced settings
          </button>
          {showAdvanced && (
            <div className="mt-1 rounded-lg border border-surface-border p-3">
              <p className="mb-2 text-[12px] text-ink-faint">
                Most servers register Jarvis automatically — these are only needed if this
                one doesn&apos;t.
              </p>
              <Field label="OAuth Client ID (optional)">
                <input className={inputClass} data-testid="custom-client-id"
                       value={clientId} onChange={(event) => setClientId(event.target.value)} />
              </Field>
              <Field label="OAuth Client Secret (optional)">
                <input className={inputClass} type="password" data-testid="custom-client-secret"
                       value={clientSecret} onChange={(event) => setClientSecret(event.target.value)} />
              </Field>
            </div>
          )}
        </>
      )}

      {mechanism === 'api' && (
        <>
          <Field label="Base web address">
            <input className={inputClass} data-testid="custom-base-url" placeholder="https://api.example.com"
                   value={baseUrl} onChange={(event) => setBaseUrl(event.target.value)} />
          </Field>
          <Field label="How the key is sent">
            <select className={inputClass} data-testid="custom-auth-kind"
                    value={authKind} onChange={(event) => setAuthKind(event.target.value)}>
              <option value="bearer">Authorization: Bearer header (most common)</option>
              <option value="header">A custom header</option>
              <option value="query">A web address parameter</option>
            </select>
          </Field>
          {authKind !== 'bearer' && (
            <Field label="Header or parameter name">
              <input className={inputClass} data-testid="custom-auth-name" placeholder='e.g. "X-API-Key"'
                     value={authName} onChange={(event) => setAuthName(event.target.value)} />
            </Field>
          )}
          <Field label="API key (optional — can add later)">
            <input className={inputClass} type="password" data-testid="custom-api-key"
                   value={apiKey} onChange={(event) => setApiKey(event.target.value)} />
          </Field>
        </>
      )}

      {mechanism === 'cli' && (
        <Field label="Command" hint="e.g. &quot;git&quot; or &quot;npm&quot;">
          <input className={inputClass} data-testid="custom-command" placeholder="git"
                 value={command} onChange={(event) => setCommand(event.target.value)} />
        </Field>
      )}

      {error && <p className="mt-2 text-[13px] text-state-danger">{error}</p>}
    </Modal>
  );
}

// ---------- Connector detail ----------

function ConnectorDetail({ target, onClose, onChanged }: {
  target: DetailTarget;
  onClose: () => void;
  onChanged: () => Promise<void>;
}) {
  const [connectorId, setConnectorId] = useState<string | null>(target.connectorId ?? null);
  const [connector, setConnector] = useState<Connector | null>(null);
  const [tools, setTools] = useState<ConnectorTool[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [toolsError, setToolsError] = useState<string | null>(null);
  const [confirmRemove, setConfirmRemove] = useState(false);
  const [confirmDisconnect, setConfirmDisconnect] = useState(false);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  // Reading an app's tools reaches its server, so it happens once on its own
  // per opening — never in a loop if the server keeps failing.
  const autoRefreshed = useRef(false);

  /** The server's view, always: what a tool is set to is never guessed here. */
  const reload = useCallback(async (id: string) => {
    const res = await api.connectors.open(id);
    setConnector(res.connector);
    setTools(res.tools);
    return res;
  }, []);

  const refreshTools = useCallback(async (id: string) => {
    setRefreshing(true);
    setToolsError(null);
    try {
      await api.connectors.refresh(id);
      await reload(id);
    } catch (err) {
      setToolsError(err instanceof ApiRequestError ? err.message : "Couldn't read this app's tools.");
    } finally {
      setRefreshing(false);
    }
  }, [reload]);

  useEffect(() => {
    let cancelled = false;
    async function run() {
      if (!connectorId) {
        setLoaded(true);
        return;
      }
      try {
        const res = await reload(connectorId);
        // Connected but nothing read yet: read it now rather than showing an
        // empty list the person can do nothing with.
        if (!cancelled && res.connector.type === 'mcp' && res.connector.status?.state === 'working'
            && res.tools.length === 0 && !autoRefreshed.current) {
          autoRefreshed.current = true;
          void refreshTools(connectorId);
        }
      } catch (err) {
        if (!cancelled) setError(err instanceof ApiRequestError ? err.message : 'Could not load this connector.');
      } finally {
        if (!cancelled) setLoaded(true);
      }
    }
    void run();
    return () => {
      cancelled = true;
    };
  }, [connectorId, reload, refreshTools]);

  useEffect(() => () => {
    if (pollRef.current) clearInterval(pollRef.current);
  }, []);

  const label = target.catalogEntry?.label || connector?.label || 'Connector';
  const description = target.catalogEntry?.description || connector?.description
    || `A custom ${target.type.toUpperCase()} connector.`;
  const isConfigured = connector?.status?.state === 'working';

  /** One save for any number of tools — a whole group is one request, so it
   *  can never end up half-changed. */
  async function setPermissions(map: Record<string, ToolPermission>) {
    if (!connector) return;
    setError(null);
    try {
      await api.connectors.update(connector.id, { toolPermissions: map });
    } catch {
      setError("Couldn't save that — try again.");
    }
    await reload(connector.id).catch(() => undefined);
  }

  /** Stay open after connecting and show what the app can do, rather than
   *  closing on the person the moment it works. */
  async function handleConnected(id: string) {
    autoRefreshed.current = true;
    setConnectorId(id);
    const res = await reload(id).catch(() => null);
    if (res && res.tools.length === 0) await refreshTools(id);
  }

  const toolsSection = (
    <ToolsSection
      tools={tools}
      onSetPermissions={setPermissions}
      refreshing={refreshing}
      toolsError={toolsError}
      note={connector?.status?.detail ?? null}
      onRefresh={connector?.type === 'mcp' ? () => void refreshTools(connector.id) : undefined}
    />
  );

  return (
    <Modal open title={label} onClose={onClose}>
      <p className="mb-4 text-[13px] text-ink-muted">{description}</p>

      {!loaded ? null : target.type === 'mcp' ? (
        <>
          {connector && (
            <div className="mb-4 flex items-center gap-2">
              {isConfigured && (
                <Button tone="quiet" data-testid="disconnect" onClick={() => setConfirmDisconnect(true)}>
                  Disconnect
                </Button>
              )}
              <Button tone="danger" data-testid="remove" onClick={() => setConfirmRemove(true)}>
                Remove
              </Button>
            </div>
          )}
          {isConfigured ? toolsSection : (
            <McpConnect
              connector={connector}
              catalogEntry={target.catalogEntry}
              label={label}
              onConnected={handleConnected}
              setPoll={(t) => { pollRef.current = t; }}
            />
          )}
        </>
      ) : (
        <>
          {connector && (
            <div className="mb-4">
              <Button tone="danger" data-testid="remove" onClick={() => setConfirmRemove(true)}>Remove</Button>
            </div>
          )}
          {target.type === 'api' && connector && (
            <ApiSetup connector={connector} onChanged={onChanged} />
          )}
          {target.type === 'cli' && (
            <p className="text-[13px] text-ink-faint">
              Jarvis runs this command&apos;s own <code>--help</code> to discover subcommands — that
              step isn&apos;t wired to this screen yet, so a CLI connector added here has no tools
              until one is added another way.
            </p>
          )}
          {tools.length > 0 && toolsSection}
        </>
      )}

      {error && <p className="mt-3 text-[13px] text-state-danger">{error}</p>}

      <Modal
        nested
        open={confirmRemove}
        title={`Remove ${label}?`}
        onClose={() => setConfirmRemove(false)}
        footer={
          <>
            <Button tone="danger" data-testid="confirm-remove" onClick={async () => {
              if (connector) await api.connectors.remove(connector.id);
              setConfirmRemove(false);
              await onChanged();
            }}>
              Remove it
            </Button>
            <Button onClick={() => setConfirmRemove(false)}>Keep it</Button>
          </>
        }
      >
        <p className="text-[14px] leading-relaxed text-ink">
          This deletes the connector and everything saved with it, including any sign-in.
        </p>
      </Modal>

      <Modal
        nested
        open={confirmDisconnect}
        title={`Disconnect ${label}?`}
        onClose={() => setConfirmDisconnect(false)}
        footer={
          <>
            <Button tone="danger" data-testid="confirm-disconnect" onClick={async () => {
              if (connector) await api.connectors.disconnect(connector.id);
              setConfirmDisconnect(false);
              await onChanged();
            }}>
              Disconnect
            </Button>
            <Button onClick={() => setConfirmDisconnect(false)}>Stay connected</Button>
          </>
        }
      >
        <p className="text-[14px] leading-relaxed text-ink">
          Signs out without deleting the connector — reconnecting after this needs no re-setup.
        </p>
      </Modal>
    </Modal>
  );
}

type Permission = ToolPermission;
const PERMISSIONS: Permission[] = ['allow', 'ask', 'deny'];
const PERMISSION_LABEL: Record<Permission, string> = {
  allow: 'Always allow', ask: 'Need approval', deny: 'Blocked',
};
const PERMISSION_ICON: Record<Permission, (p: { className?: string }) => React.ReactElement> = {
  allow: CheckIcon, ask: AskIcon, deny: BlockIcon,
};
const PERMISSION_ACTIVE: Record<Permission, string> = {
  allow: 'border-accent/40 bg-accent/15 text-accent',
  ask: 'border-state-warn/40 bg-state-warn/15 text-state-warn',
  deny: 'border-state-danger/40 bg-state-danger/15 text-state-danger',
};

/**
 * A tool an app exposes has no server-declared category — MCP's own wire
 * format is just a name, a description, and a schema — so this is a
 * deliberate, zero-cost, zero-model-call heuristic grouping by the verb in the
 * tool's own name, not a claim of real information architecture. Checks every
 * word, not just a leading one: a service that names its tools
 * "servicename-verb" (Notion's real tools do exactly this — "notion-search",
 * "notion-update-page") would otherwise land entirely in "Other".
 */
const GROUP_VERBS: [string, string[]][] = [
  ['Search & browse', ['search', 'list', 'find', 'query']],
  ['Read', ['read', 'get', 'fetch']],
  ['Create & edit', ['create', 'write', 'update', 'edit', 'set', 'upload']],
  ['Delete & move', ['delete', 'remove', 'move']],
];
const GROUP_ORDER = [...GROUP_VERBS.map(([group]) => group), 'Other'];

function groupFor(name: string): string {
  const words = name
    .replace(/([a-z0-9])([A-Z])/g, '$1 $2')
    .split(/[^a-zA-Z0-9]+/)
    .map((word) => word.toLowerCase())
    .filter(Boolean);
  for (const [group, verbs] of GROUP_VERBS) {
    if (words.some((word) => verbs.includes(word))) return group;
  }
  return 'Other';
}

function ToolsSection({ tools, onSetPermissions, refreshing, toolsError, note, onRefresh }: {
  tools: ConnectorTool[];
  onSetPermissions: (map: Record<string, Permission>) => void | Promise<void>;
  refreshing: boolean;
  toolsError: string | null;
  /** The connection's own status line — e.g. why reading its tools after
   *  connecting did not work — shown while there is nothing listed. */
  note?: string | null;
  /** Present for an app whose tools are read from its server (MCP). */
  onRefresh?: () => void;
}) {
  const groups = useMemo(() => {
    const byGroup = new Map<string, ConnectorTool[]>();
    for (const tool of tools) {
      // By the app's own tool name: the prefixed one also carries the
      // connector's label, whose words ("Search Console") are not a verb.
      const group = groupFor(tool.title);
      const list = byGroup.get(group);
      if (list) list.push(tool);
      else byGroup.set(group, [tool]);
    }
    return GROUP_ORDER.filter((group) => byGroup.has(group))
      .map((group) => [group, byGroup.get(group)!] as const);
  }, [tools]);

  const refreshButton = onRefresh && (
    <Button tone="quiet" data-testid="refresh-tools" disabled={refreshing} onClick={onRefresh}>
      {refreshing ? 'Reading tools…' : 'Refresh tools'}
    </Button>
  );

  if (tools.length === 0) {
    return (
      <div data-testid="tools-empty">
        <p className="mb-3 text-[13px] text-ink-faint">
          {refreshing
            ? "Reading this app's tools…"
            : onRefresh
              ? "Connected, but Jarvis hasn't read this app's tools yet."
              : 'Nothing set up yet.'}
        </p>
        {!refreshing && (toolsError ? (
          <p className="mb-3 text-[13px] text-state-danger" data-testid="tools-error">{toolsError}</p>
        ) : note && (
          <p className="mb-3 text-[13px] text-ink-muted" data-testid="tools-note">{note}</p>
        ))}
        {refreshButton}
      </div>
    );
  }

  return (
    <div>
      <div className="mb-3 flex items-center justify-between gap-2">
        <p className="text-[12px] font-medium text-ink-muted">Available tools</p>
        {refreshButton}
      </div>
      {toolsError && !refreshing && (
        <p className="mb-3 text-[13px] text-state-danger" data-testid="tools-error">{toolsError}</p>
      )}
      <div className="space-y-4">
        {groups.map(([group, groupTools]) => {
          const values = groupTools.map((tool) => tool.permission);
          const uniform = values.every((value) => value === values[0]) ? values[0] : null;
          return (
            <div key={group} data-testid="tool-group" data-group={group}>
              <div className="mb-1.5 flex items-center justify-between gap-2">
                <p className="flex items-center gap-2 whitespace-nowrap text-[11px] font-semibold uppercase tracking-[0.1em] text-ink-faint">
                  {group}
                  <span className="rounded-pill bg-white/[0.06] px-1.5 py-px text-[10px] normal-case tracking-normal text-ink-faint">
                    {groupTools.length}
                  </span>
                </p>
                <select
                  className={`${inputClass} !w-auto shrink-0 py-1 text-[12px]`}
                  data-testid="group-permission"
                  value={uniform ?? 'custom'}
                  onChange={(event) => {
                    const next = event.target.value as Permission;
                    void onSetPermissions(Object.fromEntries(groupTools.map((tool) => [tool.name, next])));
                  }}
                >
                  <option value="allow">✓ Always allow</option>
                  <option value="ask">Need approval</option>
                  <option value="deny">Blocked</option>
                  {!uniform && <option value="custom" disabled>Custom</option>}
                </select>
              </div>
              <div className="space-y-1">
                {groupTools.map((tool) => {
                  const current = tool.permission;
                  return (
                    <div key={tool.name} className="flex items-center justify-between gap-3 py-1.5"
                         data-testid="tool-row" data-tool={tool.title} data-permission={current}>
                      <div className="min-w-0 flex-1">
                        <p className="truncate text-[13px] text-ink" title={tool.description}>{tool.title}</p>
                      </div>
                      <div className="flex shrink-0 items-center gap-1">
                        {PERMISSIONS.map((permission) => {
                          const Icon = PERMISSION_ICON[permission];
                          return (
                            <button
                              key={permission}
                              type="button"
                              title={PERMISSION_LABEL[permission]}
                              aria-label={`${PERMISSION_LABEL[permission]} for ${tool.title}`}
                              aria-pressed={current === permission}
                              data-testid={`tool-permission-${permission}`}
                              onClick={() => {
                                if (permission !== current) void onSetPermissions({ [tool.name]: permission });
                              }}
                              className={[
                                'flex h-6 w-6 items-center justify-center rounded-full border transition duration-150 ease-out',
                                current === permission
                                  ? PERMISSION_ACTIVE[permission]
                                  : 'border-surface-border text-ink-faint hover:text-ink',
                              ].join(' ')}
                            >
                              <Icon className="h-3.5 w-3.5" />
                            </button>
                          );
                        })}
                      </div>
                    </div>
                  );
                })}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function McpConnect({ connector, catalogEntry, label, onConnected, setPoll }: {
  connector: Connector | null;
  catalogEntry: CatalogEntry | null;
  label: string;
  onConnected: (connectorId: string) => Promise<void>;
  setPoll: (timer: ReturnType<typeof setInterval> | null) => void;
}) {
  const wasErrored = connector?.status?.state === 'error';
  const manualHint = (connector?.config as { connectFlow?: { manualClient?: { message?: string } } })
    ?.connectFlow?.manualClient ?? null;

  const [status, setStatus] = useState<string>(
    manualHint?.message ? manualHint.message
      : wasErrored ? (connector?.status?.detail || 'Could not connect.')
        : `You are not connected to ${label} yet.`,
  );
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [clientId, setClientId] = useState('');
  const [clientSecret, setClientSecret] = useState('');
  const [busy, setBusy] = useState(false);
  const [buttonLabel, setButtonLabel] = useState(wasErrored ? 'Reconnect' : 'Connect');

  async function connect() {
    setBusy(true);
    try {
      let connectorId = connector?.id;
      if (!connectorId && catalogEntry) {
        connectorId = (await api.connectors.ensure(catalogEntry.id)).connectorId;
      }
      if (!connectorId) throw new Error('Nothing to connect.');

      const result = await api.connectors.connect(connectorId, {
        clientId: clientId.trim() || undefined,
        clientSecret: clientSecret.trim() || undefined,
      });

      if (result.needsManualClient) {
        setStatus(result.message || 'This service needs a Client ID before Jarvis can connect.');
        setBusy(false);
        return;
      }
      if (result.noAuthNeeded) {
        await onConnected(connectorId);
        return;
      }
      if (result.authUrl) {
        window.open(result.authUrl, '_blank', 'noopener');
        setStatus(`Waiting for you to finish signing in to ${label} in the new tab…`);
        setButtonLabel('Waiting…');
        const timer = setInterval(async () => {
          try {
            const res = await api.connectors.open(connectorId!);
            if (res.connector.status?.state === 'working') {
              clearInterval(timer);
              await onConnected(connectorId!);
            } else if (res.connector.status?.state === 'error') {
              clearInterval(timer);
              setStatus(`Could not connect: ${res.connector.status.detail || 'unknown error'}`);
              setBusy(false);
              setButtonLabel('Try again');
            }
          } catch {
            // a transient read failure here just waits for the next tick
          }
        }, 2000);
        setPoll(timer);
      }
    } catch (err) {
      setStatus(err instanceof ApiRequestError ? err.message : 'Could not start connecting that service.');
      setBusy(false);
      setButtonLabel('Reconnect');
    }
  }

  return (
    <div>
      <p className="mb-3 text-[13px] text-ink-muted">{status}</p>
      <button type="button" data-testid="advanced-toggle"
              className="mb-2 text-[12px] text-ink-faint hover:text-ink"
              onClick={() => setShowAdvanced((was) => !was)}>
        {showAdvanced ? '▾' : '▸'} Advanced settings
      </button>
      {showAdvanced && (
        <div className="mb-3 rounded-lg border border-surface-border p-3">
          <Field label="OAuth Client ID (optional)">
            <input className={inputClass} data-testid="connect-client-id"
                   value={clientId} onChange={(event) => setClientId(event.target.value)} />
          </Field>
          <Field label="OAuth Client Secret (optional)">
            <input className={inputClass} type="password" data-testid="connect-client-secret"
                   value={clientSecret} onChange={(event) => setClientSecret(event.target.value)} />
          </Field>
        </div>
      )}
      <Button tone="primary" data-testid="connect" disabled={busy} onClick={() => void connect()}>
        {buttonLabel}
      </Button>
    </div>
  );
}

function ApiSetup({ connector, onChanged }: { connector: Connector; onChanged: () => Promise<void> }) {
  const [specUrl, setSpecUrl] = useState('');
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  async function discover() {
    if (!specUrl.trim()) return;
    setBusy(true);
    setMessage('Reading that document…');
    try {
      // The refresh route runs discover_from_spec() server-side whenever a
      // specUrl is present in config — set it, then ask for a refresh.
      await api.connectors.update(connector.id, { config: { specUrl: specUrl.trim() } });
      await api.connectors.refresh(connector.id);
      await onChanged();
    } catch (err) {
      setMessage(err instanceof ApiRequestError ? err.message : "Couldn't read that document.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="mb-4">
      <Field label="OpenAPI/Swagger document (optional)" hint="Jarvis reads it and proposes operations.">
        <div className="flex gap-2">
          <input className={inputClass} placeholder="https://api.example.com/openapi.json"
                 data-testid="spec-url" value={specUrl} onChange={(event) => setSpecUrl(event.target.value)} />
          <Button disabled={busy || !specUrl.trim()} onClick={() => void discover()}>Discover</Button>
        </div>
      </Field>
      {message && <p className="text-[12px] text-ink-faint">{message}</p>}
    </div>
  );
}

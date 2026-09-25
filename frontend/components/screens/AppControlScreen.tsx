'use client';

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { AppIcon } from '@/components/ui/AppIcon';
import { Button } from '@/components/ui/Button';
import { Card } from '@/components/ui/Card';
import { EmptyState } from '@/components/ui/EmptyState';
import { Field, inputClass } from '@/components/ui/Field';
import { AskIcon, BlockIcon, CheckIcon, CloseIcon, PlusIcon, SearchIcon } from '@/components/ui/Icons';
import { Modal } from '@/components/ui/Modal';
import { Popover } from '@/components/ui/Popover';
import { Toggle } from '@/components/ui/Toggle';
import { api, ApiRequestError } from '@/lib/api';
import type {
  ApiOperation, CatalogEntry, CliCommand, Connector, ConnectorSetup, ConnectorTool, ToolPermission,
} from '@/lib/api-types';

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
            setDetail({ connectorId: entry.connectorId, catalogEntry: entry, type: entry.type ?? 'mcp' });
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
              <p className="flex items-center gap-2 truncate text-[14px] text-ink">
                {entry.label}
                {entry.type && entry.type !== 'mcp' && (
                  <span data-testid="catalog-type"
                        className="rounded-pill bg-white/[0.06] px-1.5 py-px text-[10px] uppercase tracking-wide text-ink-faint">
                    {entry.type === 'api' ? 'API' : 'CLI'}
                  </span>
                )}
              </p>
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
  const [showApiAdvanced, setShowApiAdvanced] = useState(false);
  const [authPrefix, setAuthPrefix] = useState('');
  const [testPath, setTestPath] = useState('');
  const [testOk, setTestOk] = useState('');
  const [checkField, setCheckField] = useState('');
  const [checkOk, setCheckOk] = useState('');
  const [checkMessage, setCheckMessage] = useState('');
  // cli
  const [command, setCommand] = useState('');
  const [cliLogin, setCliLogin] = useState('');
  const [cliTest, setCliTest] = useState('');

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
          auth: { kind: authKind, name: authName.trim() || undefined,
                  prefix: authKind === 'bearer' && authPrefix.trim() ? authPrefix.trim() : undefined },
          operations: [],
        };
        if (testPath.trim()) {
          const okStatus = testOk.split(',').map((v) => Number(v.trim())).filter((n) => n > 0);
          config.test = { method: 'GET', path: testPath.trim(), ...(okStatus.length ? { okStatus } : {}) };
        }
        if (checkField.trim()) {
          const ok = checkOk.split(',').map((v) => v.trim()).filter(Boolean)
            .map((v) => (/^-?\d+$/.test(v) ? Number(v) : v));
          config.responseCheck = { field: checkField.trim(), ok,
                                   ...(checkMessage.trim() ? { message: checkMessage.trim() } : {}) };
        }
      } else {
        config = { command: command.trim(), commands: [] };
        const words = (text: string) => text.trim().split(/\s+/).filter(Boolean);
        if (cliLogin.trim()) config.login = words(cliLogin);
        if (cliTest.trim()) config.test = words(cliTest);
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
          <button type="button" data-testid="api-advanced-toggle"
                  className="mb-2 text-[12px] text-ink-faint hover:text-ink"
                  onClick={() => setShowApiAdvanced((was) => !was)}>
            {showApiAdvanced ? '▾' : '▸'} Advanced settings
          </button>
          {showApiAdvanced && (
            <div className="mb-3 rounded-lg border border-surface-border p-3">
              {authKind === 'bearer' && (
                <Field label="Word before the key (optional)" hint='Most services use "Bearer"; some use another word, e.g. "Key".'>
                  <input className={inputClass} data-testid="custom-auth-prefix" placeholder="Bearer"
                         value={authPrefix} onChange={(event) => setAuthPrefix(event.target.value)} />
                </Field>
              )}
              <Field label="Connection test path (optional)" hint="A cheap endpoint Jarvis calls to check the key, e.g. /v1/me.">
                <input className={inputClass} data-testid="custom-test-path" placeholder="/v1/me"
                       value={testPath} onChange={(event) => setTestPath(event.target.value)} />
              </Field>
              <Field label="Answers that mean the key works (optional)" hint="Comma-separated status codes. Default: any success.">
                <input className={inputClass} data-testid="custom-test-ok" placeholder="200"
                       value={testOk} onChange={(event) => setTestOk(event.target.value)} />
              </Field>
              <Field label="Success field in every reply (optional)"
                     hint="For services that always answer 200 and put the real result in the body, e.g. code.">
                <input className={inputClass} data-testid="custom-check-field" placeholder="code"
                       value={checkField} onChange={(event) => setCheckField(event.target.value)} />
              </Field>
              <Field label="Value(s) that mean success">
                <input className={inputClass} data-testid="custom-check-ok" placeholder="200"
                       value={checkOk} onChange={(event) => setCheckOk(event.target.value)} />
              </Field>
              <Field label="Field holding the error message (optional)">
                <input className={inputClass} data-testid="custom-check-message" placeholder="msg"
                       value={checkMessage} onChange={(event) => setCheckMessage(event.target.value)} />
              </Field>
            </div>
          )}
        </>
      )}

      {mechanism === 'cli' && (
        <>
          <Field label="Command" hint="e.g. &quot;git&quot; or &quot;npm&quot;">
            <input className={inputClass} data-testid="custom-command" placeholder="git"
                   value={command} onChange={(event) => setCommand(event.target.value)} />
          </Field>
          <Field label="Its sign-in command (optional)" hint='What comes after the program name, e.g. "auth login".'>
            <input className={inputClass} data-testid="custom-cli-login" placeholder="auth login"
                   value={cliLogin} onChange={(event) => setCliLogin(event.target.value)} />
          </Field>
          <Field label="A command that proves it works (optional)" hint='e.g. "auth status". Default: --version.'>
            <input className={inputClass} data-testid="custom-cli-test" placeholder="auth status"
                   value={cliTest} onChange={(event) => setCliTest(event.target.value)} />
          </Field>
        </>
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
  const [setup, setSetup] = useState<ConnectorSetup | null>(null);
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
    setSetup(res.setup ?? null);
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
      if (!connectorId && target.catalogEntry && target.type !== 'mcp') {
        // An API or CLI entry opened for the first time: it becomes an ordinary
        // connector of that type, pre-filled from its definition.
        try {
          const made = await api.connectors.ensure(target.catalogEntry.id);
          if (!cancelled) setConnectorId(made.connectorId);
        } catch (err) {
          if (!cancelled) {
            setError(err instanceof ApiRequestError ? err.message : 'Could not set that up.');
            setLoaded(true);
          }
        }
        return;
      }
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
  }, [connectorId, reload, refreshTools, target.catalogEntry, target.type]);

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

  /** Take one tool off an API or CLI connector — its definition goes, so the
   *  tool goes from Jarvis too. */
  async function removeTool(tool: ConnectorTool) {
    if (!connector || !setup) return;
    setError(null);
    try {
      if (connector.type === 'api') {
        await api.connectors.update(connector.id, { config: {
          operations: (setup.operations ?? []).filter((op) => op.name !== tool.title) } });
      } else {
        await api.connectors.update(connector.id, { config: {
          commands: (setup.commands ?? []).filter((c) => c.name !== tool.title) } });
      }
    } catch (err) {
      setError(err instanceof ApiRequestError ? err.message : "Couldn't remove that.");
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
          {connector && setup && (target.type === 'api' ? (
            <ApiConnection connector={connector} setup={setup}
                           onChanged={() => reload(connector.id).then(() => undefined)} />
          ) : (
            <CliConnection connector={connector} setup={setup}
                           onChanged={() => reload(connector.id).then(() => undefined)} />
          ))}
          {connector && setup && (
            <ToolsSection
              tools={tools}
              onSetPermissions={setPermissions}
              refreshing={false}
              toolsError={null}
              onRemove={(tool) => void removeTool(tool)}
            />
          )}
          {connector && setup && (target.type === 'api' ? (
            <ApiToolEditor connector={connector} setup={setup}
                           onChanged={() => reload(connector.id).then(() => undefined)} />
          ) : (
            <CliToolEditor connector={connector} setup={setup}
                           onChanged={() => reload(connector.id).then(() => undefined)} />
          ))}
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

function ToolsSection({ tools, onSetPermissions, refreshing, toolsError, note, onRefresh, onRemove }: {
  tools: ConnectorTool[];
  onSetPermissions: (map: Record<string, Permission>) => void | Promise<void>;
  refreshing: boolean;
  toolsError: string | null;
  /** API and CLI connectors: their tools are definitions the person can remove. */
  onRemove?: (tool: ConnectorTool) => void;
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
                        {onRemove && (
                          <button type="button" data-testid="remove-tool"
                                  aria-label={`Remove ${tool.title}`} title="Remove this tool"
                                  onClick={() => onRemove(tool)}
                                  className="ml-1 flex h-6 w-6 items-center justify-center rounded-full border border-surface-border text-ink-faint transition duration-150 ease-out hover:text-state-danger">
                            <CloseIcon className="h-3 w-3" />
                          </button>
                        )}
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

// ---------- API and CLI connectors: connection ----------

/** The connection's real state, as the server last checked it. */
function ConnectionStatus({ connector }: { connector: Connector }) {
  const state = connector.status?.state;
  const detail = connector.status?.detail;
  const text = state === 'working' ? 'Connected'
    : state === 'error' ? (detail || 'Not connected')
      : 'Not checked yet';
  const tone = state === 'working' ? 'bg-state-ok' : state === 'error' ? 'bg-state-danger' : 'bg-ink-faint';
  return (
    <div className="mb-2 flex items-start gap-2" data-testid="connection-status" data-state={state ?? 'untested'}>
      <span aria-hidden className={`mt-1.5 h-2 w-2 shrink-0 rounded-full ${tone}`} />
      <div className="min-w-0">
        <p className="text-[13px] text-ink">{text}</p>
        {state === 'working' && detail && <p className="text-[12px] text-ink-faint">{detail}</p>}
      </div>
    </div>
  );
}

function ApiConnection({ connector, setup, onChanged }: {
  connector: Connector;
  setup: ConnectorSetup;
  onChanged: () => Promise<void>;
}) {
  const [key, setKey] = useState('');
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  async function run(action: () => Promise<{ connected: boolean; detail: string | null }>) {
    setBusy(true);
    setMessage(null);
    try {
      const res = await action();
      setMessage(res.connected ? 'Connected — the service accepted the key.'
        : (res.detail || 'The service did not accept that.'));
    } catch (err) {
      setMessage(err instanceof ApiRequestError ? err.message : 'That did not work.');
    } finally {
      setBusy(false);
      await onChanged();
    }
  }

  const keyLabel = setup.keyLabel || 'API key';
  return (
    <section className="mb-4 rounded-lg border border-surface-border p-3" data-testid="api-connection">
      <ConnectionStatus connector={connector} />
      {setup.baseUrl && <p className="mb-2 truncate text-[12px] text-ink-faint">Address: {setup.baseUrl}</p>}
      <Field label={setup.hasSecret ? `Replace the ${keyLabel}` : keyLabel}
             hint={setup.keyHint || 'Saved on this computer only. Jarvis never shows it again.'}>
        <div className="flex gap-2">
          <input className={inputClass} type="password" data-testid="api-key-input" autoComplete="off"
                 placeholder={setup.hasSecret ? 'A key is saved' : 'Paste the key'}
                 value={key} onChange={(event) => setKey(event.target.value)} />
          <Button tone="primary" data-testid="save-api-key" disabled={busy || !key.trim()}
                  onClick={() => void run(async () => {
                    const res = await api.connectors.saveKey(connector.id, key.trim());
                    setKey('');
                    return res;
                  })}>
            Save
          </Button>
        </div>
      </Field>
      <Button tone="quiet" data-testid="test-connection" disabled={busy}
              onClick={() => void run(() => api.connectors.test(connector.id))}>
        {busy ? 'Checking…' : 'Test connection'}
      </Button>
      {message && <p className="mt-2 text-[12px] text-ink-muted" data-testid="connection-message">{message}</p>}
    </section>
  );
}

function CliConnection({ connector, setup, onChanged }: {
  connector: Connector;
  setup: ConnectorSetup;
  onChanged: () => Promise<void>;
}) {
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [showKey, setShowKey] = useState(false);
  const [envName, setEnvName] = useState('');
  const [envValue, setEnvValue] = useState('');
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  useEffect(() => () => { if (pollRef.current) clearInterval(pollRef.current); }, []);

  const notInstalled = connector.status?.state === 'error'
    && (connector.status?.detail || '').includes('is not installed');

  async function test() {
    setBusy(true);
    setMessage(null);
    try {
      const res = await api.connectors.test(connector.id);
      setMessage(res.connected ? 'Connected.' : (res.detail || 'Not connected.'));
    } catch (err) {
      setMessage(err instanceof ApiRequestError ? err.message : 'That did not work.');
    } finally {
      setBusy(false);
      await onChanged();
    }
  }

  async function signIn() {
    setBusy(true);
    setMessage(null);
    try {
      await api.connectors.login(connector.id);
      setMessage('Finish signing in in the browser window that just opened…');
      const started = Date.now();
      if (pollRef.current) clearInterval(pollRef.current);
      pollRef.current = setInterval(async () => {
        try {
          const res = await api.connectors.test(connector.id);
          if (res.connected || Date.now() - started > 180_000) {
            if (pollRef.current) clearInterval(pollRef.current);
            setMessage(res.connected ? 'Signed in and connected.'
              : 'Sign-in did not finish. Try Sign in again when you are ready.');
            setBusy(false);
            await onChanged();
          }
        } catch {
          // a transient check failure just waits for the next tick
        }
      }, 3000);
    } catch (err) {
      setMessage(err instanceof ApiRequestError ? err.message : 'Could not start signing in.');
      setBusy(false);
    }
  }

  async function saveEnv() {
    setBusy(true);
    setMessage(null);
    try {
      const res = await api.connectors.envSecret(connector.id, envName.trim(), envValue);
      setEnvValue('');
      setMessage(res.connected ? 'Saved. Connected.' : `Saved. ${res.detail || ''}`.trim());
    } catch (err) {
      setMessage(err instanceof ApiRequestError ? err.message : "Couldn't save that.");
    } finally {
      setBusy(false);
      await onChanged();
    }
  }

  return (
    <section className="mb-4 rounded-lg border border-surface-border p-3" data-testid="cli-connection">
      <ConnectionStatus connector={connector} />
      <p className="mb-2 text-[12px] text-ink-faint">Program: <code>{setup.command}</code></p>
      {notInstalled && setup.install && (
        <p className="mb-2 text-[12px] text-ink-muted" data-testid="cli-install-hint">
          Install it first, then Test connection: <code>{setup.install}</code>
        </p>
      )}
      <div className="flex flex-wrap gap-2">
        {setup.canSignIn && (
          <Button tone="primary" data-testid="cli-sign-in" disabled={busy || notInstalled}
                  onClick={() => void signIn()}>
            Sign in
          </Button>
        )}
        <Button tone="quiet" data-testid="test-connection" disabled={busy} onClick={() => void test()}>
          Test connection
        </Button>
        <Button tone="quiet" data-testid="cli-key-toggle" onClick={() => setShowKey((was) => !was)}>
          {showKey ? 'Hide key setting' : 'Give it a key'}
        </Button>
      </div>
      {(setup.envNames ?? []).length > 0 && (
        <p className="mt-2 text-[12px] text-ink-faint">Keys given to this program: {(setup.envNames ?? []).join(', ')}</p>
      )}
      {showKey && (
        <div className="mt-3 rounded-lg border border-surface-border p-3">
          <p className="mb-2 text-[12px] text-ink-faint">
            For a program that reads its key from an environment variable. Only this program gets it.
          </p>
          <Field label="Variable name">
            <input className={inputClass} data-testid="cli-env-name" placeholder="e.g. SERVICE_API_KEY"
                   value={envName} onChange={(event) => setEnvName(event.target.value)} />
          </Field>
          <Field label="Value (leave empty to remove)">
            <input className={inputClass} type="password" data-testid="cli-env-value" autoComplete="off"
                   value={envValue} onChange={(event) => setEnvValue(event.target.value)} />
          </Field>
          <Button tone="primary" data-testid="cli-env-save" disabled={busy || !envName.trim()}
                  onClick={() => void saveEnv()}>
            Save
          </Button>
        </div>
      )}
      {message && <p className="mt-2 text-[12px] text-ink-muted" data-testid="connection-message">{message}</p>}
    </section>
  );
}

// ---------- API and CLI connectors: where their tools come from ----------

function splitList(text: string): string[] {
  return text.split(',').map((part) => part.trim()).filter(Boolean);
}

function ApiToolEditor({ connector, setup, onChanged }: {
  connector: Connector;
  setup: ConnectorSetup;
  onChanged: () => Promise<void>;
}) {
  const [panel, setPanel] = useState<'none' | 'add' | 'import'>('none');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // add endpoint
  const [name, setName] = useState('');
  const [method, setMethod] = useState('GET');
  const [path, setPath] = useState('');
  const [description, setDescription] = useState('');
  const [queryParams, setQueryParams] = useState('');
  const [hasBody, setHasBody] = useState(false);
  const [isJob, setIsJob] = useState(false);
  const [idFrom, setIdFrom] = useState('');
  const [statusPath, setStatusPath] = useState('');
  const [idQuery, setIdQuery] = useState('');
  const [statusField, setStatusField] = useState('');
  const [doneValues, setDoneValues] = useState('');
  const [failedValues, setFailedValues] = useState('');
  // import
  const [specUrl, setSpecUrl] = useState('');
  const [proposed, setProposed] = useState<ApiOperation[] | null>(null);
  const [proposedAuth, setProposedAuth] = useState<ConnectorSetup['auth'] | null>(null);
  const [chosen, setChosen] = useState<Set<string>>(new Set());

  const existing = setup.operations ?? [];

  async function save(operations: ApiOperation[], extra: Record<string, unknown> = {}) {
    setBusy(true);
    setError(null);
    try {
      await api.connectors.update(connector.id, { config: { operations, ...extra } });
      await onChanged();
      return true;
    } catch (err) {
      setError(err instanceof ApiRequestError ? err.message : "Couldn't save that.");
      return false;
    } finally {
      setBusy(false);
    }
  }

  async function addEndpoint() {
    const properties: Record<string, unknown> = {};
    for (const param of splitList(queryParams)) properties[param] = { type: 'string' };
    if (hasBody) properties.body = { type: 'object', description: 'The request body, as JSON.' };
    const operation: ApiOperation = {
      name: name.trim(), method, path: path.trim(), description: description.trim(),
      parameters: { type: 'object', properties, required: [] },
    };
    if (isJob) {
      operation.wait = {
        idFrom: idFrom.trim(), path: statusPath.trim(), statusField: statusField.trim(),
        done: splitList(doneValues), failed: splitList(failedValues),
        ...(idQuery.trim() ? { query: { [idQuery.trim()]: '{id}' } } : {}),
      };
    }
    const kept = existing.filter((op) => op.name !== operation.name);
    if (await save([...kept, operation])) {
      setName(''); setPath(''); setDescription(''); setQueryParams(''); setHasBody(false); setIsJob(false);
      setPanel('none');
    }
  }

  async function readSpec() {
    setBusy(true);
    setError(null);
    setProposed(null);
    try {
      const res = await api.connectors.importOpenapi(connector.id, specUrl.trim());
      setProposed(res.operations);
      setProposedAuth(res.auth ?? null);
      setChosen(new Set(res.operations.map((op) => op.name)));
    } catch (err) {
      setError(err instanceof ApiRequestError ? err.message : "Couldn't read that document.");
    } finally {
      setBusy(false);
    }
  }

  async function addChosen() {
    const picked = (proposed ?? []).filter((op) => chosen.has(op.name));
    const names = new Set(picked.map((op) => op.name));
    const extra: Record<string, unknown> = {};
    // The spec says how its key is sent; take that unless one was already set by hand.
    if (proposedAuth && !(setup.auth?.name || setup.auth?.prefix)) extra.auth = proposedAuth;
    if (await save([...existing.filter((op) => !names.has(op.name)), ...picked], extra)) {
      setProposed(null);
      setPanel('none');
    }
  }

  return (
    <section className="mt-4" data-testid="api-tool-editor">
      <div className="flex flex-wrap gap-2">
        <Button tone="quiet" data-testid="add-endpoint-toggle" onClick={() => setPanel(panel === 'add' ? 'none' : 'add')}>
          Add endpoint
        </Button>
        <Button tone="quiet" data-testid="import-openapi-toggle" onClick={() => setPanel(panel === 'import' ? 'none' : 'import')}>
          Import from OpenAPI
        </Button>
      </div>

      {panel === 'add' && (
        <div className="mt-3 rounded-lg border border-surface-border p-3" data-testid="add-endpoint-form">
          <Field label="Name">
            <input className={inputClass} data-testid="endpoint-name" placeholder="e.g. get_credits"
                   value={name} onChange={(event) => setName(event.target.value)} />
          </Field>
          <div className="flex gap-2">
            <Field label="Method">
              <select className={`${inputClass} !w-auto`} data-testid="endpoint-method" value={method}
                      onChange={(event) => setMethod(event.target.value)}>
                {['GET', 'POST', 'PUT', 'PATCH', 'DELETE'].map((m) => <option key={m} value={m}>{m}</option>)}
              </select>
            </Field>
            <div className="min-w-0 flex-1">
              <Field label="Path" hint="Starts with /. Use {name} for a value in the path.">
                <input className={inputClass} data-testid="endpoint-path" placeholder="/v1/items/{itemId}"
                       value={path} onChange={(event) => setPath(event.target.value)} />
              </Field>
            </div>
          </div>
          <Field label="What it does (Jarvis reads this)">
            <input className={inputClass} data-testid="endpoint-description"
                   value={description} onChange={(event) => setDescription(event.target.value)} />
          </Field>
          <Field label="Query parameters (optional, comma-separated)">
            <input className={inputClass} data-testid="endpoint-query" placeholder="e.g. limit, taskId"
                   value={queryParams} onChange={(event) => setQueryParams(event.target.value)} />
          </Field>
          <label className="mb-2 flex items-center gap-2 text-[13px] text-ink-muted">
            <input type="checkbox" data-testid="endpoint-has-body" checked={hasBody}
                   onChange={(event) => setHasBody(event.target.checked)} />
            Sends a JSON body
          </label>
          <label className="mb-2 flex items-center gap-2 text-[13px] text-ink-muted">
            <input type="checkbox" data-testid="endpoint-is-job" checked={isJob}
                   onChange={(event) => setIsJob(event.target.checked)} />
            Starts a background job — wait for it to finish
          </label>
          {isJob && (
            <div className="mb-2 rounded-lg border border-surface-border p-3">
              <Field label="Where the job id is in the reply" hint="e.g. data.taskId">
                <input className={inputClass} data-testid="job-id-from" value={idFrom}
                       onChange={(event) => setIdFrom(event.target.value)} />
              </Field>
              <Field label="Endpoint that reports progress" hint="Use {id} for the job id in the path, or name a query parameter below.">
                <input className={inputClass} data-testid="job-status-path" placeholder="/v1/jobs/status"
                       value={statusPath} onChange={(event) => setStatusPath(event.target.value)} />
              </Field>
              <Field label="Query parameter that takes the job id (optional)">
                <input className={inputClass} data-testid="job-id-query" placeholder="e.g. taskId"
                       value={idQuery} onChange={(event) => setIdQuery(event.target.value)} />
              </Field>
              <Field label="Where the status is in the progress reply" hint="e.g. data.state">
                <input className={inputClass} data-testid="job-status-field" value={statusField}
                       onChange={(event) => setStatusField(event.target.value)} />
              </Field>
              <Field label="Status values that mean finished (comma-separated)">
                <input className={inputClass} data-testid="job-done" placeholder="e.g. success"
                       value={doneValues} onChange={(event) => setDoneValues(event.target.value)} />
              </Field>
              <Field label="Status values that mean it failed (comma-separated)">
                <input className={inputClass} data-testid="job-failed" placeholder="e.g. fail"
                       value={failedValues} onChange={(event) => setFailedValues(event.target.value)} />
              </Field>
            </div>
          )}
          <Button tone="primary" data-testid="save-endpoint" disabled={busy || !name.trim() || !path.trim()}
                  onClick={() => void addEndpoint()}>
            Add endpoint
          </Button>
        </div>
      )}

      {panel === 'import' && (
        <div className="mt-3 rounded-lg border border-surface-border p-3" data-testid="import-openapi-form">
          <Field label="OpenAPI document address (JSON or YAML)">
            <div className="flex gap-2">
              <input className={inputClass} data-testid="spec-url" placeholder="https://api.example.com/openapi.json"
                     value={specUrl} onChange={(event) => setSpecUrl(event.target.value)} />
              <Button disabled={busy || !specUrl.trim()} data-testid="read-spec" onClick={() => void readSpec()}>
                {busy ? 'Reading…' : 'Read'}
              </Button>
            </div>
          </Field>
          {proposed && (
            <div data-testid="proposed-endpoints">
              <p className="mb-2 text-[12px] text-ink-faint">
                Found {proposed.length}. Tick the ones Jarvis should have — each starts on Need approval.
              </p>
              <div className="max-h-64 space-y-1 overflow-y-auto">
                {proposed.map((op) => (
                  <label key={op.name} className="flex items-start gap-2 text-[13px] text-ink">
                    <input type="checkbox" data-testid="proposed-endpoint" data-name={op.name}
                           checked={chosen.has(op.name)}
                           onChange={(event) => setChosen((was) => {
                             const next = new Set(was);
                             if (event.target.checked) next.add(op.name); else next.delete(op.name);
                             return next;
                           })} />
                    <span className="min-w-0">
                      <span className="font-medium">{op.name}</span>
                      <span className="ml-2 text-[11px] text-ink-faint">{op.method} {op.path}</span>
                    </span>
                  </label>
                ))}
              </div>
              <Button tone="primary" data-testid="add-chosen-endpoints" className="mt-3"
                      disabled={busy || chosen.size === 0} onClick={() => void addChosen()}>
                Add {chosen.size} endpoint{chosen.size === 1 ? '' : 's'}
              </Button>
            </div>
          )}
        </div>
      )}
      {error && <p className="mt-2 text-[13px] text-state-danger" data-testid="tool-editor-error">{error}</p>}
    </section>
  );
}

function CliToolEditor({ connector, setup, onChanged }: {
  connector: Connector;
  setup: ConnectorSetup;
  onChanged: () => Promise<void>;
}) {
  const [panel, setPanel] = useState<'none' | 'add'>('none');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [words, setWords] = useState('');
  const [proposed, setProposed] = useState<CliCommand[] | null>(null);
  const [chosen, setChosen] = useState<Set<string>>(new Set());

  const existing = setup.commands ?? [];

  async function save(commands: CliCommand[]) {
    setBusy(true);
    setError(null);
    try {
      await api.connectors.update(connector.id, { config: { commands } });
      await onChanged();
      return true;
    } catch (err) {
      setError(err instanceof ApiRequestError ? err.message : "Couldn't save that.");
      return false;
    } finally {
      setBusy(false);
    }
  }

  async function discover() {
    setBusy(true);
    setError(null);
    setProposed(null);
    try {
      const res = await api.connectors.discoverCommands(connector.id);
      setProposed(res.proposed);
      setChosen(new Set(res.proposed.map((c) => c.name)));
    } catch (err) {
      setError(err instanceof ApiRequestError ? err.message : "Couldn't read its help.");
    } finally {
      setBusy(false);
    }
  }

  async function addChosen() {
    const picked = (proposed ?? []).filter((c) => chosen.has(c.name));
    const names = new Set(picked.map((c) => c.name));
    if (await save([...existing.filter((c) => !names.has(c.name)), ...picked])) setProposed(null);
  }

  async function addCommand() {
    const argv = words.trim().split(/\s+/).filter(Boolean);
    const command: CliCommand = { name: name.trim(), description: description.trim(), argv };
    if (await save([...existing.filter((c) => c.name !== command.name), command])) {
      setName(''); setDescription(''); setWords(''); setPanel('none');
    }
  }

  return (
    <section className="mt-4" data-testid="cli-tool-editor">
      <div className="flex flex-wrap gap-2">
        <Button tone="quiet" data-testid="discover-commands" disabled={busy} onClick={() => void discover()}>
          {busy && !proposed ? 'Reading its help…' : 'Find commands'}
        </Button>
        <Button tone="quiet" data-testid="add-command-toggle" onClick={() => setPanel(panel === 'add' ? 'none' : 'add')}>
          Add command
        </Button>
      </div>

      {proposed && (
        <div className="mt-3 rounded-lg border border-surface-border p-3" data-testid="proposed-commands">
          <p className="mb-2 text-[12px] text-ink-faint">
            {proposed.length === 0 ? 'Nothing usable was found in its help.'
              : `Read from its own help. Tick the ones Jarvis should have — each starts on Need approval.`}
          </p>
          <div className="max-h-64 space-y-1 overflow-y-auto">
            {proposed.map((c) => (
              <label key={c.name} className="flex items-start gap-2 text-[13px] text-ink">
                <input type="checkbox" data-testid="proposed-command" data-name={c.name}
                       checked={chosen.has(c.name)}
                       onChange={(event) => setChosen((was) => {
                         const next = new Set(was);
                         if (event.target.checked) next.add(c.name); else next.delete(c.name);
                         return next;
                       })} />
                <span className="min-w-0">
                  <span className="font-medium">{c.name}</span>
                  <code className="ml-2 text-[11px] text-ink-faint">{setup.command} {c.argv.join(' ')}</code>
                </span>
              </label>
            ))}
          </div>
          {proposed.length > 0 && (
            <Button tone="primary" data-testid="add-chosen-commands" className="mt-3"
                    disabled={busy || chosen.size === 0} onClick={() => void addChosen()}>
              Add {chosen.size} command{chosen.size === 1 ? '' : 's'}
            </Button>
          )}
        </div>
      )}

      {panel === 'add' && (
        <div className="mt-3 rounded-lg border border-surface-border p-3" data-testid="add-command-form">
          <Field label="Name">
            <input className={inputClass} data-testid="command-name" placeholder="e.g. list_repos"
                   value={name} onChange={(event) => setName(event.target.value)} />
          </Field>
          <Field label={`What comes after "${setup.command}"`}
                 hint="Words and flags exactly as typed; {name} marks a value Jarvis fills in. No pipes or chaining.">
            <input className={inputClass} data-testid="command-words" placeholder="repo list --limit {count} --json"
                   value={words} onChange={(event) => setWords(event.target.value)} />
          </Field>
          <Field label="What it does (Jarvis reads this)">
            <input className={inputClass} data-testid="command-description"
                   value={description} onChange={(event) => setDescription(event.target.value)} />
          </Field>
          <Button tone="primary" data-testid="save-command" disabled={busy || !name.trim() || !words.trim()}
                  onClick={() => void addCommand()}>
            Add command
          </Button>
        </div>
      )}
      {error && <p className="mt-2 text-[13px] text-state-danger" data-testid="tool-editor-error">{error}</p>}
    </section>
  );
}

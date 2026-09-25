// The one place the front end talks to the backend.
//
// Same-origin by design: in production FastAPI serves these files and the API
// from one port, and in development next.config.mjs rewrites /api to whichever
// backend is running. So nothing here needs a base URL, and there is no CORS.

import type {
  AddConnectionResult,
  Agent,
  AgentAbilities,
  AgentDraft,
  AgentNote,
  AgentRun,
  AgentRunSummary,
  Approval,
  BriefingConfig,
  BriefingPreview,
  CatalogEntry,
  Change,
  ModelAvailability,
  ModelSelection,
  ModelsOverview,
  ProviderConnection,
  ProviderFormat,
  ProviderKind,
  Connector,
  ConnectorConnectOutcome,
  ConnectorTool,
  ApiOperation,
  CliCommand,
  ConnectionCheck,
  ConnectorSetup,
  Conversation,
  ExternalService,
  ConversationDetail,
  ConversationList,
  ImprovementStatus,
  Job,
  Lesson,
  Memory,
  MemoryCandidate,
  MemoryCategory,
  MemoryVersion,
  Monitor,
  Notification,
  OutboxRow,
  Outcome,
  ProfileEntry,
  Proposal,
  Rule,
  Skill,
  SkillDetail,
  TraceRow,
  UndoResult,
  Prefs,
  SandboxStatus,
  VoiceOptions,
  Status,
  Task,
  TaskRun,
  ContentAnalytics,
  ContentCalendarEntry,
  ContentDraft,
  ContentFilters,
  ContentItem,
  ContentItemDetail,
  ContentMediaRef,
  ContentMeta,
  ContentNicheOverview,
  ContentPlacement,
  ContentStage,
  ContentSummary,
} from './api-types';

/** A failed request, carrying the server's own message.
 *
 * The backend's error strings are written for the user to read directly — the
 * project keeps them in plain language on purpose — so they are surfaced as-is
 * rather than replaced with a generic "something went wrong".
 */
export class ApiRequestError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = 'ApiRequestError';
    this.status = status;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api${path}`, {
    ...init,
    headers: {
      // A FormData body (files) sets its own multipart boundary; labelling it
      // JSON would make the server unable to read it.
      ...(init?.body && !(init.body instanceof FormData) ? { 'Content-Type': 'application/json' } : {}),
      ...init?.headers,
    },
  });

  if (!response.ok) {
    let message = `Request failed (${response.status})`;
    try {
      const body = (await response.json()) as { error?: string };
      if (body?.error) message = body.error;
    } catch {
      // A non-JSON error body is not worth failing over — the status stands.
    }
    throw new ApiRequestError(response.status, message);
  }

  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

const json = (body: unknown): RequestInit => ({ body: JSON.stringify(body) });

/** JSON, or multipart when there are files: the description as an `item` field
 *  and each file under the key its media entry names (`{"file": key}`). */
function withFiles(body: unknown, uploads: { key: string; file: File }[]): string | FormData {
  if (!uploads.length) return JSON.stringify(body);
  const form = new FormData();
  form.set('item', JSON.stringify(body));
  for (const { key, file } of uploads) form.append(key, file, file.name);
  return form;
}

/** `?a=1&b=2` from the set values only; empty when nothing is set. */
function query(params: Record<string, string | undefined | null>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) if (value) search.set(key, value);
  const text = search.toString();
  return text ? `?${text}` : '';
}

export const api = {
  status: () => request<Status>('/status'),

  prefs: {
    get: () => request<Prefs>('/prefs'),
    update: (patch: Partial<Prefs>) => request<Prefs>('/prefs', { method: 'POST', ...json(patch) }),
  },

  conversations: {
    list: (options?: { query?: string; includeArchived?: boolean }) => {
      const params = new URLSearchParams();
      if (options?.query) params.set('q', options.query);
      if (options?.includeArchived) params.set('archived', '1');
      const suffix = params.size ? `?${params}` : '';
      return request<ConversationList>(`/conversations${suffix}`);
    },
    open: (id: string) => request<ConversationDetail>(`/conversations/${encodeURIComponent(id)}`),
    create: () => request<{ conversation: ConversationDetail['conversation'] }>('/conversations', { method: 'POST' }),
    update: (id: string, patch: { title?: string; pinned?: boolean; archived?: boolean }) =>
      request<{ conversation: ConversationDetail['conversation'] }>(
        `/conversations/${encodeURIComponent(id)}`,
        { method: 'PATCH', ...json(patch) },
      ),
    /** Moves it to the recycle bin — not a permanent delete. See `purge()`. */
    remove: (id: string) => request<{ ok: true }>(`/conversations/${encodeURIComponent(id)}`, { method: 'DELETE' }),
    activate: (id: string) =>
      request<{ conversation: ConversationDetail['conversation'] }>(
        `/conversations/${encodeURIComponent(id)}/activate`,
        { method: 'POST' },
      ),
    trash: () => request<{ conversations: Conversation[] }>('/conversations/trash'),
    restore: (id: string) =>
      request<{ conversation: ConversationDetail['conversation'] }>(
        `/conversations/${encodeURIComponent(id)}/restore`,
        { method: 'POST' },
      ),
    /** A real, irreversible delete of one item, from the recycle bin. */
    purge: (id: string) =>
      request<{ ok: true }>(`/conversations/${encodeURIComponent(id)}/permanent`, { method: 'DELETE' }),
    emptyTrash: () => request<{ ok: true; removed: number }>('/conversations/trash', { method: 'DELETE' }),
  },

  notifications: {
    list: (limit?: number) =>
      request<{ notifications: Notification[] }>(
        `/notifications${typeof limit === 'number' ? `?limit=${limit}` : ''}`,
      ),
    markRead: (ids: string[]) =>
      request<{ notifications: Notification[] }>('/notifications/read', {
        method: 'POST',
        ...json({ ids }),
      }),
    markAllRead: () =>
      request<{ notifications: Notification[] }>('/notifications/read-all', { method: 'POST' }),
    /** Moves it to the recycle bin — not a permanent delete. See `purge()`. */
    remove: (id: string) =>
      request<{ ok: true }>(`/notifications/${encodeURIComponent(id)}`, { method: 'DELETE' }),
    /** Moves everything active to the recycle bin — not a permanent delete. */
    clear: () => request<{ ok: true }>('/notifications', { method: 'DELETE' }),
    trash: (limit?: number) =>
      request<{ notifications: Notification[] }>(
        `/notifications/trash${typeof limit === 'number' ? `?limit=${limit}` : ''}`,
      ),
    restore: (id: string) =>
      request<{ ok: true }>(`/notifications/${encodeURIComponent(id)}/restore`, { method: 'POST' }),
    /** A real, irreversible delete of one item, from the recycle bin. */
    purge: (id: string) =>
      request<{ ok: true }>(`/notifications/${encodeURIComponent(id)}/permanent`, { method: 'DELETE' }),
    emptyTrash: () => request<{ ok: true; removed: number }>('/notifications/trash', { method: 'DELETE' }),
  },

  /** What can actually listen and speak right now. Every entry is computed
   *  from real state — a configured key, a capability — never from a provider
   *  name. */
  voice: {
    options: () => request<VoiceOptions>('/voice/options'),
  },

  /**
   * Provider connections and the models available through them. Testing,
   * discovering and running are three separate questions: `test` is the only
   * call that changes a connection's status. A discovery that fails throws with
   * status 501 when the provider simply does not offer a list, and 502 when it
   * could not be asked — and neither stops `addModel` from working.
   */
  models: {
    kinds: () => request<{ kinds: ProviderKind[]; formats: ProviderFormat[] }>('/models/kinds'),
    overview: () => request<ModelsOverview>('/models'),
    add: (body: { kind: string; label?: string; address?: string; format?: string; apiKey?: string }) =>
      request<AddConnectionResult>('/models', { method: 'POST', ...json(body) }),
    edit: (id: string, body: { label?: string; address?: string; apiKey?: string }) =>
      request<{ ok: true; connection: ProviderConnection }>(
        `/models/${encodeURIComponent(id)}`, { method: 'PATCH', ...json(body) }),
    test: (id: string) =>
      request<{ ok: boolean; message: string; connection: ProviderConnection }>(
        `/models/${encodeURIComponent(id)}/test`, { method: 'POST' }),
    discover: (id: string) =>
      request<{ ok: true; added: number; updated: number; connection: ProviderConnection }>(
        `/models/${encodeURIComponent(id)}/discover`, { method: 'POST' }),
    addModel: (id: string, modelId: string) =>
      request<{ ok: true; connection: ProviderConnection }>(
        `/models/${encodeURIComponent(id)}/models`, { method: 'POST', ...json({ modelId }) }),
    // A model id can hold slashes and colons ("meta-llama/Llama-3:latest"): each
    // segment is encoded, and the slashes between them are kept as the path.
    removeModel: (id: string, modelId: string) =>
      request<{ ok: true; connection: ProviderConnection }>(
        `/models/${encodeURIComponent(id)}/models/${modelId.split('/').map(encodeURIComponent).join('/')}`,
        { method: 'DELETE' }),
    remove: (id: string) =>
      request<{ ok: true; availability: ModelAvailability }>(
        `/models/${encodeURIComponent(id)}`, { method: 'DELETE' }),
    select: (choice: { providerId: string; modelId: string; effort?: string | null } | { auto: true }) =>
      request<{ ok: true; selection: ModelSelection; availability: ModelAvailability }>(
        '/models/select', { method: 'POST', ...json(choice) }),
  },

  externalServices: {
    list: () => request<{ services: ExternalService[] }>('/external-services'),
    add: (body: { label: string; key: string; extraFieldLabel?: string; extraFieldValue?: string }) =>
      request<{ ok: true; service: ExternalService }>('/external-services', {
        method: 'POST',
        ...json(body),
      }),
    update: (ref: string, body: { key: string; extraFieldLabel?: string; extraFieldValue?: string }) =>
      request<{ ok: true; service: ExternalService }>(
        `/external-services/${encodeURIComponent(ref)}`,
        { method: 'POST', ...json(body) },
      ),
    clearKey: (ref: string) =>
      request<{ ok: true }>(`/external-services/${encodeURIComponent(ref)}`, { method: 'DELETE' }),
    remove: (ref: string) =>
      request<{ ok: true }>(`/external-services/${encodeURIComponent(ref)}/full`, {
        method: 'DELETE',
      }),
  },

  connectors: {
    list: () => request<{ connectors: Connector[] }>('/connectors'),
    open: (id: string) =>
      request<{ ok: true; connector: Connector; tools: ConnectorTool[]; setup: ConnectorSetup }>(
        `/connectors/${encodeURIComponent(id)}`,
      ),
    /** API connectors: save (or replace) the key, then check it for real. */
    saveKey: (id: string, apiKey: string) =>
      request<ConnectionCheck>(`/connectors/${encodeURIComponent(id)}/key`,
        { method: 'POST', ...json({ apiKey }) }),
    /** API and CLI connectors: check the connection now and record the result. */
    test: (id: string) =>
      request<ConnectionCheck>(`/connectors/${encodeURIComponent(id)}/test`, { method: 'POST' }),
    /** CLI connectors: start the program's own browser sign-in. */
    login: (id: string) =>
      request<{ ok: true }>(`/connectors/${encodeURIComponent(id)}/login`, { method: 'POST' }),
    /** API connectors: read an OpenAPI document and PROPOSE endpoints (saves nothing). */
    importOpenapi: (id: string, specUrl: string) =>
      request<{ ok: true; baseUrl: string | null; auth: ConnectorSetup['auth'] | null;
                operations: ApiOperation[] }>(
        `/connectors/${encodeURIComponent(id)}/import-openapi`,
        { method: 'POST', ...json({ specUrl }) }),
    /** CLI connectors: read the program's --help and PROPOSE commands (saves nothing). */
    discoverCommands: (id: string) =>
      request<{ ok: true; proposed: CliCommand[]; helpText: string }>(
        `/connectors/${encodeURIComponent(id)}/discover-commands`, { method: 'POST' }),
    /** CLI connectors: give this one program a key as an environment variable. */
    envSecret: (id: string, name: string, value: string) =>
      request<ConnectionCheck>(`/connectors/${encodeURIComponent(id)}/env-secret`,
        { method: 'POST', ...json({ name, value }) }),
    /** Creates a Custom Connector — the one place a mechanism (mcp/api/cli)
     *  is picked directly, since a custom connector's mechanism can't be
     *  inferred the way a catalogue entry's can. */
    create: (body: {
      type: string; label: string; description?: string; config?: unknown; apiKey?: string;
    }) =>
      request<{ ok: true; connector: Connector }>('/connectors', { method: 'POST', ...json(body) }),
    update: (id: string, patch: {
      enabled?: boolean; label?: string; toolPermissions?: Record<string, string>;
      config?: Record<string, unknown>;
    }) =>
      request<{ ok: true; connector: Connector }>(`/connectors/${encodeURIComponent(id)}`, {
        method: 'PATCH',
        ...json(patch),
      }),
    refresh: (id: string) =>
      request<{ ok: true; tools: number; connector: Connector }>(
        `/connectors/${encodeURIComponent(id)}/refresh`,
        { method: 'POST' },
      ),
    remove: (id: string) =>
      request<{ ok: true }>(`/connectors/${encodeURIComponent(id)}`, { method: 'DELETE' }),

    /** The Official Connectors directory — one uniform Connect button per
     *  entry, no "ready"/"needs setup" badge. */
    catalog: () => request<{ catalog: CatalogEntry[] }>('/connectors/catalog'),
    /** Creates (or finds) the underlying connector record for a catalogue
     *  entry — called once, the first time its detail view opens. */
    ensure: (catalogId: string) =>
      request<{ ok: true; connectorId: string }>(
        `/connectors/catalog/${encodeURIComponent(catalogId)}/ensure`,
        { method: 'POST' },
      ),
    /** The shared Client ID/Secret for a catalogue entry — registered once,
     *  reused by every connector that entry ever creates. */
    registerClient: (catalogId: string, clientId: string, clientSecret?: string) =>
      request<{ ok: true }>(
        `/connectors/catalog/${encodeURIComponent(catalogId)}/register-client`,
        { method: 'POST', ...json({ clientId, clientSecret }) },
      ),
    deregisterClient: (catalogId: string) =>
      request<{ ok: true }>(
        `/connectors/catalog/${encodeURIComponent(catalogId)}/register-client`,
        { method: 'DELETE' },
      ),
    oauthRedirectUri: () => request<{ uri: string }>('/connectors/oauth/redirect-uri'),
    /** Starts (or restarts) an mcp connector's OAuth flow. `clientId`/
     *  `clientSecret` are the guided-setup form's fields — omit both to
     *  attempt Dynamic Client Registration first. */
    connect: (id: string, manual?: { clientId?: string; clientSecret?: string }) =>
      request<ConnectorConnectOutcome>(`/connectors/${encodeURIComponent(id)}/connect`, {
        method: 'POST',
        ...json(manual ?? {}),
      }),
    disconnect: (id: string) =>
      request<{ ok: true; connector: Connector }>(
        `/connectors/${encodeURIComponent(id)}/disconnect`,
        { method: 'POST' },
      ),
  },

  sandbox: {
    status: () => request<SandboxStatus>('/sandbox/status'),
  },

  tasks: {
    /** `descriptions` is the plain-English sentence for each task's schedule,
     *  keyed by id — computed by the backend's own `describe()` so the
     *  scheduling vocabulary has exactly one implementation. */
    list: () => request<{ tasks: Task[]; descriptions: Record<string, string> }>('/tasks'),
    /** A task beside what it has actually been doing, in one request — the
     *  detail view always wants both. */
    open: (id: string) =>
      request<{ ok: true; task: Task; runs: TaskRun[]; description: string }>(
        `/tasks/${encodeURIComponent(id)}`,
      ),
    create: (task: Partial<Task>) =>
      request<{ ok: true; task: Task }>('/tasks', { method: 'POST', ...json(task) }),
    update: (id: string, patch: Partial<Task>) =>
      request<{ ok: true; task: Task }>(`/tasks/${encodeURIComponent(id)}`, {
        method: 'PATCH',
        ...json(patch),
      }),
    remove: (id: string) =>
      request<{ ok: true }>(`/tasks/${encodeURIComponent(id)}`, { method: 'DELETE' }),
    runNow: (id: string) =>
      request<{ ok: true; result: unknown }>(`/tasks/${encodeURIComponent(id)}/run`, {
        method: 'POST',
      }),
    runs: (taskId?: string) =>
      request<{ runs: TaskRun[] }>(
        `/task-runs${taskId ? `?taskId=${encodeURIComponent(taskId)}` : ''}`,
      ),
  },

  memories: {
    /** `conflicted` names the memories a pending candidate contradicts. They are
     *  not being asserted to the model while they wait, and a screen that showed
     *  them as though nothing were wrong would be saying something untrue. */
    list: (options: { category?: string; query?: string; origin?: string;
                      includeArchived?: boolean } = {}) => {
      const params = new URLSearchParams();
      if (options.category) params.set('category', options.category);
      if (options.query) params.set('query', options.query);
      if (options.origin) params.set('origin', options.origin);
      if (options.includeArchived) params.set('includeArchived', 'true');
      const query = params.toString();
      return request<{ memories: Memory[]; conflicted: string[] }>(
        `/memories${query ? `?${query}` : ''}`,
      );
    },
    categories: () => request<{ categories: MemoryCategory[] }>('/memories/categories'),
    /** Empty for an id that never existed: "what changed about this" has a true
     *  answer for something that does not exist. */
    versions: (id: string) =>
      request<{ versions: MemoryVersion[] }>(`/memories/${encodeURIComponent(id)}/versions`),
    create: (memory: { text: string; category?: string; importance?: number }) =>
      request<{ ok: true; memory: Memory }>('/memories', { method: 'POST', ...json(memory) }),
    update: (id: string, patch: { text?: string; category?: string; importance?: number }) =>
      request<{ ok: true; memory: Memory }>(`/memories/${encodeURIComponent(id)}`, {
        method: 'PATCH', ...json(patch),
      }),
    /** Keeps the primary, archives the rest — a merge that turns out to be wrong
     *  should be recoverable. */
    merge: (primaryId: string, otherIds: string[], text: string, category?: string) =>
      request<{ ok: true; memory: Memory }>('/memories/merge', {
        method: 'POST', ...json({ primaryId, otherIds, text, category }),
      }),
    archive: (id: string) =>
      request<{ ok: true }>(`/memories/${encodeURIComponent(id)}/archive`, { method: 'POST' }),
    restore: (id: string) =>
      request<{ ok: true }>(`/memories/${encodeURIComponent(id)}/restore`, { method: 'POST' }),
    /** Only ever from an archived row: archive first is what leaves something
     *  for an undo elsewhere to point at. */
    remove: (id: string) =>
      request<{ ok: true }>(`/memories/${encodeURIComponent(id)}`, { method: 'DELETE' }),

    candidates: () => request<{ candidates: MemoryCandidate[] }>('/memories/candidates'),
    approve: (id: string, edits: { text?: string; category?: string } = {}) =>
      request<{ ok: true; memory: Memory }>(
        `/memories/candidates/${encodeURIComponent(id)}/approve`,
        { method: 'POST', ...json(edits) },
      ),
    reject: (id: string) =>
      request<{ ok: true }>(`/memories/candidates/${encodeURIComponent(id)}/reject`,
        { method: 'POST' }),
    /** `use-new` EDITS the contradicted memory rather than adding a second one:
     *  two memories asserting opposite things is the state this prevents. */
    resolveConflict: (id: string, choice: 'keep-old' | 'use-new' | 'keep-both',
                      edits: { text?: string; category?: string } = {}) =>
      request<{ ok: true; memory: Memory | null }>(
        `/memories/candidates/${encodeURIComponent(id)}/resolve-conflict`,
        { method: 'POST', ...json({ choice, ...edits }) },
      ),
  },

  profile: {
    /** Oldest first — the order they were written in, not the browse order. */
    list: () => request<{ entries: ProfileEntry[] }>('/profile'),
    add: (text: string) =>
      request<{ ok: true; entry: ProfileEntry }>('/profile', { method: 'POST', ...json({ text }) }),
    update: (id: string, text: string) =>
      request<{ ok: true; entry: ProfileEntry }>(`/profile/${encodeURIComponent(id)}`, {
        method: 'PATCH', ...json({ text }),
      }),
    versions: (id: string) =>
      request<{ versions: MemoryVersion[] }>(`/profile/${encodeURIComponent(id)}/versions`),
    remove: (id: string) =>
      request<{ ok: true }>(`/profile/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  },

  improvement: {
    status: () => request<ImprovementStatus>('/improvement/status'),
    proposals: (status = 'pending') =>
      request<{ proposals: Proposal[] }>(`/improvement/proposals?status=${encodeURIComponent(status)}`),
    /** Applied by the policy, which refuses anything that is not a rule or a
     *  setting whatever the caller believed it was approving. */
    approve: (id: string) =>
      request<{ ok: true; applied: unknown; proposal: Proposal }>(
        `/improvement/proposals/${encodeURIComponent(id)}/approve`, { method: 'POST' }),
    reject: (id: string) =>
      request<{ ok: true; proposal: Proposal }>(
        `/improvement/proposals/${encodeURIComponent(id)}/reject`, { method: 'POST' }),
    restoreProposal: (id: string) =>
      request<{ ok: true; proposal: Proposal }>(
        `/improvement/proposals/${encodeURIComponent(id)}/restore`, { method: 'POST' }),
    deleteProposal: (id: string) =>
      request<{ ok: true }>(`/improvement/proposals/${encodeURIComponent(id)}`,
        { method: 'DELETE' }),
    /** Generating this IS the approval action for an idea that needs real code:
     *  Jarvis never edits its own. */
    brief: (id: string, target: string) =>
      request<{ ok: true; prompt: string; target: string; proposal: Proposal }>(
        `/improvement/proposals/${encodeURIComponent(id)}/implementation-prompt`,
        { method: 'POST', ...json({ target }) }),

    rules: (includeArchived = false) =>
      request<{ rules: Rule[] }>(`/improvement/rules${includeArchived ? '?includeArchived=true' : ''}`),
    toggleRule: (id: string, active?: boolean) =>
      request<{ ok: true; rule: Rule }>(`/improvement/rules/${encodeURIComponent(id)}/toggle`,
        { method: 'POST', ...json({ active }) }),
    editRule: (id: string, text: string) =>
      request<{ ok: true; rule: Rule }>(`/improvement/rules/${encodeURIComponent(id)}`,
        { method: 'PATCH', ...json({ text }) }),
    archiveRule: (id: string) =>
      request<{ ok: true; rule: Rule }>(`/improvement/rules/${encodeURIComponent(id)}/archive`,
        { method: 'POST' }),
    restoreRule: (id: string) =>
      request<{ ok: true; rule: Rule }>(`/improvement/rules/${encodeURIComponent(id)}/restore`,
        { method: 'POST' }),
    deleteRule: (id: string) =>
      request<{ ok: true }>(`/improvement/rules/${encodeURIComponent(id)}`, { method: 'DELETE' }),

    lessons: (status = 'active') =>
      request<{ lessons: Lesson[] }>(`/improvement/lessons?status=${encodeURIComponent(status)}`),
    archiveLesson: (id: string) =>
      request<{ ok: true; lesson: Lesson }>(`/improvement/lessons/${encodeURIComponent(id)}/archive`,
        { method: 'POST' }),
    restoreLesson: (id: string) =>
      request<{ ok: true; lesson: Lesson }>(`/improvement/lessons/${encodeURIComponent(id)}/restore`,
        { method: 'POST' }),
    deleteLesson: (id: string) =>
      request<{ ok: true }>(`/improvement/lessons/${encodeURIComponent(id)}`, { method: 'DELETE' }),

    changes: () => request<{ changes: Change[] }>('/improvement/changes'),
    /** A refusal is a 200 with a reason, not an error: the caller asked a real
     *  question and got a real answer. */
    undo: (id: number | string, force = false) =>
      request<UndoResult>(`/improvement/changes/${encodeURIComponent(String(id))}/undo`,
        { method: 'POST', ...json({ force }) }),

    outcomes: () => request<{ outcomes: Outcome[] }>('/improvement/outcomes'),
    lookupOutcomes: (ids: string[]) =>
      request<{ outcomes: Outcome[] }>('/improvement/outcomes/lookup',
        { method: 'POST', ...json({ ids }) }),
  },

  jobs: {
    list: (status?: string) =>
      request<{ jobs: Job[] }>(`/jobs${status ? `?status=${encodeURIComponent(status)}` : ''}`),
    /** The job beside what it actually did, in one request: the trace is how
     *  "it says it did this" is told apart from "it did this". */
    open: (id: string) =>
      request<{ job: Job; trace: TraceRow[]; outbox: OutboxRow[] }>(`/jobs/${encodeURIComponent(id)}`),
    create: (job: { goal: string; title?: string; kind?: string }) =>
      request<{ ok: true; job: Job }>('/jobs', { method: 'POST', ...json(job) }),
    /** The one mechanism every parked decision uses — including the first start
     *  of desktop work, which never begins unattended. */
    resume: (id: string, guidance?: string) =>
      request<{ ok: true; job: Job }>(`/jobs/${encodeURIComponent(id)}/resume`,
        { method: 'POST', ...json({ guidance }) }),
    restart: (id: string, force = false) =>
      request<{ ok: boolean; reason?: string; message?: string; job?: Job }>(
        `/jobs/${encodeURIComponent(id)}/restart`, { method: 'POST', ...json({ force }) }),
    discard: (id: string) =>
      request<{ ok: true; job: Job }>(`/jobs/${encodeURIComponent(id)}/discard`, { method: 'POST' }),
  },

  agents: {
    list: () => request<{ agents: Agent[] }>('/agents'),
    open: (id: string) =>
      request<{ agent: Agent; runs: AgentRunSummary[]; notes: AgentNote[]; hasDefault: boolean }>(
        `/agents/${encodeURIComponent(id)}`),
    create: (draft: AgentDraft) =>
      request<{ ok: true; agent: Agent }>('/agents', { method: 'POST', ...json(draft) }),
    update: (id: string, patch: Partial<AgentDraft>) =>
      request<{ ok: true; agent: Agent }>(`/agents/${encodeURIComponent(id)}`,
        { method: 'PATCH', ...json(patch) }),
    remove: (id: string) =>
      request<{ ok: true }>(`/agents/${encodeURIComponent(id)}`, { method: 'DELETE' }),
    reset: (id: string) =>
      request<{ ok: true; agent: Agent }>(`/agents/${encodeURIComponent(id)}/reset`,
        { method: 'POST' }),
    abilities: () => request<AgentAbilities>('/agents/abilities'),
    /** One run with the whole delegation tree it belongs to. */
    run: (runId: string) =>
      request<{ run: AgentRun; tree: AgentRun[] }>(`/agent-runs/${encodeURIComponent(runId)}`),
    updateNote: (agentId: string, noteId: string, patch: { topic?: string; text?: string }) =>
      request<{ ok: true; note: AgentNote }>(
        `/agents/${encodeURIComponent(agentId)}/notes/${encodeURIComponent(noteId)}`,
        { method: 'PATCH', ...json(patch) }),
    removeNote: (agentId: string, noteId: string) =>
      request<{ ok: true }>(
        `/agents/${encodeURIComponent(agentId)}/notes/${encodeURIComponent(noteId)}`,
        { method: 'DELETE' }),
  },

  briefing: {
    get: () => request<BriefingConfig>('/briefing'),
    /** Merged, never replaced: the backend owns which keys exist. */
    save: (patch: Partial<BriefingConfig>) =>
      request<BriefingConfig>('/briefing', { method: 'POST', ...json(patch) }),
    /** Composes one for real. Reading the settings and imagining the result is
     *  not the same as hearing it. */
    preview: () => request<BriefingPreview>('/briefing/preview', { method: 'POST' }),
  },

  monitors: {
    list: () => request<{ monitors: Monitor[] }>('/monitors'),
    /** There is deliberately no way to START one here: working out a concrete
     *  check from what was actually said is the watching capability's job. */
    stop: (id: string) =>
      request<{ ok: true }>(`/monitors/${encodeURIComponent(id)}/stop`, { method: 'POST' }),
  },

  skills: {
    /** Folder Skills only. The route reads the folder list, which has no code
     *  path back to a built-in ability — a Skills UI must never enumerate
     *  capabilities and filter, because that has been got wrong before. */
    list: () => request<{ skills: Skill[] }>('/skills/installed'),
    open: (name: string) => request<SkillDetail>(`/skills/${encodeURIComponent(name)}`),
    create: (skill: { name: string; description: string; instructions: string }) =>
      request<{ ok: true; skill: Skill }>('/skills', { method: 'POST', ...json(skill) }),
    update: (name: string, patch: { enabled?: boolean; description?: string; instructions?: string }) =>
      request<{ ok: true; skill: Skill }>(`/skills/${encodeURIComponent(name)}`, {
        method: 'PATCH', ...json(patch),
      }),
    remove: (name: string) =>
      request<{ ok: true }>(`/skills/${encodeURIComponent(name)}`, { method: 'DELETE' }),
    /** One route for all three ways in — a repository link, a pasted file, or a
     *  zip — because they differ only in where the folder comes from. */
    installFromRepo: (repo: string) =>
      request<{ ok: true; skill: Skill }>('/skills/install', { method: 'POST', ...json({ repo }) }),
    installFromMarkdown: (markdown: string) =>
      request<{ ok: true; skill: Skill }>('/skills/install', { method: 'POST', ...json({ markdown }) }),
    installFromZip: async (file: File) => {
      const response = await fetch('/api/skills/install', {
        method: 'POST', body: file, headers: { 'Content-Type': 'application/zip' },
      });
      if (!response.ok) {
        let message = `Could not install that (${response.status})`;
        try {
          const body = (await response.json()) as { error?: string };
          if (body?.error) message = body.error;
        } catch {
          /* the status stands */
        }
        throw new ApiRequestError(response.status, message);
      }
      return (await response.json()) as { ok: true; skill: Skill };
    },
    downloadUrl: (name: string) => `/api/skills/${encodeURIComponent(name)}/download`,
  },

  approvals: {
    pending: (session?: string) =>
      request<{ approvals: Approval[] }>(
        `/approvals${session ? `?session=${encodeURIComponent(session)}` : ''}`,
      ),
    /** The run is genuinely waiting on this, which is why the transcript's
     *  prompt is a real control rather than a note about one. */
    decide: (id: string, decision: 'allow' | 'deny' | 'cancel') =>
      request<{ approval: Approval; ran: boolean }>(`/approvals/${encodeURIComponent(id)}`, {
        method: 'POST',
        ...json({ decision }),
      }),
  },

  content: {
    meta: () => request<ContentMeta>('/content-meta'),
    /** The folders: every niche (empty ones too), "No niche", and everything. */
    niches: () => request<ContentNicheOverview>('/content-niches'),
    createNiche: (name: string) =>
      request<{ ok: true; name: string }>('/content-niches', { method: 'POST', ...json({ name }) }),
    /** Everything in the folder moves with it. Onto another niche's name is refused. */
    renameNiche: (name: string, to: string) =>
      request<{ ok: true; name: string }>(`/content-niches/${encodeURIComponent(name)}`,
        { method: 'PATCH', ...json({ name: to }) }),
    /** Only an empty niche — deleting a folder never deletes content. */
    deleteNiche: (name: string) =>
      request<{ ok: true }>(`/content-niches/${encodeURIComponent(name)}`, { method: 'DELETE' }),
    list: (stage: ContentStage | 'bin' | 'active', filters: ContentFilters & {
      from?: string; to?: string; archivedFrom?: string; limit?: number; offset?: number;
    } = {}) => {
      const { limit, offset, ...rest } = filters;
      return request<{ items: ContentItem[]; total?: number }>(`/content-items${query({
        stage, ...rest, limit: limit ? String(limit) : undefined, offset: offset ? String(offset) : undefined,
      })}`);
    },
    summary: (filters: ContentFilters = {}) =>
      request<ContentSummary>(`/content-items/summary${query({ ...filters })}`),
    calendar: (start: string, end: string, filters: ContentFilters = {}) =>
      request<{ entries: ContentCalendarEntry[] }>(`/content-items/calendar${query({ start, end, ...filters })}`),
    analytics: (filters: ContentFilters & { from?: string; to?: string } = {}) =>
      request<ContentAnalytics>(`/content-analytics${query({ ...filters })}`),
    get: (id: string) => request<{ item: ContentItemDetail }>(`/content-items/${encodeURIComponent(id)}`),
    /** Adding content — the same door agents and Jarvis use. With files it is
     *  multipart: `item` (JSON) plus each file under the name its media entry uses. */
    create: (item: ContentDraft, uploads: { key: string; file: File }[] = []) =>
      request<{ ok: true; item: ContentItem }>('/content-items', { method: 'POST', body: withFiles(item, uploads) }),
    edit: (id: string, patch: {
      name?: string; niche?: string; fields?: Record<string, string | string[]>; media?: ContentMediaRef[];
    }, uploads: { key: string; file: File }[] = []) =>
      request<{ ok: true; item: ContentItem }>(`/content-items/${encodeURIComponent(id)}`,
        { method: 'PATCH', body: withFiles(patch, uploads) }),
    /** Handing back a revised version — what an agent does, from the screen. */
    revise: (id: string, body: {
      fields?: Record<string, string | string[]>; media?: ContentMediaRef[]; note?: string; by?: string;
    }, uploads: { key: string; file: File }[] = []) =>
      request<{ ok: true; item: ContentItem }>(`/content-items/${encodeURIComponent(id)}/revisions`,
        { method: 'POST', body: withFiles(body, uploads) }),
    approve: (id: string) =>
      request<{ ok: true; item: ContentItem }>(`/content-items/${encodeURIComponent(id)}/approve`, { method: 'POST' }),
    requestChanges: (id: string, body: { what: string; why: string; assignee: 'agent' | 'jarvis' }) =>
      request<{ ok: true; item: ContentItem }>(`/content-items/${encodeURIComponent(id)}/request-changes`,
        { method: 'POST', ...json(body) }),
    addPlacement: (id: string, body: { platform: string; destination?: string }) =>
      request<{ ok: true; placement: ContentPlacement; item: ContentItem }>(
        `/content-items/${encodeURIComponent(id)}/placements`, { method: 'POST', ...json(body) }),
    archive: (id: string) =>
      request<{ ok: true; item: ContentItem }>(`/content-items/${encodeURIComponent(id)}/archive`, { method: 'POST' }),
    unarchive: (id: string) =>
      request<{ ok: true; item: ContentItem }>(`/content-items/${encodeURIComponent(id)}/unarchive`, { method: 'POST' }),
    /** To the recycle bin — not a permanent delete. See `purge()`. */
    remove: (id: string) =>
      request<{ ok: true; item: ContentItem }>(`/content-items/${encodeURIComponent(id)}`, { method: 'DELETE' }),
    restore: (id: string) =>
      request<{ ok: true; item: ContentItem }>(`/content-items/${encodeURIComponent(id)}/restore`, { method: 'POST' }),
    purge: (id: string) =>
      request<{ ok: true }>(`/content-items/${encodeURIComponent(id)}/permanent`, { method: 'DELETE' }),
    emptyBin: () => request<{ ok: true; removed: number }>('/content-items/trash', { method: 'DELETE' }),
    updatePlacement: (id: string, body: {
      overrides?: Record<string, string | string[]>; media?: ContentMediaRef[]; destination?: string;
    }, uploads: { key: string; file: File }[] = []) =>
      request<{ ok: true; placement: ContentPlacement }>(`/content-placements/${encodeURIComponent(id)}`,
        { method: 'PATCH', body: withFiles(body, uploads) }),
    removePlacement: (id: string) =>
      request<{ ok: true; item: ContentItem }>(`/content-placements/${encodeURIComponent(id)}`, { method: 'DELETE' }),
    schedule: (id: string, scheduledAt: string, timezone: string) =>
      request<{ ok: true; placement: ContentPlacement }>(`/content-placements/${encodeURIComponent(id)}/schedule`,
        { method: 'POST', ...json({ scheduledAt, timezone }) }),
    unschedule: (id: string) =>
      request<{ ok: true; item: ContentItem }>(`/content-placements/${encodeURIComponent(id)}/unschedule`,
        { method: 'POST' }),
    postNow: (id: string) =>
      request<{ ok: true; item: ContentItem }>(`/content-placements/${encodeURIComponent(id)}/post-now`,
        { method: 'POST' }),
    requeue: (id: string) =>
      request<{ ok: true; item: ContentItem }>(`/content-placements/${encodeURIComponent(id)}/requeue`,
        { method: 'POST' }),
    markPosted: (id: string, url: string) =>
      request<{ ok: true; item: ContentItem }>(`/content-placements/${encodeURIComponent(id)}/mark-posted`,
        { method: 'POST', ...json({ url }) }),
    recordMetrics: (id: string, metrics: Record<string, number>, capturedAt?: string) =>
      request<{ ok: true; placement: ContentPlacement }>(`/content-placements/${encodeURIComponent(id)}/metrics`,
        { method: 'POST', ...json({ metrics, capturedAt, by: 'you' }) }),
    updateRequest: (id: string, body: { what?: string; why?: string; assignee?: 'agent' | 'jarvis' }) =>
      request<{ ok: true; item: ContentItemDetail }>(`/content-change-requests/${encodeURIComponent(id)}`,
        { method: 'PATCH', ...json(body) }),
    cancelRequest: (id: string) =>
      request<{ ok: true; item: ContentItem }>(`/content-change-requests/${encodeURIComponent(id)}/cancel`,
        { method: 'POST' }),
    retryJarvis: (id: string) =>
      request<{ ok: boolean; item: ContentItemDetail | null }>(
        `/content-change-requests/${encodeURIComponent(id)}/start-jarvis`, { method: 'POST' }),
  },

  uploads: {
    /** Raw body, not multipart: the backend lands the bytes and returns an id,
     *  and an id is all that ever reaches a turn. */
    create: async (file: File) => {
      const response = await fetch(
        `/api/uploads?name=${encodeURIComponent(file.name)}`,
        { method: 'POST', body: file, headers: { 'Content-Type': file.type || 'application/octet-stream' } },
      );
      if (!response.ok) {
        let message = `Upload failed (${response.status})`;
        try {
          const body = (await response.json()) as { error?: string };
          if (body?.error) message = body.error;
        } catch {
          /* the status stands */
        }
        throw new ApiRequestError(response.status, message);
      }
      return (await response.json()) as {
        ok: true; id: string; name: string; size: number;
        /** 'image' | 'video' | 'audio' | 'document' | 'unknown' — the same
         *  classification the backend uses to decide what a model gets, so
         *  the browser never re-guesses it from a `File.type` that might
         *  disagree. */
        kind: string;
      };
    },
  },
};

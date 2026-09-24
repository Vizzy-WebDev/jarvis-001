// Shapes the backend actually returns.
//
// Hand-written for now, from the recorded contract fixtures in
// backend/tests/contract/fixtures/ — the same recordings the Python port is held
// to, so these types describe what the server really sends rather than what it
// was assumed to send.
//
// These are TEMPORARY BY DESIGN. Once Wave 1 lands in FastAPI, they get
// generated from its OpenAPI schema (openapi-typescript) and this file is
// deleted. Until then, any change here should be checked against a fixture, not
// against memory.

export interface Conversation {
  id: string;
  title: string;
  createdAt: string;
  updatedAt: string;
  pinned: boolean;
  archived: boolean;
  /** Absent unless the query computed it — a listing has it, a single fetch does not. */
  messageCount?: number;
  /** Present only once it has been moved to the recycle bin — absent for an
   *  active conversation, never null. Gone for good 30 days after this, or
   *  sooner if emptied by hand. */
  deletedAt?: string;
}

export type MessageRole = 'user' | 'assistant' | 'tool';

export interface ToolCall {
  id?: string;
  name: string;
  args?: Record<string, unknown>;
}

export interface ToolResult {
  id?: string;
  name: string;
  result?: unknown;
}

export interface MediaPart {
  kind: 'image' | 'video';
  mimeType: string;
  dataBase64?: string;
  uri?: string;
}

export interface Message {
  id: string;
  role: MessageRole;
  text?: string;
  createdAt: string;
  toolCalls?: ToolCall[];
  toolResults?: ToolResult[];
  modelId?: string;
  media?: MediaPart[];
  /** Set when a barge-in cut the reply off; `spokenText` is what was actually heard. */
  interrupted?: boolean;
  spokenText?: string;
}

export interface ConversationList {
  conversations: Conversation[];
  activeId: string;
}

export interface ConversationDetail {
  conversation: Conversation;
  messages: Message[];
}

export interface QuietHours {
  enabled: boolean;
  /** 'HH:MM', 24-hour. A window may wrap past midnight. */
  start: string;
  end: string;
}

/**
 * The `selected*` fields are the person's own choice of model. They are written
 * through `api.models.select`, which checks them, not by patching prefs directly:
 * a selection that names something that does not exist is refused there.
 * `balance` is how Jarvis spends a turn — a different thing from which model, and
 * from effort — and applies to every model alike.
 */
export interface Prefs {
  clarifySensitivity: 'more' | 'balanced' | 'less';
  ttsProvider: string | null;
  selectedProviderId: string | null;
  selectedModelId: string | null;
  selectedEffort: string | null;
  balance: 'fast' | 'balanced' | 'quality';
  verifyChatAnswers: boolean;
  memoryTrust: 'ask' | 'balanced' | 'auto';
  maxBackgroundJobs: number;
  improvementEnabled: boolean;
  improvementTrust: 'ask' | 'balanced' | 'auto';
  improvementResearch: 'off' | 'weekly';
  quietHours: QuietHours;
}

export interface Status {
  configured: boolean;
}

// --- providers and the models available through them -------------------------

/** What a kind of provider needs from the person — the picker's contents. */
export interface ProviderKind {
  id: 'openai' | 'anthropic' | 'gemini' | 'ollama' | 'lmstudio' | 'custom' | string;
  label: string;
  blurb: string;
  /** 'fixed' is never asked for; 'editable' has a default they may change. */
  address: 'fixed' | 'editable' | 'required';
  defaultAddress: string | null;
  key: 'required' | 'optional' | 'none';
  /** Only Custom: the person says which type of API it speaks. */
  chooseFormat: boolean;
}

export interface ProviderFormat {
  id: string;
  label: string;
}

/**
 * Reasoning effort as the PROVIDER reported it for one model. Absent means the
 * provider said nothing, and no control is offered — levels are never assumed.
 */
export interface ModelEffort {
  levels: string[];
  default: string | null;
}

export interface ProviderModel {
  id: string;
  label: string;
  source: 'discovered' | 'manual';
  /** False once a later discovery no longer names it. A note, not a verdict: it still runs. */
  stillListed: boolean;
  effort: ModelEffort | null;
}

export interface ProviderConnection {
  id: string;
  kind: string;
  kindLabel: string;
  format: string;
  label: string;
  address: string;
  addressEditable: boolean;
  keyNeeded: 'required' | 'optional' | 'none';
  /** Whether a key is saved. The key itself is never sent to the browser. */
  hasKey: boolean;
  /** Set only by a connection test — never by a discovery or a turn. */
  state: 'untested' | 'ok' | 'error';
  detail: string | null;
  checkedAt: string | null;
  discoveredAt: string | null;
  models: ProviderModel[];
}

export interface ModelSelection {
  /** True when the person chose Auto: Jarvis picks per message, and no model is named. */
  auto: boolean;
  providerId: string | null;
  modelId: string | null;
  effort: string | null;
}

/** Whether the selection can be run right now — worked out on request, never stored. */
export interface ModelAvailability {
  state: 'ok' | 'none' | 'missing_connection' | 'missing_model' | 'no_key';
  message: string | null;
}

export interface ModelsOverview {
  connections: ProviderConnection[];
  selection: ModelSelection;
  availability: ModelAvailability;
}

/** What asking a provider for its models came back with. Never a connection status. */
export interface DiscoveryOutcome {
  ok: boolean;
  unsupported?: boolean;
  message?: string;
  added?: number;
  updated?: number;
}

export interface AddConnectionResult {
  ok: true;
  tested: { ok: boolean; message: string };
  discovery: DiscoveryOutcome | null;
  connection: ProviderConnection;
}

/** Every error response in this API is `{error: string}`, shown to the user verbatim. */
export interface ApiError {
  error: string;
}

export interface Notification {
  id: string;
  kind: string;
  level: 'info' | 'success' | 'warning' | 'error';
  title: string;
  body: string;
  /** `{label, section}` — a section id rather than a callback, so it survives
   *  JSON and still works for a notice the server generated on its own. */
  action: { label: string; section: string } | null;
  meta: Record<string, unknown> | null;
  ts: string;
  read: boolean;
  /** The same fault repeating collapses into one row and counts. */
  count: number;
  /** Present only once it has been moved to the recycle bin — absent for an
   *  active notification, never null. Gone for good 30 days after this, or
   *  sooner if emptied by hand. */
  trashedAt?: string;
}

/** One event from `GET /api/chat/stream`. The wire vocabulary is deliberately
 *  small and stable — see backend/jarvis/routes/turn.py's `to_wire`. */
/** Something a tool produced for the person to see or open. */
export interface TurnAttachment {
  type: 'attachment';
  kind: string;
  url: string;
  mimeType: string;
  /** A file's own name — shown on a download, where a picture needs none. */
  name?: string;
}

export type TurnEvent =
  | { type: 'routed'; intent: string; fast: boolean; confidence: number; reason: string;
      /** The user message's real, persisted id — present so Edit/Retry can act
       *  on THIS turn without waiting for a reload to learn it. */
      userMessageId?: string }
  | { type: 'chunk'; text: string }
  | { type: 'tool_result'; capability: string; ok: boolean; outcome: string; error: string | null;
      attachment?: TurnAttachment;
      /** Every file, when one tool produced several — a specialist handing back
       *  a worksheet and its answer key. `attachment` is the first of them. */
      attachments?: TurnAttachment[];
      /** A tool asked the interface to open a section. Navigating is something
       *  the browser does, so it arrives beside the result rather than inside
       *  it, exactly like an attachment. */
      navigate?: { section: string } }
  | { type: 'approval_required'; approvalId: string; capability: string; reason: string }
  | { type: 'model_switch'; to: string; from: string | null; reason: string }
  | { type: 'interrupted'; spokenText: string }
  | { type: 'progress'; phase: string }
  | { type: 'error'; error: string; code?: string; detail?: unknown }
  | { type: 'done'; text: string; steps: number;
      /** The reply's real, persisted id — see `userMessageId` above. */
      messageId?: string;
      /** Set when the person was talking to a specialist directly: who answered. */
      agent?: { id: string; name: string } }
  | { type: 'unknown' };

// --- scheduled tasks ----------------------------------------------------------

/** Free-form by design on the wire: `scheduler/recurrence.py` owns what a shape
 *  means, and a type here that tried to enumerate them would be a second, worse
 *  copy of that knowledge. `type` is the one field every shape has. */
export interface Recurrence {
  type: string;
  [key: string]: unknown;
}

export interface TaskAction {
  type: 'prompt' | 'message' | 'briefing' | string;
  /** `text`, NOT `prompt` — `scheduler/engine.py`'s `_run_prompt` reads this
   *  exact key, and a mismatch makes every task fail at run time with "this
   *  task has nothing to ask". */
  text?: string;
  /** A one-off model pin, or absent for Auto. Honoured by ORDER, not
   *  exclusion: a pin to a model that is later deleted falls back to the usual
   *  ranking rather than breaking the task. */
  modelId?: string;
  /** Connector IDS, never tool names. Resolved to what that connector can do
   *  at RUN time, so a task never goes stale when a connector is refreshed. */
  connectors?: string[];
  [key: string]: unknown;
}

export interface Task {
  id: string;
  title: string;
  recurrence: Recurrence;
  action: TaskAction;
  enabled: boolean;
  /** 'always' | 'on_error' | 'never'. */
  notify: string;
  /** Null whenever the task is off — a paused task must not advertise a time. */
  nextRunAt: string | null;
  createdAt: string;
  lastRunAt: string | null;
}

export interface TaskRun {
  id: string;
  taskId: string;
  ok: boolean;
  summary?: string;
  error?: string;
  ranAt?: string;
  /** Which model ANSWERED — not necessarily the one the task pinned, since a
   *  pin is an ordering the gateway can fall through. */
  modelId?: string;
  [key: string]: unknown;
}

// --- approvals ----------------------------------------------------------------

export interface Approval {
  id: string;
  capability: string;
  args: Record<string, unknown>;
  risk: string;
  reason: string;
  sessionId: string;
  surface: string;
  status: string;
  requestedAt: string;
}

// --- connectors -----------------------------------------------------------------

export interface Connector {
  id: string;
  type: string;
  label: string | null;
  description: string | null;
  enabled: boolean;
  status: { state: string; checkedAt: string | null; detail: string | null };
  source?: { type: 'catalog' | 'user'; id?: string };
  /** Present once resolved server-side — a real, current logo, not a
   *  hand-drawn mark. Absent while nothing has resolved yet. */
  iconDataUri?: string | null;
  config: { hasSecret: boolean };
  [key: string]: unknown;
}

export type ToolPermission = 'allow' | 'ask' | 'deny';

export interface ConnectorTool {
  /** The capability name permissions are keyed on (`label__tool`). */
  name: string;
  /** The app's own name for the tool — what a person reads. */
  title: string;
  description: string;
  /** The person's choice, and the final word: 'allow' runs without asking,
   *  'ask' asks every time, 'deny' never runs. A tool never set is 'ask'.
   *  Blocked tools are listed too, so they can be unblocked. */
  permission: ToolPermission;
}

export interface ConnectFlow {
  kind: string;
  guide?: {
    consoleLabel: string;
    note?: string;
    steps: string[];
  } | null;
  [key: string]: unknown;
}

/** One entry in the bundled directory of apps known to work — every entry
 *  gets one uniform Connect button, no "ready"/"needs setup" badge. */
export interface CatalogEntry {
  id: string;
  label: string;
  icon: string;
  description: string;
  connectFlow: ConnectFlow;
  /** Filled in only once the user has actually clicked into this entry at
   *  least once. */
  connectorId: string | null;
  status: string | null;
  iconDataUri: string | null;
}

export interface ConnectorConnectOutcome {
  ok: true;
  connectorId: string;
  /** No authorization was needed at all — the connector is already usable,
   *  no browser tab was opened. */
  noAuthNeeded?: true;
  /** Open this in a browser to finish signing in. */
  authUrl?: string;
  /** Automatic client registration failed or isn't supported — the guided
   *  Client ID/Secret form is what's needed next. */
  needsManualClient?: true;
  reason?: string;
  detail?: string;
  message?: string;
}

export interface SandboxStatus {
  backend: 'wsl' | 'restricted';
  isolation: 'strong' | 'weak';
  distro: string | null;
  detectedWindowsSandbox: boolean;
  setupSteps: string[];
}

export interface ExternalService {
  ref: string;
  label: string;
  configured: boolean;
  extraFieldLabel: string | null;
  extraFieldConfigured: boolean;
}

export interface VoiceEngineOption {
  id: 'pipeline' | 'duplex' | 'realtime' | string;
  label: string;
  description: string;
  available: boolean;
  /** Why not, whenever it is unavailable — never a bare "no". */
  reason: string | null;
  models?: { id: string; label: string }[];
}

export interface VoiceOption {
  id: string;
  label: string;
  configured: boolean;
  needsKey: boolean;
}

export interface VoiceOptions {
  engines: VoiceEngineOption[];
  voices: VoiceOption[];
  listening: { mode: string; serverProxied: boolean };
  connections: number;
}

// --- memory, and the profile notes that are one category of it ------------------

export interface Memory {
  id: string;
  category: string;
  text: string;
  /** How consent was given, which is a different question from where the
   *  content came from: `explicit` was typed by hand, `approved` was reviewed,
   *  `auto` cleared the confidence bar, `legacy` predates the distinction. */
  origin: 'approved' | 'auto' | 'explicit' | 'legacy';
  sourceKind: string | null;
  sourceRef: string | null;
  confidence: number | null;
  importance: number | null;
  archived: boolean;
  expiresAt: string | null;
  createdAt: string;
  updatedAt: string;
}

export interface MemoryCandidate {
  id: string;
  category: string;
  text: string;
  confidence: number | null;
  sourceKind: string | null;
  /** The id of a memory this one contradicts. A conflict always needs a person,
   *  at every trust level, with no override. */
  conflictWith: string | null;
  createdAt: string;
}

export interface MemoryVersion {
  id?: number;
  text: string;
  category?: string;
  changedAt: string;
  reason: string | null;
}

export interface MemoryCategory {
  name: string;
  status: 'approved' | 'pending';
}

/** A profile note: the same row as a memory, read as what it is on that screen. */
export interface ProfileEntry {
  id: string;
  text: string;
  addedAt: string;
}

// --- self-improvement -------------------------------------------------------------

export interface Proposal {
  id: string;
  kind: 'rule' | 'setting' | 'skill' | 'code' | 'idea';
  title: string;
  rationale: string | null;
  helpsJarvis: string | null;
  helpsUser: string | null;
  payload: Record<string, unknown> | null;
  evidence: string[];
  sourceTier: number;
  sourceUrl: string | null;
  conflictWith: string | null;
  status: 'pending' | 'approved' | 'rejected';
  implementationPrompt?: string | null;
  implementationTarget?: string | null;
  createdAt: string;
}

export interface Rule {
  id: string;
  text: string;
  scope: string;
  active: 0 | 1;
  sourceProposalId: string | null;
  archivedAt: string | null;
  createdAt: string;
  updatedAt: string;
}

export interface Lesson {
  id: string;
  kind: string;
  text: string;
  scope: string;
  evidence: string[];
  confidence: number | null;
  sourceTier: number;
  sourceUrl: string | null;
  status: 'active' | 'archived';
  createdAt: string;
}

export interface Change {
  id: number;
  kind: 'rule' | 'setting' | 'undo';
  target: string;
  before: Record<string, unknown> | null;
  after: Record<string, unknown> | null;
  reason: string | null;
  proposalId: string | null;
  appliedAt: string;
  undoneAt: string | null;
}

export interface Outcome {
  id: string;
  source: string;
  title: string;
  goal: string | null;
  status: string;
  error: string | null;
  createdAt: string;
}

export interface ImprovementStatus {
  enabled: boolean;
  trust: 'ask' | 'balanced' | 'auto';
  research: 'off' | 'weekly';
  dailyBudgetRemaining: number;
  weeklyBudgetRemaining: number;
  unreviewedOutcomes: number;
  pendingProposals: number;
}

/** An undo that would overwrite a later decision answers rather than failing. */
export type UndoResult =
  | { ok: true; change: Change }
  | { ok: false; reason: 'changed_since'; message: string; live: unknown; expected: unknown };

// --- background jobs ---------------------------------------------------------------

export interface Job {
  id: string;
  parentId: string | null;
  conversationId: string | null;
  title: string;
  goal: string;
  kind: string;
  /** What the backend actually writes. `done` and `stalled` are the two terminal
   *  states the worker sets; nothing ever emits "succeeded" or "failed". */
  status: 'queued' | 'running' | 'awaiting_decision' | 'stalled' | 'done' | 'cancelled';
  plan: { summary?: string; steps?: string[] } | null;
  resource: string | null;
  /** What the trace says about picking this up again — never a guess. */
  recovery: 'resumable' | 'restartable' | 'needs_input' | 'unrecoverable' | null;
  result: string | null;
  error: string | null;
  retries: number;
  progress: number | null;
  currentStep: string | null;
  priority: number;
  createdAt: string;
  startedAt: string | null;
  heartbeatAt: string | null;
  /** The specialist doing this job's work, when a specialist is. */
  agentId?: string | null;
  finishedAt: string | null;
}

/** One row of the write-ahead record: an intent before an action, an outcome
 *  after it, so a crash between the two still leaves the intent on record. */
export interface TraceRow {
  id: number;
  seq: number;
  phase: 'intent' | 'outcome';
  effect: 'read' | 'workspace' | 'external';
  kind: string;
  summary: string;
  detail: unknown;
  created_at: string;
}

export interface OutboxRow {
  id: number;
  tier: number;
  reason: string;
  summary: string;
  deliveredAt: string | null;
  createdAt: string;
}

// --- the morning briefing, and what is being watched for ----------------------------

export interface BriefingConfig {
  sections: {
    greeting: boolean;
    dateTime: boolean;
    tasks: boolean;
    goals: boolean;
    focus: boolean;
    custom: boolean;
  };
  customText: string;
  /** Empty means weather is skipped: it cannot be mentioned without a place. */
  weatherPlace: string;
  headlines: boolean;
  /** Connector ids, never tool names — a connector's tools change on reconnect. */
  connectors: string[];
}

export type BriefingPreview =
  | { ok: true; text: string; facts: Record<string, unknown>; modelId: string | null }
  | { ok: false; text: string; error: string; facts?: Record<string, unknown> };

export interface Monitor {
  id: string;
  description: string;
  status: 'watching' | 'stopped' | 'triggered' | 'error';
  check: Record<string, unknown>;
  onTrigger: Record<string, unknown>;
  createdAt: string;
  lastCheckedAt: string | null;
  triggeredAt: string | null;
  error: string | null;
}

// --- folder skills ------------------------------------------------------------------

/**
 * A Skill is a FOLDER OF INSTRUCTIONS, never a built-in ability under another
 * name. Everything here comes from the one backend function that reads folders
 * directly and structurally cannot return a built-in — which is why this type
 * has no "kind" discriminator to get wrong.
 */
export interface Skill {
  /** The folder name: what a model calls, and what is shown. It can never drift
   *  from where the Skill actually lives, even if its own file disagrees. */
  name: string;
  declaredName: string;
  description: string;
  hasSkillMd: boolean;
  hasToml: boolean;
  allowedTools: string[];
  enabled: boolean;
  source: { type: string; repo?: string; url?: string };
  installedAt: string | null;
  updatedAt: string | null;
  /** Whether this Skill's helper scripts may run in the sandbox. */
  scriptsApproved: boolean;
  /** Whether its pipeline may run. A different mechanism from the above, with a
   *  different default: true only for a Skill written and reviewed in the app. */
  pipelineApproved: boolean;
}

export interface SkillDetail extends Skill {
  ok: true;
  /** The whole file as written, and just the instructions under the frontmatter. */
  raw: string;
  body: string;
  supportingFiles: { name: string; size: number }[];
  pipeline: { description?: string; inputs?: unknown; steps?: unknown[] } | null;
  pipelineErrors: string[];
}

// --- specialist agents ---------------------------------------------------------

export interface AgentAccess {
  /** `all`: anything Jarvis has, every call still gated by its own risk. */
  mode: 'all' | 'selected';
  names: string[];
  /** Connector ids, or every connector set up. */
  connectors: 'all' | string[];
}

/** One specialist. A built-in one and a custom one are the same shape; `builtin`
 *  only decides whether it can be reset (built-in) or deleted (custom). */
export interface Agent {
  id: string;
  name: string;
  description: string;
  mission: string;
  doctrine: string;
  guardrails: string;
  /** A model id to always use, or null for whatever Jarvis is set to. */
  modelPin: string | null;
  capabilityAccess: AgentAccess;
  memoryAccess: 'none' | 'read';
  /** Who it may ask for help: `any`, or agent ids. */
  collaborators: 'any' | string[];
  enabled: boolean;
  builtin: boolean;
  createdAt: string;
  updatedAt: string;
  lastRun?: AgentRunSummary | null;
}

export type AgentDraft = Pick<Agent, 'name' | 'description' | 'mission' | 'doctrine' | 'guardrails'
  | 'modelPin' | 'capabilityAccess' | 'memoryAccess' | 'collaborators' | 'enabled'>;

export interface AgentRunSummary {
  id: string;
  agentId: string;
  status: 'running' | 'done' | 'failed' | 'awaiting_approval';
  task: string;
  /** `jarvis`, `operator` (a direct chat), `job`, `schedule`, or another agent's id. */
  requestedBy: string;
  startedAt: string;
  finishedAt: string | null;
  depth: number;
  parentRunId: string | null;
  rootRunId: string;
  error: string | null;
}

export interface AgentRun extends AgentRunSummary {
  agentName: string;
  result: string | null;
  modelId: string | null;
  toolsUsed: string[];
  conversationId: string | null;
  jobId: string | null;
}

export interface AgentNote {
  id: string;
  agentId: string;
  topic: string;
  text: string;
  createdAt: string;
  updatedAt: string;
}

/** What an agent's access can be made of — three separately-sourced groups. */
export interface AgentAbilities {
  builtIn: { name: string; description: string; risk: string }[];
  skills: { name: string; description: string }[];
  connectors: { id: string; label: string; type: string | null }[];
}

// --- Content Management ------------------------------------------------------------

export type ContentStage =
  'review' | 'changes_requested' | 'approved' | 'scheduling' | 'published' | 'archived';

export type PlacementStatus = 'draft' | 'scheduled' | 'queued' | 'publishing' | 'published' | 'failed';

export interface ContentMedia {
  fileId: string;
  name: string;
  mime: string;
  size: number;
  /** 'image' | 'video' | 'audio' | 'document' | 'unknown' */
  kind: string;
  url: string;
  role: 'primary' | 'slide' | 'thumbnail' | 'cover' | 'attachment';
  order: number;
}

export interface ContentPlacement {
  id: string;
  itemId: string;
  platform: string;
  platformLabel: string;
  destination: string;
  overrides: Record<string, string | string[]>;
  /** This platform's OWN files; a role it has none of uses the item's. */
  media: ContentMedia[];
  status: PlacementStatus;
  scheduledAt: string | null;
  timezone: string | null;
  /** Scheduled, and its time has come: waiting for a publisher to take it. */
  due: boolean;
  claimedBy: string | null;
  claimedAt: string | null;
  /** Claimed by a publisher that has said nothing for a long while. */
  stale: boolean;
  publishedAt: string | null;
  publishedUrl: string | null;
  failure: string | null;
  /** The latest numbers someone REPORTED for this post; null when nobody has. */
  metrics: ContentMetricsSnapshot | null;
  /** Every report, newest first — on the item's detail only, published posts only. */
  metricsHistory?: ContentMetricsSnapshot[];
  createdAt: string;
  updatedAt: string;
}

export interface ContentMetricsSnapshot {
  capturedAt: string;
  reportedAt?: string;
  source: string;
  values: Record<string, number>;
}

/** A file an item or a platform uses: one already stored (`fileId`) or one
 *  sent in the same request (`file`, the key it is uploaded under). */
export interface ContentMediaRef {
  fileId?: string;
  file?: string;
  role: ContentMedia['role'];
  order?: number;
}

/** What the person (or anyone) hands in. `readyToPost` is the screen's choice
 *  to approve it as it is added. */
export interface ContentDraft {
  name: string;
  contentType: string;
  niche?: string;
  producer?: string;
  fields?: Record<string, string | string[]>;
  media?: ContentMediaRef[];
  platforms?: { platform: string; destination?: string }[];
  readyToPost?: boolean;
}

export interface ContentJobState {
  id: string;
  status: string;
  error: string | null;
  currentStep: string | null;
  result: string | null;
}

export interface ContentChangeRequest {
  id: string;
  itemId: string;
  revision: number;
  what: string;
  why: string;
  assignee: 'agent' | 'jarvis';
  status: 'open' | 'in_progress' | 'resolved' | 'cancelled';
  pickedUpBy: string | null;
  pickedUpAt: string | null;
  jobId: string | null;
  startError: string | null;
  createdAt: string;
  resolvedAt: string | null;
  resolvedRevision: number | null;
  job?: ContentJobState | null;
}

export interface ContentItem {
  id: string;
  name: string;
  contentType: string;
  typeLabel: string;
  niche: string;
  stage: ContentStage;
  producer: string;
  revision: number;
  fields: Record<string, string | string[]>;
  media: ContentMedia[];
  findings: { level: 'note' | 'warning' | 'problem'; text: string }[];
  approvedAt: string | null;
  archivedAt: string | null;
  archivedFrom: ContentStage | null;
  deletedAt: string | null;
  createdAt: string;
  updatedAt: string;
  placements: ContentPlacement[];
  openRequest: ContentChangeRequest | null;
  /** Whether its text and files can still be changed (Review until it has gone out). */
  editable: boolean;
  /** What needs the person, in their words. Empty when nothing does. */
  attention: string[];
  /** The next thing that has to happen, and who is responsible for it. */
  next: { step: string; who: string };
}

export interface ContentItemDetail extends ContentItem {
  requests: ContentChangeRequest[];
  revisions: { revision: number; fields: Record<string, string | string[]>; media: ContentMedia[];
               by: string; note: string; createdAt: string }[];
  events: { at: string; actor: string; kind: string; note: string }[];
}

export interface ContentTypeInfo {
  label: string;
  render: 'video' | 'image' | 'slides' | 'text' | 'audio';
  media: 'primary' | 'slide' | null;
  fields: string[];
  assets: string[];
}

export interface ContentMeta {
  fields: Record<string, { label: string; kind: 'text' | 'longtext' | 'list' }>;
  assets: Record<string, string>;
  types: Record<string, ContentTypeInfo>;
  platforms: Record<string, { label: string; fields: string[]; accepts: string[] }>;
  stages: { id: ContentStage; label: string }[];
  niches: string[];
  metrics: Record<string, { label: string; kind: 'count' | 'seconds' | 'number' }>;
}

export interface ContentSummary {
  /** Items per stage. An item is counted under every stage one of its platforms is in. */
  counts: Record<ContentStage | 'bin' | 'active', number>;
  /** Platform posts in each post-approval stage. */
  posts: { approved: number; scheduling: number; published: number };
  attention: { toReview: number; revisionsReady: number; failedPosts: number; revisionsStuck: number };
}

export interface ContentAnalyticsPost {
  placementId: string;
  itemId: string;
  name: string;
  contentType: string;
  typeLabel: string;
  niche: string;
  platform: string;
  platformLabel: string;
  destination: string;
  publishedAt: string | null;
  publishedUrl: string | null;
  metrics: ContentMetricsSnapshot | null;
}

export interface ContentAnalytics {
  posts: ContentAnalyticsPost[];
  /** Sums of what was REPORTED — a post nobody reported adds nothing. */
  totals: Record<string, number>;
  reported: number;
  byPlatform: { platform: string; platformLabel: string; posts: number; reported: number;
                totals: Record<string, number> }[];
}

export interface ContentCalendarEntry extends ContentPlacement {
  itemName: string;
  contentType: string;
  typeLabel: string;
  niche: string;
}

/** One folder's card: what is still in play (not archived, not in the bin) by
 *  type, how many wait in Review, and what sits in the archive or the bin. */
export interface ContentFolder {
  total: number;
  byType: Record<string, number>;
  review: number;
  archived: number;
  binned: number;
  updatedAt: string | null;
}

export interface ContentNicheOverview {
  niches: (ContentFolder & { name: string; createdAt: string | null })[];
  none: ContentFolder;
  all: ContentFolder;
}

export interface ContentFilters {
  niche?: string;
  /** '1': only content with no niche. */
  noNiche?: '1';
  type?: string;
  platform?: string;
  q?: string;
}

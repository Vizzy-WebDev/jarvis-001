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

export interface Prefs {
  autoSelect: boolean;
  balance: 'fast' | 'balanced' | 'quality';
  manualModelId: string | null;
  clarifySensitivity: 'more' | 'balanced' | 'less';
  voiceModelId: string | null;
  ttsProvider: string | null;
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
}

/** One event from `GET /api/chat/stream`. The wire vocabulary is deliberately
 *  small and stable — see backend/jarvis/routes/turn.py's `to_wire`. */
export type TurnEvent =
  | { type: 'routed'; intent: string; fast: boolean; confidence: number; reason: string }
  | { type: 'chunk'; text: string }
  | { type: 'tool_result'; capability: string; ok: boolean; outcome: string; error: string | null;
      attachment?: { type: 'attachment'; kind: string; url: string; mimeType: string } }
  | { type: 'approval_required'; approvalId: string; capability: string; reason: string }
  | { type: 'model_switch'; to: string; from: string | null; reason: string }
  | { type: 'interrupted'; spokenText: string }
  | { type: 'progress'; phase: string }
  | { type: 'error'; error: string; code?: string; detail?: unknown }
  | { type: 'done'; text: string; steps: number }
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

// --- models, connections, connectors -------------------------------------------

export interface ModelEntry {
  id: string;
  connectionId: string;
  label: string;
  model: string;
  enabled: boolean;
  /** Computed at read time: enabled, and its connection has what it needs. */
  ready: boolean;
  hasSecret: boolean;
  caps?: Record<string, boolean>;
  tier?: Record<string, number>;
  [key: string]: unknown;
}

export interface ConnectionEntry {
  id: string;
  label: string;
  adapter: string;
  baseUrl: string | null;
  provider?: string;
  kind?: string;
  hasSecret: boolean;
  modelCount: number;
  [key: string]: unknown;
}

/** Why a model is being skipped right now, keyed by model id. A model nobody
 *  has had trouble with is simply absent. */
export type ModelHealth = Record<string, { reason: string | null; kind: string | null; retryInMs: number }>;

export interface Connector {
  id: string;
  type: string;
  label: string | null;
  description: string | null;
  enabled: boolean;
  status: { state: string; checkedAt: string | null; detail: string | null };
  config: { hasSecret: boolean };
  [key: string]: unknown;
}

export interface Provider {
  id: string;
  label: string;
  icon: string;
  iconBg: string;
  baseUrl: string | null;
  urlEditable: boolean;
  keyRequired: boolean;
  kind: string;
  suggestions?: string[];
  keyHint?: string;
}

/** One model a server says it has, before anything is added. */
export interface DiscoveredModel {
  model: string;
  label: string;
  contextTokens: number | null;
  billing: string | null;
}

/** What a probe tried, and what it found. `steps` is the point: a failure that
 *  cannot be explained is the exact problem this flow was built to fix. */
export interface ProbeResult {
  ok: boolean;
  steps: string[];
  adapter: string | null;
  baseUrl: string | null;
  kind: string | null;
  keyRequired: boolean | null;
  models: DiscoveredModel[];
  error: string | null;
  needsKey: boolean;
}

export interface ExternalService {
  ref: string;
  label: string;
  configured: boolean;
  extraFieldLabel: string | null;
  extraFieldConfigured: boolean;
}

export interface RecheckPreview {
  total: number;
  notWorking: number;
  byConnection: { id: string; label: string; count: number;
                  isFreeTier: boolean | null; remaining: number | null }[];
}

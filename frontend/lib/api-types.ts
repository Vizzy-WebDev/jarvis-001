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

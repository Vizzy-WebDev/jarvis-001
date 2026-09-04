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

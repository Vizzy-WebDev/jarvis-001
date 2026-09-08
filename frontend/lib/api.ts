// The one place the front end talks to the backend.
//
// Same-origin by design: in production FastAPI serves these files and the API
// from one port, and in development next.config.mjs rewrites /api to whichever
// backend is running. So nothing here needs a base URL, and there is no CORS.

import type {
  ConversationDetail,
  ConversationList,
  Notification,
  Prefs,
  Status,
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
      ...(init?.body ? { 'Content-Type': 'application/json' } : {}),
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
    remove: (id: string) => request<{ ok: true }>(`/conversations/${encodeURIComponent(id)}`, { method: 'DELETE' }),
    activate: (id: string) =>
      request<{ conversation: ConversationDetail['conversation'] }>(
        `/conversations/${encodeURIComponent(id)}/activate`,
        { method: 'POST' },
      ),
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
    remove: (id: string) =>
      request<{ ok: true }>(`/notifications/${encodeURIComponent(id)}`, { method: 'DELETE' }),
    clear: () => request<{ ok: true }>('/notifications', { method: 'DELETE' }),
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
      return (await response.json()) as { ok: true; id: string; name: string; size: number };
    },
  },
};

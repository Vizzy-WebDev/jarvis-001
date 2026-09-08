'use client';

import type { TurnEvent } from './api-types';

/**
 * One turn, streamed.
 *
 * `EventSource` rather than a fetch reader, because that is what the route is
 * built for: a GET with no body, which is also why the message rides in the
 * query string and attachments ride as ids.
 *
 * The stream ends on `done` or `error`; nothing here retries. A turn that
 * failed is something the user should see, not something to quietly attempt
 * again with their words a second time.
 */
export interface TurnOptions {
  message: string;
  attachments?: string[];
  source?: 'text' | 'voice';
  confidence?: number;
  onEvent: (event: TurnEvent) => void;
}

export interface RunningTurn {
  /** Stop listening. The server notices the disconnect and stops the turn. */
  cancel: () => void;
  /** Resolves when the turn is finished, one way or another. */
  done: Promise<void>;
}

export function streamTurn({
  message,
  attachments = [],
  source = 'text',
  confidence,
  onEvent,
}: TurnOptions): RunningTurn {
  const params = new URLSearchParams({ message });
  if (attachments.length) params.set('attachments', attachments.join(','));
  if (source !== 'text') params.set('source', source);
  if (typeof confidence === 'number') params.set('confidence', String(confidence));

  const events = new EventSource(`/api/chat/stream?${params}`);
  let settle: () => void = () => {};
  const done = new Promise<void>((resolve) => {
    settle = resolve;
  });

  const finish = () => {
    events.close();
    settle();
  };

  events.onmessage = (raw) => {
    let event: TurnEvent;
    try {
      event = JSON.parse(raw.data) as TurnEvent;
    } catch {
      return; // a frame we cannot read is not worth ending the turn over
    }
    onEvent(event);
    if (event.type === 'done' || event.type === 'error') finish();
  };

  events.onerror = () => {
    // EventSource reconnects by default; a turn is not a subscription, so a
    // dropped connection ends it rather than silently starting it again.
    if (events.readyState === EventSource.CLOSED) {
      onEvent({ type: 'error', error: 'The connection to Jarvis dropped.' });
    }
    finish();
  };

  return { cancel: finish, done };
}

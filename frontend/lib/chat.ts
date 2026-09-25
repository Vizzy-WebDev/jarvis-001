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
  /** Edit and Retry both ride this one parameter: the id of the user message
   *  to redo. The server cuts the conversation back to just before it, then
   *  this call's own `message`/`attachments` replay as if just sent — the
   *  original text for Retry, the revised text for Edit. */
  editOf?: string;
  /** Talking to a specialist directly: its id. Absent means Jarvis. */
  agent?: string;
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
  editOf,
  agent,
  onEvent,
}: TurnOptions): RunningTurn {
  const params = new URLSearchParams({ message });
  if (attachments.length) params.set('attachments', attachments.join(','));
  if (source !== 'text') params.set('source', source);
  if (typeof confidence === 'number') params.set('confidence', String(confidence));
  if (editOf) params.set('edit_of', editOf);
  if (agent) params.set('agent', agent);

  const events = new EventSource(`/api/chat/stream?${params}`);
  let settle: () => void = () => {};
  const done = new Promise<void>((resolve) => {
    settle = resolve;
  });

  let ended = false;
  const finish = () => {
    ended = true;
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
    // An approval request ENDS the turn: the server closes the stream right
    // after it, with no `done`. Not finishing here meant that close landed in
    // onerror below and every "Need approval" showed "The connection to Jarvis
    // dropped." under the approval card.
    if (event.type === 'done' || event.type === 'error' || event.type === 'approval_required') finish();
  };

  events.onerror = () => {
    // EventSource reconnects by default; a turn is not a subscription, so ANY
    // error ends it here. The readyState === CLOSED check this replaces almost
    // never passed: the browser is normally still CONNECTING (0) at the moment
    // onerror fires, because it is already queuing its own retry — so a refused
    // or dropped turn resolved silently and the user was shown nothing at all.
    if (ended) return; // a turn already settled by done/error/cancel is not a failure
    onEvent({ type: 'error', error: 'The connection to Jarvis dropped.' });
    finish();
  };

  return { cancel: finish, done };
}

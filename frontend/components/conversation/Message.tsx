'use client';

import type { TurnEvent } from '@/lib/api-types';

export type Turn = {
  id: string;
  role: 'user' | 'assistant' | 'note';
  text: string;
  /** Set while a reply is still streaming. */
  streaming?: boolean;
  /** A picture or a video a tool produced, shown in place. */
  attachment?: { kind: string; url: string; mimeType: string } | null;
  /** A barge-in cut it off; what is shown is what was actually heard. */
  interrupted?: boolean;
};

export function attachmentOf(event: TurnEvent): Turn['attachment'] {
  if (event.type !== 'tool_result' || !event.attachment) return null;
  const { kind, url, mimeType } = event.attachment;
  return { kind, url, mimeType };
}

/**
 * One turn in the transcript.
 *
 * The user's own words are the emphatic thing on the page, so they get the
 * accent tint; Jarvis's replies are plain, because they are the long ones. A
 * note (a model switch, a tone shift) is neither — it is quieter than both, and
 * never looks like something anyone said.
 */
export function Message({ turn }: { turn: Turn }) {
  if (turn.role === 'note') {
    return (
      <p className="animate-fade-up px-1 py-1 text-center text-[12px] italic text-ink-faint">
        {turn.text}
      </p>
    );
  }

  const mine = turn.role === 'user';
  return (
    <div className={`animate-fade-up flex ${mine ? 'justify-end' : 'justify-start'}`}>
      <div
        className={[
          'max-w-[88%] rounded-lg px-3.5 py-2.5 text-[14px] leading-relaxed',
          mine ? 'bg-bubble-user text-ink' : 'bg-bubble-assistant text-ink',
        ].join(' ')}
      >
        {turn.attachment?.kind === 'image' && (
          // eslint-disable-next-line @next/next/no-img-element -- a static export has no image optimiser
          <img
            src={turn.attachment.url}
            alt="Screenshot"
            className="mb-2 max-h-[320px] w-full rounded object-contain"
          />
        )}
        {turn.attachment?.kind === 'video' && (
          <video src={turn.attachment.url} controls className="mb-2 max-h-[320px] w-full rounded" />
        )}
        <p className="whitespace-pre-wrap break-words">
          {turn.text}
          {turn.streaming && <span className="ml-0.5 inline-block animate-pulse text-accent">▍</span>}
        </p>
        {turn.interrupted && (
          <p className="mt-1 text-[11px] italic text-ink-faint">interrupted</p>
        )}
      </div>
    </div>
  );
}

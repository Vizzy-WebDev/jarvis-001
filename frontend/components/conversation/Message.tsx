'use client';

import { Button } from '@/components/ui/Button';
import type { TurnEvent } from '@/lib/api-types';

export type Turn = {
  id: string;
  role: 'user' | 'assistant' | 'note' | 'approval';
  text: string;
  /** Set on an `approval` turn: the run is genuinely paused on this answer. */
  approvalId?: string;
  capability?: string;
  /** Once answered, what was decided — the card stays, as the record of it. */
  decided?: 'allow' | 'deny';
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
 *
 * An approval is the one turn that is a CONTROL rather than a record. The run
 * is stopped, waiting for this answer; showing it as a line of text the user
 * cannot act on would leave the whole turn stuck with no way out of it.
 */
export function Message({
  turn,
  onDecide,
}: {
  turn: Turn;
  onDecide?: (approvalId: string, decision: 'allow' | 'deny') => void;
}) {
  if (turn.role === 'approval') {
    return (
      <div
        data-testid="approval"
        className="animate-fade-up rounded-lg border border-accent/25 bg-accent/[0.06] p-3.5"
      >
        <p className="text-[13px] leading-relaxed text-ink">{turn.text}</p>
        {turn.decided ? (
          <p className="mt-2 text-[12px] text-ink-faint">
            {turn.decided === 'allow' ? 'You allowed this.' : 'You said no to this.'}
          </p>
        ) : (
          <div className="mt-3 flex gap-2">
            <Button
              tone="primary"
              data-testid="approve"
              onClick={() => turn.approvalId && onDecide?.(turn.approvalId, 'allow')}
            >
              Allow
            </Button>
            <Button
              data-testid="deny"
              onClick={() => turn.approvalId && onDecide?.(turn.approvalId, 'deny')}
            >
              Not now
            </Button>
          </div>
        )}
      </div>
    );
  }

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

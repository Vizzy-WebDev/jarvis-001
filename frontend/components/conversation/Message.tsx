'use client';

import { useEffect, useRef, useState } from 'react';

import { Button } from '@/components/ui/Button';
import { CloseIcon } from '@/components/ui/Icons';
import { IconButton } from '@/components/ui/IconButton';
import { isTopmost, popOverlay, pushOverlay } from '@/components/ui/overlay-stack';
import type { TurnEvent } from '@/lib/api-types';

// `mimeType` is optional: a tool-result attachment always carries one, but a
// USER-sent attachment's Turn is built from the composer's own upload
// response, which has no reason to know it — nothing here actually reads it.
type Attachment = { kind: string; url: string; mimeType?: string };

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
  /** A picture or a video a tool produced, shown in place. Assistant turns
   *  only ever carry one — a tool result is never more than one file. */
  attachment?: Attachment | null;
  /** What the USER attached and sent — genuinely plural, since the composer
   *  allows attaching more than one file to a single message. Kept as a
   *  separate field from `attachment` above rather than unifying them: the
   *  two have different producers (a tool result vs. what was actually sent)
   *  and forcing one shape on both would blur that. */
  attachments?: Attachment[];
  /** A barge-in cut it off; what is shown is what was actually heard. */
  interrupted?: boolean;
};

export function attachmentOf(event: TurnEvent): Turn['attachment'] {
  if (event.type !== 'tool_result' || !event.attachment) return null;
  const { kind, url, mimeType } = event.attachment;
  return { kind, url, mimeType };
}

/**
 * A picture or video, shown at bubble size, that expands to a real full-size
 * view on click — for either an assistant/tool attachment or one the user
 * sent. Previously static: nothing anywhere in the transcript was clickable.
 */
function AttachmentTile({ attachment }: { attachment: Attachment }) {
  const [open, setOpen] = useState(false);
  if (attachment.kind !== 'image' && attachment.kind !== 'video') return null;
  return (
    <>
      <button
        type="button"
        data-testid="attachment-tile"
        onClick={() => setOpen(true)}
        className="mb-2 block w-full cursor-zoom-in overflow-hidden rounded focus-visible:outline-none
                   focus-visible:ring-2 focus-visible:ring-accent/60"
      >
        {attachment.kind === 'image' ? (
          // eslint-disable-next-line @next/next/no-img-element -- a static export has no image optimiser
          <img src={attachment.url} alt="" className="max-h-[320px] w-full object-contain" />
        ) : (
          // Muted: a video that starts making noise the instant it appears in
          // a transcript is a worse surprise than the thumbnail not autoplaying.
          <video src={attachment.url} muted className="max-h-[320px] w-full object-contain" />
        )}
      </button>
      <Lightbox open={open} onClose={() => setOpen(false)} attachment={attachment} />
    </>
  );
}

/** The full-size view. Mirrors `ui/Modal.tsx`'s own Escape/backdrop/overlay-
 *  stack handling rather than reusing that component directly — a lightbox
 *  wants the image/video sized to the viewport, not `Modal`'s fixed 560px
 *  dialog width and title bar. */
function Lightbox({ open, onClose, attachment }: {
  open: boolean;
  onClose: () => void;
  attachment: Attachment;
}) {
  const closeRef = useRef(onClose);
  closeRef.current = onClose;

  useEffect(() => {
    if (!open) return;
    const token = pushOverlay();
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && isTopmost(token)) {
        event.stopPropagation();
        closeRef.current();
      }
    };
    window.addEventListener('keydown', onKey, true);
    return () => {
      window.removeEventListener('keydown', onKey, true);
      popOverlay(token);
    };
  }, [open]);

  if (!open) return null;
  return (
    <div className="fixed inset-0 z-[60] flex items-center justify-center p-5" data-testid="attachment-lightbox">
      <div className="absolute inset-0 bg-surface-overlay backdrop-blur-[2px]" onClick={onClose} aria-hidden />
      <div className="animate-fade-up relative max-h-[90vh] max-w-[90vw]">
        <IconButton
          label="Close"
          data-testid="lightbox-close"
          className="absolute -top-11 right-0 text-white hover:bg-white/10 hover:text-white"
          onClick={onClose}
        >
          <CloseIcon />
        </IconButton>
        {attachment.kind === 'image' ? (
          // eslint-disable-next-line @next/next/no-img-element -- a static export has no image optimiser
          <img src={attachment.url} alt="" className="max-h-[90vh] max-w-[90vw] rounded object-contain" />
        ) : (
          <video src={attachment.url} controls autoPlay className="max-h-[90vh] max-w-[90vw] rounded" />
        )}
      </div>
    </div>
  );
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
  // Normalised to one list either way, so rendering below never has to branch
  // on which field it came from — `attachments` (the user's, genuinely
  // plural) when set, else `attachment` (a tool's, at most one) as a single-
  // item list, else none.
  const shown = turn.attachments ?? (turn.attachment ? [turn.attachment] : []);
  return (
    <div className={`animate-fade-up flex ${mine ? 'justify-end' : 'justify-start'}`}>
      <div
        className={[
          'max-w-[88%] rounded-lg px-3.5 py-2.5 text-[14px] leading-relaxed',
          mine ? 'bg-bubble-user text-ink' : 'bg-bubble-assistant text-ink',
        ].join(' ')}
      >
        {shown.map((attachment, index) => (
          <AttachmentTile key={`${attachment.url}-${index}`} attachment={attachment} />
        ))}
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

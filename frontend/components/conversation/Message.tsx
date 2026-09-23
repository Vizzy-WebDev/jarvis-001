'use client';

import { useEffect, useRef, useState } from 'react';

import { Button } from '@/components/ui/Button';
import { CheckIcon, CloseIcon, CopyIcon, EditIcon, RetryIcon } from '@/components/ui/Icons';
import { IconButton } from '@/components/ui/IconButton';
import { isTopmost, popOverlay, pushOverlay } from '@/components/ui/overlay-stack';
import type { TurnEvent } from '@/lib/api-types';

// `mimeType` is optional: a tool-result attachment always carries one, but a
// USER-sent attachment's Turn is built from the composer's own upload
// response, which has no reason to know it — nothing here actually reads it.
type Attachment = { kind: string; url: string; mimeType?: string; name?: string };

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
  attachment?: Attachment | null;
  /** Everything the tools in this reply produced — pictures, but also files to
   *  open and voice-overs to play, several at once when a specialist hands back
   *  a whole package. Shown after `attachment`, never instead of it. */
  files?: Attachment[];
  /** Who is speaking, when it is a specialist the person is talking to directly
   *  rather than Jarvis. */
  speaker?: string;
  /** What the USER attached and sent — genuinely plural, since the composer
   *  allows attaching more than one file to a single message. Kept as a
   *  separate field from `attachment` above rather than unifying them: the
   *  two have different producers (a tool result vs. what was actually sent)
   *  and forcing one shape on both would blur that. */
  attachments?: Attachment[];
  /** A barge-in cut it off; what is shown is what was actually heard. */
  interrupted?: boolean;
  /** The turn failed and `text` is why — shown as a problem, not as Jarvis speaking. */
  failed?: boolean;
};

export function attachmentOf(event: TurnEvent): Turn['attachment'] {
  if (event.type !== 'tool_result' || !event.attachment) return null;
  const { kind, url, mimeType, name } = event.attachment;
  return { kind, url, mimeType, name };
}

/** Every attachment a tool result carried, one or several. */
export function attachmentsOf(event: TurnEvent): Attachment[] {
  if (event.type !== 'tool_result') return [];
  const all = event.attachments ?? (event.attachment ? [event.attachment] : []);
  return all.map(({ kind, url, mimeType, name }) => ({ kind, url, mimeType, name }));
}

/** A voice-over to play, or a file to open — the things a picture tile is not. */
function FileTile({ attachment }: { attachment: Attachment }) {
  const name = attachment.name || 'file';
  return (
    <div className="mb-2 rounded border border-surface-border bg-surface-base/40 px-2.5 py-2"
         data-testid={attachment.kind === 'audio' ? 'audio-tile' : 'file-tile'}>
      {attachment.kind === 'audio' && (
        <audio controls preload="none" src={attachment.url} className="mb-1.5 h-8 w-full" />
      )}
      <a href={attachment.url} download={name}
         className="block truncate text-[12px] text-accent hover:underline">
        {name}
      </a>
    </div>
  );
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

/** One small text+icon control in a message's own action row — Copy, Edit,
 *  Retry. Deliberately not `IconButton`: that one is sized for the header/
 *  composer's standalone 36px controls, and reads as oversized sitting under
 *  a message bubble at that size. */
function ActionButton({
  label,
  onClick,
  testId,
  children,
}: {
  label: string;
  onClick: () => void;
  testId: string;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      aria-label={label}
      title={label}
      data-testid={testId}
      onClick={onClick}
      className="inline-flex items-center gap-1 rounded px-1 py-0.5 text-ink-faint transition
                 duration-150 ease-out hover:text-ink focus-visible:outline-none
                 focus-visible:ring-2 focus-visible:ring-accent/60"
    >
      {children}
    </button>
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
  onEdit,
  onRetry,
}: {
  turn: Turn;
  onDecide?: (approvalId: string, decision: 'allow' | 'deny') => void;
  /** Present only for a user turn the caller can still act on — see
   *  `Transcript.tsx`'s wiring. Called with the revised text once the user
   *  confirms an edit; this component owns only the editing UI, not what
   *  happens next (truncate-and-resend lives in `app/page.tsx`). */
  onEdit?: (newText: string) => void;
  /** Present only on an assistant turn with a preceding user turn to redo,
   *  and not while still streaming. Regenerates this exchange from that
   *  user message's own text/attachments. */
  onRetry?: () => void;
}) {
  // Declared unconditionally, ahead of the early returns below (approval/note
  // turns never show these), so this component never violates the rule that
  // hooks run in the same order on every render regardless of `turn.role`.
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(turn.text);
  const [copied, setCopied] = useState(false);
  const copyTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => () => {
    if (copyTimer.current) clearTimeout(copyTimer.current);
  }, []);

  const copyText = () => {
    navigator.clipboard.writeText(turn.text).then(() => {
      setCopied(true);
      if (copyTimer.current) clearTimeout(copyTimer.current);
      copyTimer.current = setTimeout(() => setCopied(false), 1500);
    }).catch(() => undefined);
  };

  const startEdit = () => {
    setDraft(turn.text);
    setEditing(true);
  };

  const saveEdit = () => {
    const text = draft.trim();
    setEditing(false);
    if (text && text !== turn.text) onEdit?.(text);
  };

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
  const shown = [
    ...(turn.attachments ?? (turn.attachment ? [turn.attachment] : [])),
    ...(turn.files ?? []).filter((file) => file.url !== turn.attachment?.url),
  ];
  return (
    <div className={`animate-fade-up flex flex-col ${mine ? 'items-end' : 'items-start'}`}>
      <div
        role={turn.failed ? 'alert' : undefined}
        data-testid={turn.failed ? 'turn-error' : undefined}
        className={[
          'max-w-[88%] rounded-lg px-3.5 py-2.5 text-[14px] leading-relaxed',
          turn.failed
            ? 'border border-state-danger/30 bg-state-danger/[0.08] text-ink'
            : mine ? 'bg-bubble-user text-ink' : 'bg-bubble-assistant text-ink',
        ].join(' ')}
      >
        {!mine && turn.speaker && (
          <p className="mb-1 text-[11px] font-semibold uppercase tracking-[0.12em] text-accent"
             data-testid="turn-speaker">
            {turn.speaker}
          </p>
        )}
        {shown.map((attachment, index) => (
          attachment.kind === 'image' || attachment.kind === 'video'
            ? <AttachmentTile key={`${attachment.url}-${index}`} attachment={attachment} />
            : <FileTile key={`${attachment.url}-${index}`} attachment={attachment} />
        ))}
        {editing ? (
          <div className="min-w-[220px]">
            <textarea
              data-testid="edit-message-input"
              autoFocus
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === 'Enter' && !event.shiftKey) {
                  event.preventDefault();
                  saveEdit();
                } else if (event.key === 'Escape') {
                  setEditing(false);
                }
              }}
              rows={Math.min(8, Math.max(2, draft.split('\n').length))}
              className="w-full resize-none rounded border border-surface-border bg-surface-base/60
                         px-2 py-1.5 text-[14px] leading-relaxed text-ink outline-none
                         focus-visible:ring-2 focus-visible:ring-accent/60"
            />
            <div className="mt-1.5 flex justify-end gap-1.5">
              <Button data-testid="cancel-edit" onClick={() => setEditing(false)}>
                Cancel
              </Button>
              <Button tone="primary" data-testid="save-edit" onClick={saveEdit}>
                Save &amp; resend
              </Button>
            </div>
          </div>
        ) : (
          <p className="whitespace-pre-wrap break-words">
            {turn.text}
            {turn.streaming && <span className="ml-0.5 inline-block animate-pulse text-accent">▍</span>}
          </p>
        )}
        {turn.interrupted && (
          <p className="mt-1 text-[11px] italic text-ink-faint">interrupted</p>
        )}
      </div>
      {!editing && !turn.streaming && (
        <div className="mt-1 flex gap-0.5 px-1">
          <ActionButton label="Copy" testId="copy-message" onClick={copyText}>
            {copied ? <CheckIcon className="h-3.5 w-3.5" /> : <CopyIcon className="h-3.5 w-3.5" />}
          </ActionButton>
          {onEdit && (
            <ActionButton label="Edit and resend" testId="edit-message" onClick={startEdit}>
              <EditIcon className="h-3.5 w-3.5" />
            </ActionButton>
          )}
          {onRetry && (
            <ActionButton label="Retry" testId="retry-message" onClick={onRetry}>
              <RetryIcon className="h-3.5 w-3.5" />
            </ActionButton>
          )}
        </div>
      )}
    </div>
  );
}

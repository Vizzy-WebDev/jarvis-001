'use client';

import { useEffect, useRef, useState } from 'react';

import { CloseIcon } from '@/components/ui/Icons';
import { IconButton } from '@/components/ui/IconButton';
import { isTopmost, popOverlay, pushOverlay } from '@/components/ui/overlay-stack';
import { ArtifactCard } from '@/components/artifacts/ArtifactCard';
import type { TurnEvent } from '@/lib/api-types';
import { artifactIdFromUrl } from '@/lib/artifacts';

// `mimeType` is optional: a tool-result attachment always carries one, but a
// USER-sent attachment's Turn is built from the composer's own upload
// response, which has no reason to know it — nothing here actually reads it.
export type Attachment = {
  kind: string; url: string; mimeType?: string; name?: string;
  /** A saved artifact: the card opens it in the viewer instead of only downloading. */
  artifactId?: string; title?: string; artifactKind?: string; size?: number;
};

export type Turn = {
  id: string;
  role: 'user' | 'assistant' | 'note' | 'approval';
  text: string;
  /** When it was said (ISO). Absent only on a note. */
  at?: string;
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
  return pick(event.attachment);
}

function pick({ kind, url, mimeType, name, artifactId, title, artifactKind, size }: Attachment): Attachment {
  return { kind, url, mimeType, name, artifactId, title, artifactKind, size };
}

/** Every attachment a tool result carried, one or several. */
export function attachmentsOf(event: TurnEvent): Attachment[] {
  if (event.type !== 'tool_result') return [];
  const all = event.attachments ?? (event.attachment ? [event.attachment] : []);
  return all.map(pick);
}

/** A file's extension, the way a tile labels it. */
export function extensionOf(name: string): string {
  const dot = name.lastIndexOf('.');
  return dot > 0 ? name.slice(dot + 1).slice(0, 5) : 'file';
}

/** A voice-over to play, or a file to open — the things a picture tile is not. */
function FileTile({ attachment }: { attachment: Attachment }) {
  const artifactId = attachment.artifactId ?? artifactIdFromUrl(attachment.url);
  if (artifactId && attachment.kind !== 'audio') {
    return <ArtifactCard card={{ ...attachment, artifactId }} />;
  }
  const name = attachment.name || 'file';
  if (attachment.kind === 'audio') {
    return (
      <div className="rounded-lg border border-line/[0.22] bg-[rgb(8_12_18/0.6)] px-2.5 py-2" data-testid="audio-tile">
        <audio controls preload="none" src={attachment.url} className="mb-1.5 h-8 w-full" />
        <a href={attachment.url} download={name} className="block truncate text-[12px] hover:underline">{name}</a>
      </div>
    );
  }
  return (
    <a href={attachment.url} download={name} data-testid="file-tile" title={`Open ${name}`}
       className="flex max-w-[220px] items-center gap-2 rounded-lg border border-line/[0.22] bg-[rgb(8_12_18/0.6)] px-2.5 py-1.5
                  text-[12px] text-ink-soft hover:border-line/[0.45] hover:text-ink-strong">
      <span className="font-mono text-[9.5px] uppercase tracking-[0.1em] text-ink-muted">{extensionOf(name)}</span>
      <span className="truncate">{name}</span>
    </a>
  );
}

/**
 * A picture or video at thumbnail size that opens a real full-size view — for
 * an attachment Jarvis produced or one the person sent.
 */
function AttachmentTile({ attachment }: { attachment: Attachment }) {
  const [open, setOpen] = useState(false);
  if (attachment.kind !== 'image' && attachment.kind !== 'video') return null;
  return (
    <>
      <button
        type="button"
        data-testid="attachment-tile"
        title={attachment.name ? `Open ${attachment.name}` : 'Open'}
        onClick={() => setOpen(true)}
        className="block cursor-zoom-in overflow-hidden rounded-lg border border-line/[0.22] focus-visible:outline-none
                   focus-visible:ring-2 focus-visible:ring-accent/60"
      >
        {attachment.kind === 'image' ? (
          // eslint-disable-next-line @next/next/no-img-element -- a static export has no image optimiser
          <img src={attachment.url} alt={attachment.name ?? ''} className="block h-[90px] w-[120px] object-cover" />
        ) : (
          // Muted: a video that starts making noise the instant it appears in
          // a transcript is a worse surprise than the thumbnail not autoplaying.
          <video src={attachment.url} muted className="block h-[90px] w-[120px] object-cover" />
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
      <div className="absolute inset-0 bg-[rgb(2_3_5/0.84)]" onClick={onClose} aria-hidden />
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

/** One small control in a message's action row (Copy, Edit, Retry). */
function Action({ label, onClick, testId, lit, children }: {
  label: string;
  onClick: () => void;
  testId: string;
  lit?: boolean;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      aria-label={label}
      title={label}
      data-testid={testId}
      onClick={onClick}
      className="flex h-[26px] w-7 items-center justify-center rounded-md text-ink-muted transition-colors duration-150
                 hover:text-white focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/60"
      style={{ background: lit ? 'rgb(var(--line) / 0.1)' : undefined }}
    >
      {children}
    </button>
  );
}

const ICON = 'h-[15px] w-[15px] fill-none stroke-current stroke-[1.8]';
const CopyGlyph = () => (
  <svg viewBox="0 0 24 24" aria-hidden className={ICON} strokeLinecap="round" strokeLinejoin="round">
    <rect x="9" y="9" width="11" height="11" rx="2" /><path d="M5 15V6a2 2 0 0 1 2-2h9" />
  </svg>
);
const EditGlyph = () => (
  <svg viewBox="0 0 24 24" aria-hidden className={ICON} strokeLinecap="round" strokeLinejoin="round">
    <path d="M12 20h9M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z" />
  </svg>
);
const RetryGlyph = () => (
  <svg viewBox="0 0 24 24" aria-hidden className={ICON} strokeLinecap="round" strokeLinejoin="round">
    <path d="M3 12a9 9 0 0 1 15.5-6.2L21 8M21 3v5h-5M21 12a9 9 0 0 1-15.5 6.2L3 16M3 21v-5h5" />
  </svg>
);

/** "08:14" today, "Fri 23:02" this week, a date before that. */
export function stamp(at?: string): string {
  if (!at) return '';
  const d = new Date(at);
  if (Number.isNaN(d.getTime())) return '';
  const time = d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
  const now = new Date();
  if (d.toDateString() === now.toDateString()) return time;
  if (now.getTime() - d.getTime() < 6 * 86400_000) {
    return `${d.toLocaleDateString(undefined, { weekday: 'short' })} ${time}`;
  }
  return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}

/**
 * One turn in the transcript (design Home v6 + 1h).
 *
 * The person's words sit right with a "You" header and their own avatar;
 * Jarvis's sit left behind its "J" mark, with who is speaking — Jarvis, or the
 * specialist being talked to directly. Under a sent message: Copy, Edit (which
 * loads it into the composer, "Editing message") and Retry (the same message,
 * a fresh reply); under a reply: Copy and Retry.
 *
 * An approval is the one turn that is a CONTROL rather than a record. The run
 * is stopped, waiting for this answer; showing it as a line of text the user
 * cannot act on would leave the whole turn stuck with no way out of it.
 */
export function Message({
  turn,
  editing,
  onDecide,
  onEdit,
  onRetry,
}: {
  turn: Turn;
  /** This turn is the one loaded into the composer for editing. */
  editing?: boolean;
  onDecide?: (approvalId: string, decision: 'allow' | 'deny') => void;
  /** Present only for a user turn the caller can still act on. */
  onEdit?: () => void;
  /** Present when there is a user message to redo from (this one, or the one
   *  this reply answered), and not while still streaming. */
  onRetry?: () => void;
}) {
  const [copied, setCopied] = useState(false);
  const copyTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => () => { if (copyTimer.current) clearTimeout(copyTimer.current); }, []);

  const copyText = () => {
    navigator.clipboard.writeText(turn.text).then(() => {
      setCopied(true);
      if (copyTimer.current) clearTimeout(copyTimer.current);
      copyTimer.current = setTimeout(() => setCopied(false), 1500);
    }).catch(() => undefined);
  };

  if (turn.role === 'approval') {
    return (
      <div data-testid="approval"
           className="animate-fade-up ml-[38px] flex flex-col gap-2.5 rounded-xl border border-[rgb(127_184_230/0.3)] bg-[rgb(127_184_230/0.07)] px-3.5 py-3">
        <p className="text-[13.5px] leading-relaxed text-ink-strong">{turn.text}</p>
        {turn.decided ? (
          <p className="text-[12px] text-ink-muted">
            {turn.decided === 'allow' ? 'You allowed this.' : 'You said no to this.'}
          </p>
        ) : (
          <div className="flex gap-2">
            <button type="button" data-testid="approve"
                    onClick={() => turn.approvalId && onDecide?.(turn.approvalId, 'allow')}
                    className="h-8 rounded-full border border-[rgb(127_184_230/0.4)] bg-[rgb(63_127_174/0.35)] px-4 text-[13px] text-ink-strong hover:bg-[rgb(63_127_174/0.6)]">
              Allow
            </button>
            <button type="button" data-testid="deny"
                    onClick={() => turn.approvalId && onDecide?.(turn.approvalId, 'deny')}
                    className="h-8 rounded-full border border-line/25 px-4 text-[13px] text-ink-soft hover:bg-line/10">
              Not now
            </button>
          </div>
        )}
      </div>
    );
  }

  if (turn.role === 'note') {
    return <p className="animate-fade-up self-center text-center text-[12px] italic text-ink-muted">{turn.text}</p>;
  }

  const mine = turn.role === 'user';
  // Normalised to one list either way: `attachments` (the user's) when set,
  // else `attachment` (a tool's, at most one), then everything else it made.
  const shown = [
    ...(turn.attachments ?? (turn.attachment ? [turn.attachment] : [])),
    ...(turn.files ?? []).filter((file) => file.url !== turn.attachment?.url),
  ];
  const media = shown.length > 0 && (
    <div className="flex flex-wrap gap-1.5 pt-0.5">
      {shown.map((attachment, index) => (
        attachment.kind === 'image' || attachment.kind === 'video'
          ? <AttachmentTile key={`${attachment.url}-${index}`} attachment={attachment} />
          : <FileTile key={`${attachment.url}-${index}`} attachment={attachment} />
      ))}
    </div>
  );
  const body = (turn.text || turn.streaming || !shown.length) && (
    <p className="whitespace-pre-wrap break-words text-[13.5px] leading-[1.55] text-[#dce4ee]"
       style={{ opacity: turn.streaming && !turn.text ? 0.5 : 1 }}>
      {turn.text || (turn.streaming ? 'Thinking…' : '')}
      {turn.streaming && turn.text && <span className="ml-0.5 inline-block animate-pulse text-accent">▍</span>}
    </p>
  );
  const done = !turn.streaming && (turn.text || mine);

  if (mine) {
    return (
      <div className="animate-fade-up flex max-w-[90%] flex-col items-end gap-1 self-end">
        <div className="flex items-start gap-2.5">
          <div className="flex min-w-0 flex-col gap-1.5 rounded-xl border bg-bubble-user px-3.5 py-2.5"
               style={{ borderColor: editing ? 'rgb(245 165 36 / 0.55)' : 'rgb(var(--line) / 0.2)' }}>
            <div className="flex justify-between gap-6 text-[11.5px]">
              <span className="font-medium text-ink-strong">You</span>
              <span className="text-ink-muted">{stamp(turn.at)}</span>
            </div>
            {media}
            {body}
          </div>
          <span aria-hidden className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full border border-line/30 bg-line/[0.08]">
            <svg viewBox="0 0 24 24" className="h-3.5 w-3.5 fill-none stroke-[#9fb2c6] stroke-[1.8]" strokeLinecap="round">
              <circle cx="12" cy="8" r="4" /><path d="M4 21a8 8 0 0 1 16 0" />
            </svg>
          </span>
        </div>
        {done && (
          <div className="mr-[38px] flex items-center gap-1.5">
            {copied && <span className="mr-1 text-[11.5px] text-state-ok">Copied</span>}
            <Action label="Copy" testId="copy-message" onClick={copyText} lit={copied}><CopyGlyph /></Action>
            {onEdit && <Action label="Edit" testId="edit-message" onClick={onEdit} lit={editing}><EditGlyph /></Action>}
            {onRetry && <Action label="Retry" testId="retry-message" onClick={onRetry}><RetryGlyph /></Action>}
          </div>
        )}
      </div>
    );
  }

  return (
    <div className="animate-fade-up flex flex-col gap-1">
      <div className="flex items-start gap-2.5">
        <span aria-hidden
              className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-[radial-gradient(circle_at_35%_30%,#ff6a5f,#b3140c)]
                         text-[13px] font-semibold text-white shadow-[0_0_12px_rgba(255,59,48,0.45)]">
          J
        </span>
        <div
          role={turn.failed ? 'alert' : undefined}
          data-testid={turn.failed ? 'turn-error' : undefined}
          className={[
            'flex min-w-0 flex-1 flex-col gap-1 rounded-xl border px-3.5 py-2.5',
            turn.failed ? 'border-state-danger/30 bg-state-danger/[0.08]' : 'border-line/[0.16] bg-bubble-assistant',
          ].join(' ')}
        >
          <div className="flex gap-2.5 text-[11.5px]">
            <span className="font-semibold tracking-[0.12em] text-[#ff5a4f]" data-testid={turn.speaker ? 'turn-speaker' : undefined}>
              {(turn.speaker ?? 'Jarvis').toUpperCase()}
            </span>
            <span className="text-ink-muted">{stamp(turn.at)}</span>
          </div>
          {body}
          {media}
          {turn.interrupted && <span className="text-[11.5px] italic text-ink-muted">interrupted</span>}
        </div>
      </div>
      {done && (
        <div className="ml-[38px] flex items-center gap-1.5">
          <Action label="Copy" testId="copy-message" onClick={copyText} lit={copied}><CopyGlyph /></Action>
          {onRetry && <Action label="Retry" testId="retry-message" onClick={onRetry}><RetryGlyph /></Action>}
          {copied && <span className="text-[11.5px] text-state-ok">Copied</span>}
        </div>
      )}
    </div>
  );
}

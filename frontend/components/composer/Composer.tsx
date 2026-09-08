'use client';

import { useEffect, useRef, useState, type ChangeEvent, type FormEvent, type KeyboardEvent } from 'react';

import { AttachIcon, CloseIcon, MicIcon, SendIcon } from '@/components/ui/Icons';
import { IconButton } from '@/components/ui/IconButton';
import { api, ApiRequestError } from '@/lib/api';

export interface Attachment {
  id: string;
  name: string;
  size: number;
  /** A local preview for a picture, so a tile shows the actual image. Made with
   *  `createObjectURL` and revoked when the tile goes — the file is already on
   *  the server, this is only what the user looks at while they type. */
  preview?: string;
}

/**
 * The bottom band of the conversation panel — part of it, not a separate
 * container attached to it.
 *
 * **Three rows, in this order, and the order is load-bearing**: the files
 * chosen but not yet sent, the text on a line of its own, then the controls on
 * a permanent line below it — attach at the left, dictation and send at the
 * right. The original put the controls on their own wrapped line for a real
 * reason its own comment records: sharing a line with the textarea is what
 * made a grown message clip the control row off. A narrow panel makes it worse
 * again, since four inline controls leave almost no room to type.
 *
 * **Attachments scroll SIDEWAYS.** They are one row that never wraps, so
 * attaching a tenth file cannot grow the composer downwards into the
 * conversation. The original wrapped them into a vertical stack with its own
 * scroll cap, which is the behaviour this deliberately replaces.
 *
 * Anything can be attached; there is no `accept` filter, deliberately. An
 * upload returns an id and only the id ever reaches a turn.
 */
export function Composer({
  disabled,
  busy,
  onSend,
}: {
  disabled: boolean;
  busy: boolean;
  onSend: (text: string, attachments: string[]) => void;
}) {
  const [text, setText] = useState('');
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const textRef = useRef<HTMLTextAreaElement>(null);

  // A preview URL is a real allocation; letting them pile up over a long
  // session is a leak the browser cannot clean up on its own.
  const liveRef = useRef<Attachment[]>([]);
  liveRef.current = attachments;
  useEffect(() => () => liveRef.current.forEach(revoke), []);

  const grow = (el: HTMLTextAreaElement) => {
    el.style.height = 'auto';
    el.style.height = `${el.scrollHeight}px`;
  };

  async function addFiles(event: ChangeEvent<HTMLInputElement>) {
    const chosen = Array.from(event.target.files ?? []);
    event.target.value = '';
    if (!chosen.length) return;
    setUploading(true);
    setError(null);
    try {
      for (const file of chosen) {
        const saved = await api.uploads.create(file);
        setAttachments((current) => [...current, {
          id: saved.id,
          name: saved.name,
          size: saved.size,
          preview: file.type.startsWith('image/') ? URL.createObjectURL(file) : undefined,
        }]);
      }
    } catch (err) {
      setError(err instanceof ApiRequestError ? err.message : 'That file could not be attached.');
    } finally {
      setUploading(false);
    }
  }

  function remove(id: string) {
    setAttachments((current) => {
      current.filter((a) => a.id === id).forEach(revoke);
      return current.filter((a) => a.id !== id);
    });
  }

  function submit(event?: FormEvent) {
    event?.preventDefault();
    const message = text.trim();
    if ((!message && attachments.length === 0) || disabled || busy) return;
    onSend(message, attachments.map((a) => a.id));
    attachments.forEach(revoke);
    setText('');
    setAttachments([]);
    if (textRef.current) textRef.current.style.height = 'auto';
  }

  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    // Enter sends; Shift+Enter is a new line. A message is usually one line,
    // and reaching for a button to send each one is the wrong default.
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      submit();
    }
  }

  const nothingToSend = !text.trim() && attachments.length === 0;

  return (
    // No ground and no border of its own: this is the bottom band of the
    // conversation panel, not a container sitting beneath one.
    <div className="shrink-0 px-2.5 pb-2.5 pt-2">
      {attachments.length > 0 && (
        <div
          data-testid="attachments"
          className="scroll-quiet mb-2 flex gap-2 overflow-x-auto overflow-y-hidden pb-1.5"
        >
          {attachments.map((file) => (
            <div
              key={file.id}
              title={file.name}
              className="relative shrink-0 overflow-hidden rounded border border-surface-border bg-surface-raised"
              style={{ width: 'var(--composer-tile)', height: 'var(--composer-tile)' }}
            >
              {file.preview ? (
                // eslint-disable-next-line @next/next/no-img-element -- a local object URL, and a static export has no optimiser
                <img src={file.preview} alt={file.name} className="h-full w-full object-cover" />
              ) : (
                <>
                  <p className="line-clamp-3 break-words p-2 pr-6 text-[11px] leading-tight text-ink-muted">
                    {file.name}
                  </p>
                  <span className="absolute bottom-1.5 left-2 text-[9px] font-semibold uppercase tracking-[0.1em] text-ink-faint">
                    {extensionOf(file.name)}
                  </span>
                </>
              )}
              <button
                type="button"
                aria-label={`Remove ${file.name}`}
                onClick={() => remove(file.id)}
                // Inside the tile, not hanging off its corner: this row scrolls,
                // and a scroll container clips anything outside its child.
                className="absolute right-1 top-1 flex h-5 w-5 items-center justify-center rounded-full
                           bg-surface/80 text-ink-muted backdrop-blur transition hover:bg-surface hover:text-ink"
              >
                <CloseIcon className="h-3 w-3" />
              </button>
            </div>
          ))}
        </div>
      )}

      {error && <p className="px-1 pb-1.5 text-[12px] text-state-danger">{error}</p>}

      <form onSubmit={submit} autoComplete="off">
        <textarea
          ref={textRef}
          rows={1}
          value={text}
          data-testid="composer-input"
          onChange={(event) => {
            setText(event.target.value);
            grow(event.target);
          }}
          onKeyDown={onKeyDown}
          placeholder={uploading ? 'Attaching…' : 'Message Jarvis…'}
          className="scroll-quiet block min-h-[34px] w-full resize-none bg-transparent px-1.5 py-1.5
                     text-[14px] leading-relaxed text-ink outline-none placeholder:text-ink-faint"
          style={{ maxHeight: 'var(--composer-text-max)' }}
        />

        {/* Its own line, permanently. See this file's own note on why. */}
        <div data-testid="composer-controls" className="mt-1 flex items-center gap-1">
          <IconButton
            label="Attach a file"
            data-testid="attach"
            onClick={() => fileRef.current?.click()}
            disabled={uploading}
          >
            <AttachIcon />
          </IconButton>
          <input ref={fileRef} type="file" multiple hidden onChange={addFiles} />

          <div className="ml-auto flex items-center gap-1">
            <IconButton
              label="Speak instead of typing"
              disabled
              title="Dictation lands with the voice engines"
            >
              <MicIcon />
            </IconButton>
            <IconButton
              label="Send"
              type="submit"
              data-testid="send"
              disabled={disabled || busy || nothingToSend}
              className="bg-accent/15 text-accent hover:bg-accent/25 hover:text-accent"
            >
              <SendIcon />
            </IconButton>
          </div>
        </div>
      </form>
    </div>
  );
}

function revoke(file: Attachment) {
  if (file.preview) URL.revokeObjectURL(file.preview);
}

function extensionOf(name: string): string {
  const dot = name.lastIndexOf('.');
  return dot > 0 ? name.slice(dot + 1).slice(0, 5) : 'file';
}

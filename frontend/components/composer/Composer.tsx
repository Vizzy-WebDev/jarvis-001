'use client';

import { useRef, useState, type ChangeEvent, type FormEvent, type KeyboardEvent } from 'react';

import { AttachIcon, CloseIcon, MicIcon, SendIcon } from '@/components/ui/Icons';
import { IconButton } from '@/components/ui/IconButton';
import { api, ApiRequestError } from '@/lib/api';

export interface Attachment {
  id: string;
  name: string;
  size: number;
}

/**
 * Where a message is written.
 *
 * The bottom band of the conversation panel — part of it, not a separate
 * container attached to it. It holds everything that goes into sending: the
 * files chosen but not yet sent, the attach button, the text itself, the
 * dictation mic (speaking instead of typing — not Jarvis's own voice control)
 * and send. Each region is capped on its own, so this band's height is bounded
 * by construction and never needs a scrollbar.
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
        setAttachments((current) => [...current, { id: saved.id, name: saved.name, size: saved.size }]);
      }
    } catch (err) {
      setError(err instanceof ApiRequestError ? err.message : 'That file could not be attached.');
    } finally {
      setUploading(false);
    }
  }

  function submit(event?: FormEvent) {
    event?.preventDefault();
    const message = text.trim();
    if ((!message && attachments.length === 0) || disabled || busy) return;
    onSend(message, attachments.map((a) => a.id));
    setText('');
    setAttachments([]);
    if (textRef.current) {
      textRef.current.style.height = 'auto';
    }
  }

  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    // Enter sends; Shift+Enter is a new line. A message is usually one line,
    // and reaching for a button to send each one is the wrong default.
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      submit();
    }
  }

  return (
    // No ground and no border of its own: this is the bottom band of the
    // conversation panel, not a container sitting beneath one.
    <div className="shrink-0">
      {attachments.length > 0 && (
        <div
          className="scroll-quiet flex flex-wrap gap-2 overflow-y-auto px-3 pt-3"
          style={{ maxHeight: 'var(--composer-chips-max)' }}
        >
          {attachments.map((file) => (
            <span
              key={file.id}
              className="flex items-center gap-1.5 rounded-pill bg-white/[0.06] py-1 pl-3 pr-1 text-[12px] text-ink-muted"
            >
              <span className="max-w-[180px] truncate">{file.name}</span>
              <button
                type="button"
                aria-label={`Remove ${file.name}`}
                onClick={() => setAttachments((current) => current.filter((a) => a.id !== file.id))}
                className="rounded-full p-0.5 text-ink-faint transition hover:bg-white/10 hover:text-ink"
              >
                <CloseIcon className="h-3.5 w-3.5" />
              </button>
            </span>
          ))}
        </div>
      )}

      {error && <p className="px-4 pt-2 text-[12px] text-state-danger">{error}</p>}

      <form onSubmit={submit} autoComplete="off" className="flex items-end gap-1 p-2">
        <IconButton
          label="Attach a file"
          onClick={() => fileRef.current?.click()}
          disabled={uploading}
        >
          <AttachIcon />
        </IconButton>
        <input ref={fileRef} type="file" multiple hidden onChange={addFiles} />

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
          className="scroll-quiet min-h-[38px] flex-1 resize-none bg-transparent px-2 py-2 text-[14px]
                     text-ink outline-none placeholder:text-ink-faint"
          style={{ maxHeight: 'var(--composer-text-max)' }}
        />

        <IconButton label="Speak instead of typing" disabled title="Dictation lands with the voice engines">
          <MicIcon />
        </IconButton>
        <IconButton
          label="Send"
          type="submit"
          data-testid="send"
          disabled={disabled || busy || (!text.trim() && attachments.length === 0)}
          className="bg-accent/15 text-accent hover:bg-accent/25 hover:text-accent"
        >
          <SendIcon />
        </IconButton>
      </form>
    </div>
  );
}

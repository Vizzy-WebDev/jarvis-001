'use client';

import { useEffect, useMemo, useRef } from 'react';

import { Button } from '@/components/ui/Button';
import type { ContentMedia, ContentMediaRef, ContentMeta, ContentTypeInfo } from '@/lib/api-types';

type Role = ContentMedia['role'];

/** One file in an editor: already stored (`fileId`) or picked just now (`file`). */
export interface FileEntry {
  key: string;
  role: Role;
  fileId?: string;
  file?: File;
  name: string;
  kind: string;
  url?: string;
}

let counter = 0;
const nextKey = () => `upload${(counter += 1)}`;

export function entriesFrom(media: ContentMedia[]): FileEntry[] {
  return [...media]
    .sort((a, b) => (a.role === b.role ? a.order - b.order : a.role.localeCompare(b.role)))
    .map((m) => ({ key: m.fileId, role: m.role, fileId: m.fileId, name: m.name, kind: m.kind, url: m.url }));
}

/** What the API takes: the list, plus the files to upload with it. */
export function toRequest(entries: FileEntry[]): { media: ContentMediaRef[]; uploads: { key: string; file: File }[] } {
  const orders: Record<string, number> = {};
  const media = entries.map((e) => {
    const order = orders[e.role] ?? 0;
    orders[e.role] = order + 1;
    return e.fileId ? { fileId: e.fileId, role: e.role, order } : { file: e.key, role: e.role, order };
  });
  const uploads = entries.filter((e) => e.file).map((e) => ({ key: e.key, file: e.file! }));
  return { media, uploads };
}

function kindOf(file: File): string {
  const type = file.type.split('/')[0];
  return type === 'image' || type === 'video' || type === 'audio' ? type : 'document';
}

const ACCEPT: Record<string, string> = {
  video: 'video/*', image: 'image/*', audio: 'audio/*', slides: 'image/*,video/*',
};

/** The roles an item of this type has: the content itself, then its assets. */
export function rolesFor(info: ContentTypeInfo | undefined): Role[] {
  if (!info) return [];
  return [...(info.media ? [info.media] : []), ...(info.assets as Role[])];
}

const MANY: Role[] = ['slide', 'attachment'];

/**
 * The files of an item — or of one platform's own version of it — edited as
 * slots, one per role the content type has: the content itself (a file, or
 * slides in order) and its supporting assets (thumbnail, cover, attachment).
 *
 * With `shared`, this is a PLATFORM's own files: a role it has none of says
 * "uses the shared …" and offers "Use its own…"; one it has offers "Back to
 * shared". Nothing is forced either way.
 */
export function FilesEditor({
  info, meta, entries, onChange, shared, testid = 'files-editor',
}: {
  info: ContentTypeInfo | undefined;
  meta: ContentMeta;
  entries: FileEntry[];
  onChange: (entries: FileEntry[]) => void;
  shared?: ContentMedia[];
  testid?: string;
}) {
  const roles = useMemo(() => rolesFor(info), [info]);
  const previews = useRef(new Map<File, string>());
  useEffect(() => () => {
    previews.current.forEach((url) => URL.revokeObjectURL(url));
  }, []);

  function preview(file: File): string {
    const known = previews.current.get(file);
    if (known) return known;
    const url = URL.createObjectURL(file);
    previews.current.set(file, url);
    return url;
  }

  function add(role: Role, files: FileList | null) {
    if (!files?.length) return;
    const picked = [...files].map((file) => ({
      key: nextKey(), role, file, name: file.name, kind: kindOf(file), url: preview(file),
    }));
    onChange(MANY.includes(role)
      ? [...entries, ...picked]
      : [...entries.filter((e) => e.role !== role), picked[0]!]);
  }

  function move(entry: FileEntry, by: -1 | 1) {
    const same = entries.filter((e) => e.role === entry.role);
    const at = same.indexOf(entry);
    const to = at + by;
    if (to < 0 || to >= same.length) return;
    const reordered = [...same];
    [reordered[at], reordered[to]] = [reordered[to]!, reordered[at]!];
    onChange([...entries.filter((e) => e.role !== entry.role), ...reordered]);
  }

  return (
    <div className="space-y-3" data-testid={testid}>
      {roles.map((role) => {
        const mine = entries.filter((e) => e.role === role);
        const theirs = shared?.filter((m) => m.role === role) ?? [];
        const label = role === 'primary' ? (info?.label ?? 'File') : role === 'slide' ? 'Slides'
          : meta.assets[role] ?? role;
        const accept = role === 'primary' || role === 'slide' ? ACCEPT[info?.render ?? ''] ?? '' : 'image/*';
        const input = (
          <label className="inline-flex cursor-pointer items-center rounded-pill border border-surface-border px-3
                            py-1 text-[12px] text-ink-muted hover:text-ink">
            {shared && !mine.length ? 'Use its own…' : mine.length && !MANY.includes(role) ? 'Replace…'
              : MANY.includes(role) ? '+ Add' : 'Choose…'}
            <input type="file" className="sr-only" accept={role === 'attachment' ? undefined : accept}
                   multiple={MANY.includes(role)} data-testid={`${testid}-pick-${role}`}
                   onChange={(e) => {
                     add(role, e.target.files);
                     e.target.value = '';
                   }} />
          </label>
        );
        return (
          <div key={role} className="rounded border border-surface-border p-2.5" data-testid={`${testid}-${role}`}>
            <div className="mb-1.5 flex items-center gap-2">
              <span className="text-[12px] font-medium text-ink">{label}</span>
              {shared && (
                <span className="text-[11px] text-ink-faint" data-testid={`${testid}-${role}-source`}>
                  {mine.length ? 'its own' : theirs.length ? 'shared' : 'none'}
                </span>
              )}
              <span className="ml-auto flex items-center gap-2">
                {shared && mine.length > 0 && (
                  <button type="button" className="text-[12px] text-accent underline"
                          onClick={() => onChange(entries.filter((e) => e.role !== role))}>Back to shared</button>
                )}
                {input}
              </span>
            </div>
            {mine.length === 0 ? (
              <p className="text-[12px] text-ink-faint">
                {shared ? (theirs.length ? `Uses the shared ${label.toLowerCase()}: ${theirs.map((t) => t.name).join(', ')}`
                  : `No ${label.toLowerCase()}.`) : 'None yet.'}
              </p>
            ) : (
              <ul className="flex flex-wrap gap-2">
                {mine.map((entry, index) => (
                  <li key={entry.key} className="w-28" data-testid={`${testid}-file`}>
                    <Tile entry={entry} />
                    <span className="mt-0.5 block truncate text-[11px] text-ink-muted" title={entry.name}>
                      {role === 'slide' ? `${index + 1}. ` : ''}{entry.name}
                    </span>
                    <span className="flex gap-2 text-[11px]">
                      {role === 'slide' && (
                        <>
                          <button type="button" className="text-ink-faint hover:text-ink disabled:opacity-30"
                                  disabled={index === 0} onClick={() => move(entry, -1)} aria-label="Earlier">‹</button>
                          <button type="button" className="text-ink-faint hover:text-ink disabled:opacity-30"
                                  disabled={index === mine.length - 1} onClick={() => move(entry, 1)}
                                  aria-label="Later">›</button>
                        </>
                      )}
                      <button type="button" className="text-state-danger/80 hover:text-state-danger"
                              data-testid={`${testid}-remove`}
                              onClick={() => onChange(entries.filter((e) => e !== entry))}>Remove</button>
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </div>
        );
      })}
    </div>
  );
}

function Tile({ entry }: { entry: FileEntry }) {
  const common = 'h-16 w-28 rounded border border-surface-border bg-black/40 object-cover';
  if (entry.kind === 'image' && entry.url) {
    // eslint-disable-next-line @next/next/no-img-element
    return <img src={entry.url} alt="" className={common} />;
  }
  if (entry.kind === 'video' && entry.url) {
    return <video src={entry.url} muted preload="metadata" className={common} />;
  }
  return (
    <div className={`${common} flex items-center justify-center px-1 text-center text-[10px] text-ink-faint`}>
      {entry.kind === 'audio' ? 'Audio' : 'File'}
    </div>
  );
}

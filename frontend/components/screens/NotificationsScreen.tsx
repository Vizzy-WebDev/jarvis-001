'use client';

import { useCallback, useEffect, useState } from 'react';

import { Button } from '@/components/ui/Button';
import { Card } from '@/components/ui/Card';
import { EmptyState } from '@/components/ui/EmptyState';
import { Modal } from '@/components/ui/Modal';
import { api, ApiRequestError } from '@/lib/api';
import type { Notification } from '@/lib/api-types';

/**
 * Everything Jarvis has told the user, including while they were away.
 *
 * Every row opens — a notice is a thing that happened, with a reason and a time
 * and sometimes somewhere to go, and a list you can only look at makes the user
 * ask "what was that one?" with no way to find out. Opening one marks it read,
 * because reading it is what "read" means.
 */
export function NotificationsScreen({ onNavigate }: { onNavigate: (id: string) => void }) {
  const [rows, setRows] = useState<Notification[] | null>(null);
  const [open, setOpen] = useState<Notification | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState<'all' | 'unread'>('all');
  const [binOpen, setBinOpen] = useState(false);
  const [trashCount, setTrashCount] = useState(0);

  const load = useCallback(async () => {
    try {
      const [active, trashed] = await Promise.all([
        api.notifications.list(),
        api.notifications.trash(),
      ]);
      setRows(active.notifications);
      setTrashCount(trashed.notifications.length);
    } catch (err) {
      setError(err instanceof ApiRequestError ? err.message : 'Could not read your notifications.');
      setRows([]);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  // A notice can arrive while this screen is open — it is the one screen where
  // that is likely, since the things that produce them run in the background.
  useEffect(() => {
    const source = new EventSource('/api/events');
    source.onmessage = (raw) => {
      try {
        if ((JSON.parse(raw.data) as { type?: string }).type === 'notification.stored') void load();
      } catch {
        /* a frame we cannot read is not worth acting on */
      }
    };
    return () => source.close();
  }, [load]);

  async function show(row: Notification) {
    setOpen(row);
    if (!row.read) {
      await api.notifications.markRead([row.id]).catch(() => undefined);
      setRows((current) =>
        (current ?? []).map((n) => (n.id === row.id ? { ...n, read: true } : n)));
    }
  }

  async function remove(id: string) {
    setRows((current) => (current ?? []).filter((n) => n.id !== id));
    setTrashCount((n) => n + 1);
    setOpen(null);
    await api.notifications.remove(id).catch(() => void load());
  }

  const unread = (rows ?? []).filter((row) => !row.read).length;
  const visibleRows = (rows ?? []).filter((row) => filter === 'all' || !row.read);

  return (
    <>
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <Button
          data-testid="mark-all-read"
          disabled={!unread}
          onClick={async () => {
            await api.notifications.markAllRead();
            void load();
          }}
        >
          Mark all read{unread ? ` (${unread})` : ''}
        </Button>
        <Button
          tone="danger"
          data-testid="clear-all"
          disabled={!(rows ?? []).length}
          onClick={async () => {
            setTrashCount((n) => n + (rows ?? []).length);
            await api.notifications.clear();
            setRows([]);
          }}
        >
          Clear all
        </Button>
        <Button
          data-testid="open-recycle-bin"
          onClick={() => setBinOpen(true)}
          className="ml-auto"
        >
          Recycle Bin{trashCount ? ` (${trashCount})` : ''}
        </Button>
      </div>

      <p className="mb-4 text-[12px] text-ink-faint">
        Cleared or deleted notifications go to the recycle bin for 30 days before they're gone for good.
      </p>

      <div className="mb-4 inline-flex rounded-pill border border-surface-border p-0.5">
        {([['all', 'All'], ['unread', 'Unread']] as [typeof filter, string][]).map(
          ([id, label]) => (
            <button
              key={id}
              type="button"
              data-testid={`notification-filter-${id}`}
              aria-pressed={filter === id}
              onClick={() => setFilter(id)}
              className={[
                'rounded-pill px-3 py-1 text-[12px] transition duration-150 ease-out',
                filter === id ? 'bg-accent/15 text-accent' : 'text-ink-muted hover:text-ink',
              ].join(' ')}
            >
              {label}{id === 'unread' && unread ? ` (${unread})` : ''}
            </button>
          ),
        )}
      </div>

      {error && <p className="mb-4 text-[13px] text-state-danger">{error}</p>}

      {rows === null ? (
        <p className="py-10 text-center text-[13px] text-ink-faint">Reading…</p>
      ) : rows.length === 0 ? (
        <EmptyState
          title="Nothing to catch up on."
          body="When a scheduled task fails overnight, or something you asked Jarvis to watch for happens, it lands here — and stays until you have seen it."
        />
      ) : visibleRows.length === 0 ? (
        <p className="py-10 text-center text-[13px] text-ink-faint">Nothing unread.</p>
      ) : (
        <ul className="space-y-2" data-testid="notification-list">
          {visibleRows.map((row) => (
            <li key={row.id}>
              <Card
                interactive
                role="button"
                tabIndex={0}
                data-testid="notification-row"
                onClick={() => void show(row)}
                onKeyDown={(event) => {
                  if (event.key === 'Enter' || event.key === ' ') {
                    event.preventDefault();
                    void show(row);
                  }
                }}
                className="flex items-start gap-3"
              >
                <span
                  aria-hidden
                  className={`mt-1.5 h-2 w-2 shrink-0 rounded-full ${row.read ? 'bg-transparent' : levelDot(row.level)}`}
                />
                <div className="min-w-0 flex-1">
                  <p className={`text-[14px] ${row.read ? 'text-ink-muted' : 'text-ink'}`}>
                    {row.title}
                    {row.count > 1 && (
                      <span className="ml-2 rounded-pill bg-white/[0.06] px-1.5 py-px text-[10px] text-ink-faint">
                        ×{row.count}
                      </span>
                    )}
                  </p>
                  {row.body && (
                    <p className="mt-0.5 truncate text-[12px] text-ink-faint">{row.body}</p>
                  )}
                </div>
                <span className="shrink-0 text-[11px] text-ink-faint">{when(row.ts)}</span>
              </Card>
            </li>
          ))}
        </ul>
      )}

      <Modal
        open={open !== null}
        title={open?.title ?? ''}
        onClose={() => setOpen(null)}
        footer={
          open && (
            <>
              {open.action?.section && (
                <Button
                  tone="primary"
                  onClick={() => {
                    onNavigate(open.action!.section);
                    setOpen(null);
                  }}
                >
                  {open.action.label || 'Take me there'}
                </Button>
              )}
              <Button tone="danger" className="ml-auto" onClick={() => void remove(open.id)}>
                Delete
              </Button>
            </>
          )
        }
      >
        {open && (
          <>
            <p className="mb-3 flex flex-wrap items-center gap-2 text-[11px] uppercase tracking-[0.12em] text-ink-faint">
              <span>{open.kind.replace(/_/g, ' ')}</span>
              <span aria-hidden>·</span>
              <span>{open.level}</span>
              <span aria-hidden>·</span>
              <span>{new Date(open.ts).toLocaleString()}</span>
            </p>
            <p className="whitespace-pre-wrap text-[14px] leading-relaxed text-ink">
              {open.body || 'No further detail was recorded.'}
            </p>
            {open.count > 1 && (
              <p className="mt-3 text-[12px] text-ink-muted">
                This happened {open.count} times. The detail above is from the most recent one.
              </p>
            )}
          </>
        )}
      </Modal>

      {binOpen && (
        <RecycleBin
          onClose={() => setBinOpen(false)}
          onChanged={(count) => {
            setTrashCount(count);
            void load();
          }}
        />
      )}
    </>
  );
}

function RecycleBin({ onClose, onChanged }: {
  onClose: () => void;
  onChanged: (trashCount: number) => void;
}) {
  const [rows, setRows] = useState<Notification[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const found = (await api.notifications.trash()).notifications;
      setRows(found);
      onChanged(found.length);
    } catch (err) {
      setError(err instanceof ApiRequestError ? err.message : 'Could not read the recycle bin.');
      setRows([]);
    }
    // onChanged is a fresh arrow from the caller every render; only re-run
    // this on a real reload, not because the parent re-rendered.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function restore(id: string) {
    setRows((current) => (current ?? []).filter((n) => n.id !== id));
    await api.notifications.restore(id).catch(() => void load());
    onChanged((rows ?? []).length - 1);
  }

  async function deleteForever(id: string) {
    setRows((current) => (current ?? []).filter((n) => n.id !== id));
    await api.notifications.purge(id).catch(() => void load());
    onChanged((rows ?? []).length - 1);
  }

  async function emptyBin() {
    setRows([]);
    await api.notifications.emptyTrash().catch(() => void load());
    onChanged(0);
  }

  return (
    <Modal
      open
      title="Recycle Bin"
      onClose={onClose}
      footer={
        <Button
          tone="danger"
          data-testid="empty-recycle-bin"
          disabled={!(rows ?? []).length}
          onClick={() => void emptyBin()}
        >
          Empty recycle bin
        </Button>
      }
    >
      {error && <p className="mb-3 text-[13px] text-state-danger">{error}</p>}
      {rows === null ? (
        <p className="py-6 text-center text-[13px] text-ink-faint">Reading…</p>
      ) : rows.length === 0 ? (
        <p className="py-6 text-center text-[13px] text-ink-faint">
          The recycle bin is empty.
        </p>
      ) : (
        <ul className="space-y-2" data-testid="recycle-bin-list">
          {rows.map((row) => (
            <li key={row.id}>
              <Card data-testid="recycle-bin-row" className="flex items-center gap-3">
                <div className="min-w-0 flex-1">
                  <p className="truncate text-[14px] text-ink-muted">{row.title}</p>
                  <p className="mt-0.5 truncate text-[12px] text-ink-faint">
                    {daysLeft(row.trashedAt)}
                  </p>
                </div>
                <Button data-testid="restore-notification" onClick={() => void restore(row.id)}>
                  Restore
                </Button>
                <Button tone="danger" data-testid="delete-forever"
                        onClick={() => void deleteForever(row.id)}>
                  Delete forever
                </Button>
              </Card>
            </li>
          ))}
        </ul>
      )}
    </Modal>
  );
}

/** How long until the automatic 30-day sweep would take it anyway. */
function daysLeft(trashedAt: string | undefined): string {
  if (!trashedAt) return '';
  const trashedMs = new Date(trashedAt).getTime();
  if (Number.isNaN(trashedMs)) return '';
  const remaining = 30 - Math.floor((Date.now() - trashedMs) / 86_400_000);
  if (remaining <= 0) return 'Gone very soon';
  return `${remaining} day${remaining === 1 ? '' : 's'} left`;
}

function levelDot(level: string): string {
  if (level === 'error') return 'bg-state-danger';
  if (level === 'warning') return 'bg-state-warn';
  if (level === 'success') return 'bg-state-ok';
  return 'bg-accent';
}

/** Relative for anything recent, a real date once "3 days ago" stops helping. */
function when(iso: string): string {
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return '';
  const seconds = Math.round((Date.now() - then) / 1000);
  if (seconds < 60) return 'just now';
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h ago`;
  if (seconds < 604800) return `${Math.round(seconds / 86400)}d ago`;
  return new Date(then).toLocaleDateString();
}

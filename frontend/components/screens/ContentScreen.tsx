'use client';

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { AddSeveralDialog, oneFileTypes } from '@/components/content/AddSeveralDialog';
import { AnalyticsView } from '@/components/content/AnalyticsView';
import { CalendarView } from '@/components/content/CalendarView';
import { ConfirmDialog } from '@/components/content/Dialogs';
import {
  FLOW, inZone, PLACEMENT_LABEL, PLACEMENT_TONE, previewImage, STAGE_LABEL, STAGE_TONE, VIEW_STATUSES, when,
} from '@/components/content/format';
import { NewContentDialog } from '@/components/content/NewContentDialog';
import { NicheDeleteDialog, NicheNameDialog } from '@/components/content/NicheDialogs';
import { type Folder, NichesView, typeCounts } from '@/components/content/NichesView';
import { Workspace } from '@/components/content/Workspace';
import { Button } from '@/components/ui/Button';
import { Card } from '@/components/ui/Card';
import { EmptyState } from '@/components/ui/EmptyState';
import { inputClass } from '@/components/ui/Field';
import { Modal } from '@/components/ui/Modal';
import { Popover } from '@/components/ui/Popover';
import { api, ApiRequestError } from '@/lib/api';
import type {
  ContentFilters, ContentFolder, ContentItem, ContentMeta, ContentNicheOverview, ContentStage, ContentSummary,
} from '@/lib/api-types';

/** `active` is "All" inside a folder: everything still in play, every stage. */
type View = ContentStage | 'bin' | 'analytics' | 'active';

/** A page of cards: a busy stage holds thousands, and they arrive a page at a time. */
const PAGE = 50;

/** The shared input look, sized to its content: `inputClass` is full-width. */
const compact = inputClass.replace('w-full', 'w-auto');

const EMPTY: Record<Exclude<View, 'analytics' | 'active'>, { title: string; body: string }> = {
  review: { title: 'Nothing waiting for review.', body: 'New content — yours, Jarvis’s or an agent’s — lands here first.' },
  changes_requested: { title: 'No changes pending.', body: 'Anything you send back for changes waits here until the revision is in.' },
  approved: { title: 'Nothing ready to post.', body: 'Approved content waits here until you post it or schedule it.' },
  scheduling: { title: 'Nothing scheduled.', body: 'Scheduled and queued posts show here, and on the calendar.' },
  published: { title: 'Nothing published yet.', body: 'Everything that goes out stays here as a record.' },
  archived: { title: 'Nothing archived.', body: 'Archive keeps things you are done with, out of the way but searchable.' },
  bin: { title: 'The Recycle Bin is empty.', body: 'Deleted content waits here until you restore it or delete it forever. Nothing is removed automatically.' },
};

const POSTS_WORD: Partial<Record<View, string>> = {
  approved: 'ready to post', scheduling: 'scheduled or on their way', published: 'published',
};

/** Where the person is, kept in the address (`#/content/niche/Psychology`) so a
 *  refresh stays in the folder and Back leaves it. */
function readFolder(): Folder | null {
  const rest = window.location.hash.replace(/^#\/?/, '').split('/').slice(1);
  if (rest[0] === 'all') return { kind: 'all' };
  if (rest[0] === 'none') return { kind: 'none' };
  if (rest[0] === 'niche' && rest[1]) {
    try {
      return { kind: 'niche', name: decodeURIComponent(rest.slice(1).join('/')) };
    } catch {
      return null;
    }
  }
  return null;
}

function folderHash(folder: Folder | null): string {
  if (!folder) return '#/content';
  if (folder.kind !== 'niche') return `#/content/${folder.kind}`;
  return `#/content/niche/${encodeURIComponent(folder.name)}`;
}

function sameFolder(a: Folder | null, b: Folder | null): boolean {
  return folderHash(a) === folderHash(b);
}

/**
 * Content Management: niches are folders, and inside one is the whole life of
 * its content — review, changes, approval, scheduling, publishing, the archive,
 * the recycle bin, and what was reported about it afterwards.
 *
 * The first screen is the folders. Inside a folder, "All" shows every piece —
 * videos, carousels, images, posts — together, and the lifecycle strip is the
 * navigation. Every piece is still its own item in the ONE workflow: a folder is
 * the niche filter held fixed, never a copy of anything.
 *
 * "+ Add" is the person doing what agents and Jarvis already do: handing
 * something in, through the same door, into the same pipeline — one piece, or
 * many files at once, each its own item.
 */
export function ContentScreen({ onNavigate }: { onNavigate: (id: string) => void }) {
  const [meta, setMeta] = useState<ContentMeta | null>(null);
  const [overview, setOverview] = useState<ContentNicheOverview | null>(null);
  const [folder, setFolder] = useState<Folder | null | undefined>(undefined);
  const [view, setView] = useState<View>('active');
  const [filters, setFilters] = useState<ContentFilters>({});
  const [search, setSearch] = useState('');
  const [dates, setDates] = useState<{ from: string; to: string }>({ from: '', to: '' });
  const [archivedFrom, setArchivedFrom] = useState('');
  const [mode, setMode] = useState<'list' | 'calendar'>('list');
  const [summary, setSummary] = useState<ContentSummary | null>(null);
  const [items, setItems] = useState<ContentItem[] | null>(null);
  const [total, setTotal] = useState(0);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState<{ id: string; schedule?: string } | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);
  const [creating, setCreating] = useState<{ type?: string } | null>(null);
  const [several, setSeveral] = useState(false);
  const [naming, setNaming] = useState<'new' | 'rename' | null>(null);
  const [deletingNiche, setDeletingNiche] = useState(false);
  const [pickDay, setPickDay] = useState<string | null>(null);
  const [emptying, setEmptying] = useState(false);
  const [adding, setAdding] = useState(false);
  const addAnchor = useRef<HTMLSpanElement>(null);
  // Which tab a folder opens on: "All", unless the way in asked for another.
  const openingView = useRef<View>('active');

  const bump = useCallback(() => setRefreshKey((k) => k + 1), []);

  useEffect(() => {
    api.content.meta().then(setMeta).catch((err) =>
      setError(err instanceof ApiRequestError ? err.message : 'Could not read Content Management.'));
  }, []);

  // The folder lives in the address; Back and refresh follow it.
  useEffect(() => {
    const read = () => {
      const next = readFolder();
      setFolder((current) => (current !== undefined && sameFolder(current, next) ? current : next));
    };
    read();
    window.addEventListener('hashchange', read);
    return () => window.removeEventListener('hashchange', read);
  }, []);

  const openFolder = useCallback((next: Folder | null, startOn: View = 'active', replace = false) => {
    openingView.current = startOn;
    const hash = folderHash(next);
    if (replace) window.location.replace(hash);
    else window.location.hash = hash;
  }, []);

  // A different folder starts clean: its "All" tab, no leftover filters.
  const folderKey = folder === undefined ? '' : folderHash(folder);
  useEffect(() => {
    if (folder === undefined) return;
    setView(openingView.current);
    openingView.current = 'active';
    setFilters({});
    setSearch('');
    setDates({ from: '', to: '' });
    setArchivedFrom('');
    setMode('list');
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [folderKey]);

  // Search waits for a pause in typing rather than asking on every key.
  useEffect(() => {
    const timer = setTimeout(() => setFilters((f) => ({ ...f, q: search.trim() || undefined })), 250);
    return () => clearTimeout(timer);
  }, [search]);

  // Everything a folder shows is the ordinary lists with its niche held fixed.
  const scoped = useMemo<ContentFilters>(() => {
    if (folder?.kind === 'niche') return { ...filters, niche: folder.name, noNiche: undefined };
    if (folder?.kind === 'none') return { ...filters, niche: undefined, noNiche: '1' };
    return filters;
  }, [filters, folder]);

  const fromIso = dates.from ? new Date(`${dates.from}T00:00:00`).toISOString() : undefined;
  const toIso = dates.to ? new Date(`${dates.to}T23:59:59`).toISOString() : undefined;
  const listed = folder && view !== 'analytics' ? view : null;
  const query = useMemo(() => ({
    ...scoped, from: fromIso, to: toIso,
    archivedFrom: view === 'archived' ? archivedFrom || undefined : undefined,
  }), [scoped, fromIso, toIso, view, archivedFrom]);

  // How many cards to (re)load: a page, plus however many "Show more" added —
  // so a refresh while reading far down a list keeps the place. A different
  // stage or filter starts again from the top. Declared BEFORE the load effect
  // so it has already reset when that runs.
  const wanted = useRef(PAGE);
  useEffect(() => {
    wanted.current = PAGE;
    setItems(null);
  }, [listed, query]);

  const load = useCallback(async () => {
    if (folder === undefined) return;
    try {
      const folders = api.content.niches();
      const counts = api.content.summary(folder ? scoped : {});
      if (listed) {
        const list = await api.content.list(listed, { ...query, limit: wanted.current });
        setItems(list.items);
        setTotal(list.total ?? list.items.length);
      }
      setSummary(await counts);
      setOverview(await folders);
      setError(null);
    } catch (err) {
      setError(err instanceof ApiRequestError ? err.message : 'Could not read your content.');
      setItems([]);
    }
  }, [folder, listed, query, scoped]);

  useEffect(() => {
    void load();
  }, [load, refreshKey]);

  async function showMore() {
    if (!listed || !items) return;
    setLoadingMore(true);
    try {
      const next = await api.content.list(listed, { ...query, limit: PAGE, offset: items.length });
      wanted.current = items.length + next.items.length;
      setItems((current) => {
        const seen = new Set((current ?? []).map((i) => i.id));
        return [...(current ?? []), ...next.items.filter((i) => !seen.has(i.id))];
      });
      setTotal(next.total ?? total);
    } catch (err) {
      setError(err instanceof ApiRequestError ? err.message : 'Could not read more.');
    } finally {
      setLoadingMore(false);
    }
  }

  // Agents hand things in, publishers report back and Jarvis's own jobs finish
  // while this screen is open. A burst of them (an agent submitting twenty
  // things) becomes one refresh, not twenty.
  const pending = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => {
    const source = new EventSource('/api/events');
    source.onmessage = (raw) => {
      try {
        const type = (JSON.parse(raw.data) as { type?: string }).type;
        if (type === 'content.changed' || type === 'job.completed' || type === 'job.updated') {
          if (pending.current) clearTimeout(pending.current);
          pending.current = setTimeout(bump, 400);
        }
      } catch {
        /* a frame we cannot read is not worth acting on */
      }
    };
    return () => {
      source.close();
      if (pending.current) clearTimeout(pending.current);
    };
  }, [bump]);

  // Every niche picker offers the folders as they are now, empty ones included.
  // A new object only when the NAMES change: every refresh brings a new
  // overview, and an open Workspace re-reads its item when `meta` changes.
  const nicheNames = overview ? overview.niches.map((n) => n.name).join('\n') : null;
  const metaNow = useMemo<ContentMeta | null>(
    () => (meta && nicheNames !== null ? { ...meta, niches: nicheNames ? nicheNames.split('\n') : [] } : meta),
    [meta, nicheNames]);

  const counts = summary?.counts;
  const needs = summary?.attention;
  const needsParts = useMemo(() => {
    if (!needs) return [];
    const parts: { label: string; go: View }[] = [];
    if (needs.toReview) parts.push({ label: `${needs.toReview} to review`, go: 'review' });
    if (needs.revisionsReady) parts.push({ label: `${needs.revisionsReady} revision${needs.revisionsReady === 1 ? '' : 's'} ready`, go: 'review' });
    if (needs.failedPosts) parts.push({ label: `${needs.failedPosts} post${needs.failedPosts === 1 ? '' : 's'} failed`, go: 'approved' });
    if (needs.revisionsStuck) parts.push({ label: `${needs.revisionsStuck} revision${needs.revisionsStuck === 1 ? '' : 's'} couldn't start`, go: 'changes_requested' });
    return parts;
  }, [needs]);

  const current: ContentFolder | undefined = !overview || !folder ? undefined
    : folder.kind === 'all' ? overview.all
      : folder.kind === 'none' ? overview.none
        : overview.niches.find((n) => n.name.toLowerCase() === folder.name.toLowerCase());
  const folderName = !folder ? '' : folder.kind === 'all' ? 'All content' : folder.kind === 'none' ? 'No niche'
    : overview?.niches.find((n) => n.name.toLowerCase() === folder.name.toLowerCase())?.name ?? folder.name;
  const nicheHere = folder?.kind === 'niche' ? folderName : '';

  if (!metaNow || folder === undefined) {
    return error ? <p className="text-[13px] text-state-danger">{error}</p>
      : <p className="text-[13px] text-ink-faint">Reading…</p>;
  }

  const needsLine = (
    <p className="text-[13px]" data-testid="content-needs">
      {needsParts.length ? (
        <>
          <span className="text-ink-faint">{folder ? 'Needs you here: ' : 'Needs you: '}</span>
          {needsParts.map((part, i) => (
            <span key={part.label}>
              {i > 0 && <span className="text-ink-faint"> · </span>}
              <button type="button" className="text-state-warn underline-offset-2 hover:underline"
                      onClick={() => (folder ? setView(part.go) : openFolder({ kind: 'all' }, part.go))}>
                {part.label}</button>
            </span>
          ))}
        </>
      ) : <span className="text-ink-faint">Nothing needs you right now.</span>}
    </p>
  );

  const dialogs = (
    <>
      {open && (
        <Workspace itemId={open.id} meta={metaNow} refreshKey={refreshKey} initialSchedule={open.schedule}
                   onClose={() => setOpen(null)} onChanged={bump} onNavigate={onNavigate} />
      )}
      {creating && (
        <NewContentDialog meta={metaNow} initialType={creating.type} initialNiche={nicheHere}
                          onClose={() => setCreating(null)} onCreated={(id) => {
                            bump();
                            setOpen({ id });
                          }} />
      )}
      {several && (
        <AddSeveralDialog meta={metaNow} niche={nicheHere} onAdded={bump} onClose={() => setSeveral(false)} />
      )}
      {naming && (
        <NicheNameDialog current={naming === 'rename' ? folderName : undefined} onClose={() => setNaming(null)}
                         onDone={(name) => {
                           bump();
                           // A renamed folder is the same place under a new name.
                           openFolder({ kind: 'niche', name }, naming === 'rename' ? view : 'active',
                                      naming === 'rename');
                         }} />
      )}
      {deletingNiche && current && (
        <NicheDeleteDialog name={folderName} folder={current} onClose={() => setDeletingNiche(false)}
                           onDone={() => {
                             bump();
                             openFolder(null, 'active', true);
                           }} />
      )}
    </>
  );

  // --- the folders ------------------------------------------------------------------------
  if (!folder) {
    return (
      <div data-testid="content-screen">
        <div className="mb-4 flex flex-wrap items-center gap-3">
          {needsLine}
          <Button tone="primary" className="ml-auto" data-testid="new-niche" onClick={() => setNaming('new')}>
            + New niche</Button>
        </div>
        {error && <p className="mb-3 text-[13px] text-state-danger">{error}</p>}
        {overview ? (
          <NichesView overview={overview} meta={metaNow} onNew={() => setNaming('new')}
                      onOpen={(next) => openFolder(next)} />
        ) : <p className="text-[13px] text-ink-faint">Reading…</p>}
        {dialogs}
      </div>
    );
  }

  // --- inside a folder ----------------------------------------------------------------------
  const missing = folder.kind === 'niche' && overview && !current;
  const dated = view === 'published' || view === 'archived' || view === 'bin' || view === 'analytics';
  const postsHere = summary?.posts && view in summary.posts
    ? summary.posts[view as keyof ContentSummary['posts']] : null;
  // Every type is offered in every tab (the archive holds kinds nothing in play
  // does); the counts are what the folder has IN PLAY, so they show on "All" only.
  const inPlay = new Map((current ? typeCounts(current, metaNow) : []).map((t) => [t.id, t.n]));
  const types = Object.entries(metaNow.types).map(([id, t]) => ({ id, label: t.label, n: inPlay.get(id) ?? 0 }));
  const counted = view === 'active';
  const severalTypes = oneFileTypes(metaNow);
  const narrowed = filters.q || filters.niche || filters.type || filters.platform || dates.from || dates.to
    || archivedFrom;

  return (
    <div data-testid="content-screen" data-folder={folderName}>
      {/* Where you are, and what this folder is. */}
      <div className="mb-3 flex flex-wrap items-center gap-x-3 gap-y-2" data-testid="folder-header">
        <button type="button" data-testid="back-to-niches" onClick={() => openFolder(null)}
                className="rounded-pill border border-surface-border px-3 py-1 text-[13px] text-ink-muted
                           hover:text-ink">← All niches</button>
        <h2 className="min-w-0 max-w-full truncate text-[18px] font-medium text-ink" data-testid="folder-name"
            title={folderName}>{folderName}</h2>
        {current && (
          <span className="text-[12px] text-ink-faint" data-testid="folder-total">
            {current.total.toLocaleString()} item{current.total === 1 ? '' : 's'}</span>
        )}
        {folder.kind === 'niche' && current && (
          <span className="flex items-center gap-1">
            <button type="button" data-testid="rename-niche" onClick={() => setNaming('rename')}
                    className="rounded px-2 py-1 text-[12px] text-ink-faint hover:text-ink">Rename</button>
            <button type="button" data-testid="delete-niche" onClick={() => setDeletingNiche(true)}
                    className="rounded px-2 py-1 text-[12px] text-ink-faint hover:text-state-danger">Delete</button>
          </span>
        )}
        <span className="ml-auto inline-flex" ref={addAnchor}>
          <Button tone="primary" data-testid="add-content" disabled={Boolean(missing)}
                  onClick={() => setAdding((v) => !v)}>+ Add</Button>
        </span>
        <Popover open={adding} anchorRef={addAnchor} onClose={() => setAdding(false)} width={260} align="end">
          <div className="py-1" data-testid="add-menu">
            <p className="px-3 pb-1 pt-1.5 text-[11px] font-medium text-ink-faint">
              One piece{nicheHere ? ` in ${nicheHere}` : ''}</p>
            {Object.entries(metaNow.types).map(([id, t]) => (
              <button key={id} type="button" data-testid={`add-${id}`}
                      className="block w-full px-3 py-1.5 text-left text-[13px] text-ink hover:bg-white/[0.05]"
                      onClick={() => { setAdding(false); setCreating({ type: id }); }}>{t.label}</button>
            ))}
            <div className="my-1 border-t border-surface-border" />
            <button type="button" data-testid="add-several"
                    className="block w-full px-3 py-1.5 text-left hover:bg-white/[0.05]"
                    onClick={() => { setAdding(false); setSeveral(true); }}>
              <span className="block text-[13px] text-ink">Add several files…</span>
              <span className="block text-[11px] text-ink-faint">
                Each file becomes its own {severalTypes.map((id) => metaNow.types[id]!.label.toLowerCase())
                  .join(', ')}</span>
            </button>
          </div>
        </Popover>
      </div>

      {missing ? (
        <EmptyState title={`There's no niche called “${folder.name}” any more.`}
                    body="It may have been renamed or deleted."
                    action={<Button onClick={() => openFolder(null)}>Back to all niches</Button>} />
      ) : (
        <>
          <div className="mb-3">{needsLine}</div>

          {/* The lifecycle, left to right — after "All", which is every stage at once. */}
          <nav className="mb-3 flex flex-wrap items-center gap-1.5" data-testid="content-strip"
               aria-label="Content stages">
            <StageTab id="active" label="All" count={counts?.active ?? 0} active={view === 'active'}
                      onClick={() => setView('active')} />
            <span className="mx-1 h-4 w-px bg-surface-border" aria-hidden />
            {FLOW.map((stage, i) => (
              <span key={stage.id} className="flex items-center gap-1.5">
                <StageTab id={stage.id} label={stage.label} count={counts?.[stage.id] ?? 0} active={view === stage.id}
                          alert={(stage.id === 'review' && Boolean(counts?.review))
                            || (stage.id === 'approved' && Boolean(needs?.failedPosts))
                            || (stage.id === 'changes_requested' && Boolean(needs?.revisionsStuck))}
                          onClick={() => setView(stage.id)} />
                {i < FLOW.length - 1 && <span className="text-ink-faint" aria-hidden>→</span>}
              </span>
            ))}
            <span className="ml-auto flex items-center gap-1.5">
              <StageTab id="analytics" label="Analytics" active={view === 'analytics'} quiet
                        onClick={() => setView('analytics')} />
              <StageTab id="archived" label="Archived" count={counts?.archived ?? 0} active={view === 'archived'} quiet
                        onClick={() => setView('archived')} />
              <StageTab id="bin" label="Recycle Bin" count={counts?.bin ?? 0} active={view === 'bin'} quiet
                        onClick={() => setView('bin')} />
            </span>
          </nav>

          {/* What kinds of content this folder holds — every one its own item. */}
          {/* A folder with nothing in it at all has no kinds to pick between. */}
          {(!current || current.total + current.archived + current.binned > 0) && (
          <div className="mb-3 flex flex-wrap items-center gap-1" data-testid="type-chips">
            <TypeChip label="Every type" count={counted ? current?.total : undefined} active={!filters.type}
                      testid="type-all" onClick={() => setFilters((f) => ({ ...f, type: undefined }))} />
            {types.map((t) => (
              <TypeChip key={t.id} label={t.label} count={counted ? t.n : undefined} active={filters.type === t.id}
                        testid={`type-${t.id}`}
                        onClick={() => setFilters((f) => ({ ...f, type: f.type === t.id ? undefined : t.id }))} />
            ))}
          </div>
          )}

          {/* Filters apply to every stage, and the counts above follow them. */}
          <div className="mb-4 flex flex-wrap items-center gap-2" data-testid="content-filters">
            <input data-testid="content-search" className={`${inputClass} max-w-[16rem]`}
                   placeholder={folder.kind === 'niche' ? `Search ${folderName}…` : 'Search…'}
                   value={search} onChange={(e) => setSearch(e.target.value)} />
            {folder.kind === 'all' && (
              <FilterSelect testid="filter-niche" label="Niche" value={filters.niche}
                            options={metaNow.niches.map((n) => [n, n])}
                            onChange={(v) => setFilters((f) => ({ ...f, niche: v }))} />
            )}
            <FilterSelect testid="filter-platform" label="Platform" value={filters.platform}
                          options={Object.entries(metaNow.platforms).map(([id, p]) => [id, p.label])}
                          onChange={(v) => setFilters((f) => ({ ...f, platform: v }))} />
            {dated && (
              <>
                <input type="date" data-testid="filter-from" className={compact} value={dates.from}
                       title="From" onChange={(e) => setDates((d) => ({ ...d, from: e.target.value }))} />
                <input type="date" data-testid="filter-to" className={compact} value={dates.to}
                       title="To" onChange={(e) => setDates((d) => ({ ...d, to: e.target.value }))} />
              </>
            )}
            {view === 'archived' && (
              <FilterSelect testid="filter-archived-from" label="Status" value={archivedFrom || undefined}
                            options={FLOW.map((s) => [s.id, `Was ${s.label}`])} onChange={(v) => setArchivedFrom(v ?? '')} />
            )}
            {view === 'scheduling' && (
              <span className="ml-auto inline-flex rounded-pill border border-surface-border p-0.5" data-testid="schedule-mode">
                {(['list', 'calendar'] as const).map((m) => (
                  <button key={m} type="button" aria-pressed={mode === m} data-testid={`mode-${m}`}
                          onClick={() => setMode(m)}
                          className={`rounded-pill px-3 py-1 text-[13px] ${mode === m ? 'bg-accent/15 text-accent'
                            : 'text-ink-muted hover:text-ink'}`}>{m === 'list' ? 'List' : 'Calendar'}</button>
                ))}
              </span>
            )}
          </div>

          {view === 'bin' && (
            <div className="mb-3 flex flex-wrap items-center gap-3 text-[12px] text-ink-faint">
              {folder.kind === 'all'
                ? 'Deleted content stays here until you restore it or delete it forever — nothing is removed automatically.'
                : `What was deleted from ${folder.kind === 'niche' ? folderName : 'content with no niche'}. It stays until you restore it or delete it forever.`}
              {folder.kind === 'all' && (
                <Button tone="danger" className="ml-auto" data-testid="empty-bin" disabled={!items?.length}
                        onClick={() => setEmptying(true)}>Empty Recycle Bin</Button>
              )}
            </div>
          )}

          {error && <p className="mb-3 text-[13px] text-state-danger">{error}</p>}

          {view === 'analytics' ? (
            <AnalyticsView meta={metaNow} filters={scoped} from={fromIso} to={toIso} refreshKey={refreshKey}
                           onOpen={(id) => setOpen({ id })} />
          ) : view === 'scheduling' && mode === 'calendar' ? (
            <CalendarView filters={scoped} refreshKey={refreshKey} onOpen={(id) => setOpen({ id })}
                          onPickDay={(date) => setPickDay(date)} />
          ) : items === null ? (
            <p className="text-[13px] text-ink-faint">Reading…</p>
          ) : items.length === 0 ? (
            narrowed ? (
              <EmptyState title={`Nothing in ${view === 'bin' ? 'the Recycle Bin' : view === 'active' ? folderName
                : STAGE_LABEL[view]} matches.`} body="Try a different search, or clear a filter." />
            ) : view === 'active' ? (
              <EmptyState
                title={folder.kind === 'niche' ? `Nothing in “${folderName}” yet.` : folder.kind === 'none'
                  ? 'Everything has a niche.' : 'No content yet.'}
                body={folder.kind === 'none' ? 'Anything handed in without a niche shows up here.'
                  : 'Add a video, carousel, image or post with “+ Add” — or many files at once, each its own item.'}
                action={folder.kind === 'none' ? undefined : (
                  <span className="flex flex-wrap justify-center gap-2">
                    <Button tone="primary" data-testid="empty-add" onClick={() => setCreating({})}>+ Add content</Button>
                    <Button data-testid="empty-add-several" onClick={() => setSeveral(true)}>Add several files…</Button>
                  </span>
                )} />
            ) : <EmptyState title={EMPTY[view].title} body={EMPTY[view].body} />
          ) : (
            <>
              <p className="mb-2 text-[12px] text-ink-faint" data-testid="content-list-count">
                {total.toLocaleString()} item{total === 1 ? '' : 's'}
                {postsHere !== null && POSTS_WORD[view]
                  ? ` · ${postsHere.toLocaleString()} platform post${postsHere === 1 ? '' : 's'} ${POSTS_WORD[view]}` : ''}
                {items.length < total ? ` · showing ${items.length.toLocaleString()}` : ''}
              </p>
              <ul className="space-y-2" data-testid="content-list">
                {items.map((item) => (
                  <li key={item.id}>
                    <ItemCard item={item} view={view} showNiche={folder.kind === 'all'}
                              onOpen={() => setOpen({ id: item.id })} />
                  </li>
                ))}
              </ul>
              {items.length < total && (
                <div className="mt-3 flex justify-center">
                  <Button data-testid="show-more" disabled={loadingMore} onClick={() => void showMore()}>
                    {loadingMore ? 'Reading…' : `Show ${Math.min(PAGE, total - items.length)} more`}
                  </Button>
                </div>
              )}
            </>
          )}
        </>
      )}

      {dialogs}
      {pickDay && (
        <PickReady date={pickDay} filters={scoped} onClose={() => setPickDay(null)} onPick={(id) => {
          setPickDay(null);
          setOpen({ id, schedule: pickDay });
        }} />
      )}
      {emptying && (
        <ConfirmDialog title="Empty the Recycle Bin?" confirm="Delete all forever"
                       body={<>{total} item{total === 1 ? '' : 's'} and all of their files,
                         versions and history will be permanently deleted. This can&apos;t be undone.</>}
                       onClose={() => setEmptying(false)}
                       onConfirm={async () => {
                         await api.content.emptyBin();
                         bump();
                       }} />
      )}
    </div>
  );
}

function StageTab({
  id, label, count, active, alert = false, quiet = false, onClick,
}: {
  id: View; label: string; count?: number; active: boolean; alert?: boolean; quiet?: boolean; onClick: () => void;
}) {
  return (
    <button
      type="button"
      data-testid={`stage-${id}`}
      data-count={count}
      aria-pressed={active}
      onClick={onClick}
      className={`relative rounded-pill border px-3 py-1.5 text-[13px] transition-colors
        ${active ? 'border-accent/40 bg-accent/15 text-accent'
          : quiet ? 'border-transparent text-ink-faint hover:text-ink'
            : 'border-surface-border text-ink-muted hover:text-ink'}`}
    >
      {label}
      {count !== undefined && (
        <span className={`ml-1.5 text-[12px] ${active ? 'text-accent' : 'text-ink-faint'}`}>{count.toLocaleString()}</span>
      )}
      {alert && <span className="absolute -right-0.5 -top-0.5 h-2 w-2 rounded-full bg-state-warn" aria-label="needs you" />}
    </button>
  );
}

function TypeChip({
  label, count, active, testid, onClick,
}: { label: string; count?: number; active: boolean; testid: string; onClick: () => void }) {
  return (
    <button type="button" data-testid={testid} data-count={count} aria-pressed={active} onClick={onClick}
            className={`rounded-pill px-2.5 py-0.5 text-[12px] transition-colors ${active
              ? 'bg-white/[0.12] text-ink' : count === 0 ? 'text-ink-faint hover:bg-white/[0.05] hover:text-ink'
                : 'text-ink-muted hover:bg-white/[0.05] hover:text-ink'}`}>
      {label}
      {count !== undefined && <span className={`ml-1 ${active ? 'text-ink-muted' : 'text-ink-faint'}`}>{count}</span>}
    </button>
  );
}

function FilterSelect({
  testid, label, value, options, onChange,
}: {
  testid: string; label: string; value: string | undefined; options: [string, string][];
  onChange: (value: string | undefined) => void;
}) {
  return (
    <select data-testid={testid} className={compact} value={value ?? ''} aria-label={label}
            onChange={(e) => onChange(e.target.value || undefined)}>
      <option value="">{label}: All</option>
      {options.map(([id, text]) => <option key={id} value={id}>{text}</option>)}
    </select>
  );
}

/** In "All", an item is described by the stage it is in; elsewhere, by the
 *  view — in Ready to Post an item whose YouTube is scheduled still has TikTok
 *  waiting on you. */
function viewFor(item: ContentItem, view: View): View {
  return view === 'active' ? item.stage : view;
}

/** One line of the answer to "what is this, where is it, what next" — for the
 *  platforms this view is about. */
function stateLine(item: ContentItem, view: View): string {
  if (view === 'bin') return `Deleted ${when(item.deletedAt)} · was ${STAGE_LABEL[item.stage]}`;
  if (item.stage === 'archived') {
    return `Archived ${when(item.archivedAt)}${item.archivedFrom ? ` · was ${STAGE_LABEL[item.archivedFrom]}` : ''}`;
  }
  if (item.stage === 'review') return item.revision > 1 ? `Revision ${item.revision} — back for review` : 'New';
  if (item.stage === 'changes_requested' && item.openRequest) return `“${item.openRequest.what}”`;
  const here = VIEW_STATUSES[view as ContentStage];
  const mine = here ? item.placements.filter((p) => here.includes(p.status)) : item.placements;
  if (view === 'scheduling' && mine.length) {
    return mine.map((p) => `${p.platformLabel} ${p.status === 'scheduled' && !p.due
      ? inZone(p.scheduledAt, p.timezone) : PLACEMENT_LABEL[p.status].toLowerCase()}`).join(' · ');
  }
  if (view === 'published' && mine.length) {
    const latest = mine.map((p) => p.publishedAt ?? '').sort().pop();
    return `Published on ${mine.map((p) => p.platformLabel).join(', ')} · ${when(latest)}`;
  }
  if (view === 'approved' && mine.length) {
    const failed = mine.filter((p) => p.status === 'failed');
    const ready = mine.filter((p) => p.status !== 'failed');
    return [ready.length ? `Ready for ${ready.map((p) => p.platformLabel).join(', ')}` : '',
            failed.length ? `Failed on ${failed.map((p) => p.platformLabel).join(', ')}` : '']
      .filter(Boolean).join(' · ');
  }
  if (!item.placements.length) return `Approved ${when(item.approvedAt)} · no platforms yet`;
  return `Approved ${when(item.approvedAt)}`;
}

/** The next step, for the platforms THIS view is about. */
function nextHere(item: ContentItem, view: View): { step: string; who: string } {
  if (view === 'approved' && !item.deletedAt && item.stage !== 'archived') {
    const failed = item.placements.filter((p) => p.status === 'failed');
    const ready = item.placements.filter((p) => p.status === 'draft');
    if (failed.length) return { step: `Retry the failed post on ${failed.map((p) => p.platformLabel).join(', ')}`, who: 'You' };
    if (ready.length) return { step: `Post or schedule it on ${ready.map((p) => p.platformLabel).join(', ')}`, who: 'You' };
  }
  return item.next;
}

function ItemCard({
  item, view: shownIn, showNiche, onOpen,
}: { item: ContentItem; view: View; showNiche: boolean; onOpen: () => void }) {
  const thumb = previewImage(item);
  const view = viewFor(item, shownIn);
  const here = shownIn === 'active' ? undefined : VIEW_STATUSES[view as ContentStage];
  const next = nextHere(item, view);
  return (
    <Card interactive onClick={onOpen} data-testid="content-card" data-item-id={item.id} data-name={item.name}
          data-niche={item.niche} className="flex gap-3 p-3">
      <div className="flex h-16 w-24 shrink-0 items-center justify-center overflow-hidden rounded bg-black/30
                      text-[11px] text-ink-faint">
        {thumb ? (
          // eslint-disable-next-line @next/next/no-img-element
          <img src={thumb.url} alt="" loading="lazy" className="h-full w-full object-cover" />
        ) : item.typeLabel}
      </div>
      <div className="min-w-0 flex-1">
        <div className="flex items-baseline gap-2">
          <span className="truncate text-[14px] font-medium text-ink" title={item.name}>{item.name}</span>
          {(shownIn === 'active' || (shownIn !== item.stage && !here)) && (
            <span className={`shrink-0 text-[11px] ${STAGE_TONE[item.deletedAt ? 'bin' : item.stage]}`}
                  data-testid="card-stage">{STAGE_LABEL[item.deletedAt ? 'bin' : item.stage]}</span>
          )}
        </div>
        <div className="mt-0.5 flex flex-wrap items-center gap-1.5 text-[12px] text-ink-muted">
          <span className="rounded-pill bg-white/[0.06] px-2 py-px">{item.typeLabel}</span>
          {showNiche && item.niche && (
            <span className="max-w-[12rem] truncate rounded-pill bg-accent/10 px-2 py-px text-accent"
                  data-testid="card-niche" title={item.niche}>{item.niche}</span>
          )}
          {item.placements.map((p) => {
            // In a post-approval view, the platforms this view is ABOUT stand
            // out; the others (already out, or still waiting) step back.
            const inView = !here || here.includes(p.status);
            const tone = p.status === 'draft' && here ? 'text-ink' : PLACEMENT_TONE[p.status];
            return (
              <span key={p.id} data-testid="card-platform" data-in-view={inView ? 'true' : 'false'}
                    title={`${p.platformLabel}: ${PLACEMENT_LABEL[p.status]}`}
                    className={`rounded-pill px-2 py-px ${tone} ${inView
                      ? `border ${here ? 'border-ink-faint/60 bg-white/[0.07]' : 'border-surface-border'}`
                      : 'border border-transparent opacity-35'}`}>
                {p.platformLabel}</span>
            );
          })}
          {item.producer && <span className="text-ink-faint">by {item.producer === 'you' ? 'you' : item.producer}</span>}
        </div>
        <p className="mt-1 truncate text-[12px] text-ink-muted" data-testid="card-state">{stateLine(item, view)}</p>
        {item.attention.length > 0 && (
          <p className="mt-0.5 truncate text-[12px] text-state-warn" data-testid="card-attention">
            {item.attention.join(' · ')}</p>
        )}
      </div>
      <div className="hidden w-52 shrink-0 text-right text-[12px] sm:block">
        <span className="block text-ink-faint">Next</span>
        <span className="line-clamp-2 block text-ink" data-testid="card-next">{next.step}</span>
        {next.who && next.who !== '—' && (
          <span className="block truncate text-accent" data-testid="card-who">{next.who}</span>
        )}
      </div>
    </Card>
  );
}

/** From an empty calendar day: which approved item goes out then? Searchable,
 *  a page at a time — there can be hundreds — and from this folder only. */
function PickReady({
  date, filters: scope, onPick, onClose,
}: { date: string; filters: ContentFilters; onPick: (id: string) => void; onClose: () => void }) {
  const [ready, setReady] = useState<ContentItem[] | null>(null);
  const [q, setQ] = useState('');
  useEffect(() => {
    const timer = setTimeout(() => {
      // Ready to post, or already going out somewhere — another platform can
      // always be added for this day.
      const filters = { niche: scope.niche, noNiche: scope.noNiche, q: q.trim() || undefined, limit: 30 };
      Promise.all((['approved', 'scheduling', 'published'] as const).map((stage) => api.content.list(stage, filters)))
        .then((lists) => {
          const seen = new Set<string>();
          setReady(lists.flatMap((l) => l.items).filter((i) => !seen.has(i.id) && Boolean(seen.add(i.id))));
        })
        .catch(() => setReady([]));
    }, 200);
    return () => clearTimeout(timer);
  }, [q, scope.niche, scope.noNiche]);
  const label = new Date(`${date}T12:00:00`).toLocaleDateString(undefined, { dateStyle: 'full' });
  return (
    <Modal open title={`Schedule for ${label}`} onClose={onClose}>
      <input className={`${inputClass} mb-3`} placeholder="Search approved content…" value={q} autoFocus
             data-testid="pick-ready-search" onChange={(e) => setQ(e.target.value)} />
      {ready === null ? <p className="text-[13px] text-ink-faint">Reading…</p>
        : ready.length === 0 ? (
          <EmptyState title="Nothing is ready to post." body="Approve something in Review first." />
        ) : (
          <ul className="max-h-[55vh] space-y-2 overflow-y-auto" data-testid="pick-ready">
            {ready.map((item) => (
              <li key={item.id}>
                <Card interactive onClick={() => onPick(item.id)} className="p-3" data-testid="pick-ready-item">
                  <span className="text-[14px] text-ink">{item.name}</span>
                  <span className="ml-2 text-[12px] text-ink-faint">{item.typeLabel}{item.niche ? ` · ${item.niche}` : ''}</span>
                </Card>
              </li>
            ))}
          </ul>
        )}
    </Modal>
  );
}

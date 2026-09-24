'use client';

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { AnalyticsView } from '@/components/content/AnalyticsView';
import { CalendarView } from '@/components/content/CalendarView';
import { ConfirmDialog } from '@/components/content/Dialogs';
import {
  FLOW, inZone, PLACEMENT_LABEL, PLACEMENT_TONE, previewImage, STAGE_LABEL, STAGE_TONE, VIEW_STATUSES, when,
} from '@/components/content/format';
import { NewContentDialog } from '@/components/content/NewContentDialog';
import { Workspace } from '@/components/content/Workspace';
import { Button } from '@/components/ui/Button';
import { Card } from '@/components/ui/Card';
import { EmptyState } from '@/components/ui/EmptyState';
import { inputClass } from '@/components/ui/Field';
import { Modal } from '@/components/ui/Modal';
import { api, ApiRequestError } from '@/lib/api';
import type { ContentFilters, ContentItem, ContentMeta, ContentStage, ContentSummary } from '@/lib/api-types';

type View = ContentStage | 'bin' | 'analytics';

/** A page of cards: a busy stage holds thousands, and they arrive a page at a time. */
const PAGE = 50;

/** The shared input look, sized to its content: `inputClass` is full-width. */
const compact = inputClass.replace('w-full', 'w-auto');

const EMPTY: Record<Exclude<View, 'analytics'>, { title: string; body: string }> = {
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

/**
 * Content Management: one screen for the whole life of a piece of content —
 * review, changes, approval, scheduling, publishing, the archive, the recycle
 * bin, and what was reported about it afterwards.
 *
 * The lifecycle strip across the top IS the navigation. An item shows under
 * every stage one of its platforms is in (YouTube scheduled and TikTok
 * published: both). Niche, type and platform are filters over the one workflow
 * — never separate copies of it — so a hundred niches cost nothing here.
 *
 * "New content" is the person doing what agents and Jarvis already do: handing
 * something in, through the same door, into the same pipeline.
 */
export function ContentScreen({ onNavigate }: { onNavigate: (id: string) => void }) {
  const [meta, setMeta] = useState<ContentMeta | null>(null);
  const [view, setView] = useState<View>('review');
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
  const [creating, setCreating] = useState(false);
  const [pickDay, setPickDay] = useState<string | null>(null);
  const [emptying, setEmptying] = useState(false);

  const bump = useCallback(() => setRefreshKey((k) => k + 1), []);

  const loadMeta = useCallback(() => {
    api.content.meta().then(setMeta).catch((err) =>
      setError(err instanceof ApiRequestError ? err.message : 'Could not read Content Management.'));
  }, []);
  useEffect(loadMeta, [loadMeta]);

  // Search waits for a pause in typing rather than asking on every key.
  useEffect(() => {
    const timer = setTimeout(() => setFilters((f) => ({ ...f, q: search.trim() || undefined })), 250);
    return () => clearTimeout(timer);
  }, [search]);

  const fromIso = dates.from ? new Date(`${dates.from}T00:00:00`).toISOString() : undefined;
  const toIso = dates.to ? new Date(`${dates.to}T23:59:59`).toISOString() : undefined;
  const listed = view === 'analytics' ? null : view;
  const query = useMemo(() => ({
    ...filters, from: fromIso, to: toIso,
    archivedFrom: view === 'archived' ? archivedFrom || undefined : undefined,
  }), [filters, fromIso, toIso, view, archivedFrom]);

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
    try {
      const counts = api.content.summary(filters);
      if (listed) {
        const list = await api.content.list(listed, { ...query, limit: wanted.current });
        setItems(list.items);
        setTotal(list.total ?? list.items.length);
      }
      setSummary(await counts);
      setError(null);
    } catch (err) {
      setError(err instanceof ApiRequestError ? err.message : 'Could not read your content.');
      setItems([]);
    }
  }, [listed, query, filters]);

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

  if (!meta) {
    return error ? <p className="text-[13px] text-state-danger">{error}</p>
      : <p className="text-[13px] text-ink-faint">Reading…</p>;
  }

  const dated = view === 'published' || view === 'archived' || view === 'bin' || view === 'analytics';
  const postsHere = summary?.posts && view in summary.posts
    ? summary.posts[view as keyof ContentSummary['posts']] : null;

  return (
    <div data-testid="content-screen">
      <div className="mb-4 flex flex-wrap items-center gap-3">
        <p className="text-[13px]" data-testid="content-needs">
          {needsParts.length ? (
            <>
              <span className="text-ink-faint">Needs you: </span>
              {needsParts.map((part, i) => (
                <span key={part.label}>
                  {i > 0 && <span className="text-ink-faint"> · </span>}
                  <button type="button" className="text-state-warn underline-offset-2 hover:underline"
                          onClick={() => setView(part.go)}>{part.label}</button>
                </span>
              ))}
            </>
          ) : <span className="text-ink-faint">Nothing needs you right now.</span>}
        </p>
        <Button tone="primary" className="ml-auto" data-testid="new-content" onClick={() => setCreating(true)}>
          + New content</Button>
      </div>

      {/* The lifecycle, left to right. */}
      <nav className="mb-4 flex flex-wrap items-center gap-1.5" data-testid="content-strip" aria-label="Content stages">
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

      {/* Filters apply to every stage, and the counts above follow them. */}
      <div className="mb-4 flex flex-wrap items-center gap-2" data-testid="content-filters">
        <input data-testid="content-search" className={`${inputClass} max-w-[16rem]`} placeholder="Search…"
               value={search} onChange={(e) => setSearch(e.target.value)} />
        <FilterSelect testid="filter-niche" label="Niche" value={filters.niche}
                      options={meta.niches.map((n) => [n, n])}
                      onChange={(v) => setFilters((f) => ({ ...f, niche: v }))} />
        <FilterSelect testid="filter-type" label="Type" value={filters.type}
                      options={Object.entries(meta.types).map(([id, t]) => [id, t.label])}
                      onChange={(v) => setFilters((f) => ({ ...f, type: v }))} />
        <FilterSelect testid="filter-platform" label="Platform" value={filters.platform}
                      options={Object.entries(meta.platforms).map(([id, p]) => [id, p.label])}
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
        <div className="mb-3 flex items-center gap-3 text-[12px] text-ink-faint">
          Deleted content stays here until you restore it or delete it forever — nothing is removed automatically.
          <Button tone="danger" className="ml-auto" data-testid="empty-bin" disabled={!items?.length}
                  onClick={() => setEmptying(true)}>Empty Recycle Bin</Button>
        </div>
      )}

      {error && <p className="mb-3 text-[13px] text-state-danger">{error}</p>}

      {view === 'analytics' ? (
        <AnalyticsView meta={meta} filters={filters} from={fromIso} to={toIso} refreshKey={refreshKey}
                       onOpen={(id) => setOpen({ id })} />
      ) : view === 'scheduling' && mode === 'calendar' ? (
        <CalendarView filters={filters} refreshKey={refreshKey} onOpen={(id) => setOpen({ id })}
                      onPickDay={(date) => setPickDay(date)} />
      ) : items === null ? (
        <p className="text-[13px] text-ink-faint">Reading…</p>
      ) : items.length === 0 ? (
        filters.q || filters.niche || filters.type || filters.platform || dates.from || dates.to || archivedFrom
          ? <EmptyState title={`Nothing in ${view === 'bin' ? 'the Recycle Bin' : STAGE_LABEL[view]} matches.`}
                        body="Try a different search, or clear a filter." />
          : <EmptyState title={EMPTY[view].title} body={EMPTY[view].body} />
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
                <ItemCard item={item} view={view} onOpen={() => setOpen({ id: item.id })} />
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

      {open && (
        <Workspace itemId={open.id} meta={meta} refreshKey={refreshKey} initialSchedule={open.schedule}
                   onClose={() => setOpen(null)} onChanged={bump} onNavigate={onNavigate} />
      )}
      {creating && (
        <NewContentDialog meta={meta} onClose={() => setCreating(false)} onCreated={(id) => {
          loadMeta();  // a new niche is one to filter by
          bump();
          setOpen({ id });
        }} />
      )}
      {pickDay && (
        <PickReady date={pickDay} onClose={() => setPickDay(null)} onPick={(id) => {
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

/** The next step, for the platforms THIS view is about: in Ready to Post an
 *  item whose YouTube is scheduled still has TikTok waiting on you. */
function nextHere(item: ContentItem, view: View): { step: string; who: string } {
  if (view === 'approved' && !item.deletedAt && item.stage !== 'archived') {
    const failed = item.placements.filter((p) => p.status === 'failed');
    const ready = item.placements.filter((p) => p.status === 'draft');
    if (failed.length) return { step: `Retry the failed post on ${failed.map((p) => p.platformLabel).join(', ')}`, who: 'You' };
    if (ready.length) return { step: `Post or schedule it on ${ready.map((p) => p.platformLabel).join(', ')}`, who: 'You' };
  }
  return item.next;
}

function ItemCard({ item, view, onOpen }: { item: ContentItem; view: View; onOpen: () => void }) {
  const thumb = previewImage(item);
  const here = VIEW_STATUSES[view as ContentStage];
  const next = nextHere(item, view);
  return (
    <Card interactive onClick={onOpen} data-testid="content-card" data-item-id={item.id} data-name={item.name}
          className="flex gap-3 p-3">
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
          {view !== item.stage && !here && (
            <span className={`shrink-0 text-[11px] ${STAGE_TONE[item.deletedAt ? 'bin' : item.stage]}`}>
              {STAGE_LABEL[item.stage]}</span>
          )}
        </div>
        <div className="mt-0.5 flex flex-wrap items-center gap-1.5 text-[12px] text-ink-muted">
          <span className="rounded-pill bg-white/[0.06] px-2 py-px">{item.typeLabel}</span>
          {item.niche && <span className="rounded-pill bg-accent/10 px-2 py-px text-accent" data-testid="card-niche">
            {item.niche}</span>}
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
 *  and a page at a time — there can be hundreds. */
function PickReady({ date, onPick, onClose }: { date: string; onPick: (id: string) => void; onClose: () => void }) {
  const [ready, setReady] = useState<ContentItem[] | null>(null);
  const [q, setQ] = useState('');
  useEffect(() => {
    const timer = setTimeout(() => {
      // Ready to post, or already going out somewhere — another platform can
      // always be added for this day.
      const filters = { q: q.trim() || undefined, limit: 30 };
      Promise.all((['approved', 'scheduling', 'published'] as const).map((stage) => api.content.list(stage, filters)))
        .then((lists) => {
          const seen = new Set<string>();
          setReady(lists.flatMap((l) => l.items).filter((i) => !seen.has(i.id) && Boolean(seen.add(i.id))));
        })
        .catch(() => setReady([]));
    }, 200);
    return () => clearTimeout(timer);
  }, [q]);
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

'use client';

import { useMemo, useState } from 'react';

import { Button } from '@/components/ui/Button';
import { Card } from '@/components/ui/Card';
import { EmptyState } from '@/components/ui/EmptyState';
import { inputClass } from '@/components/ui/Field';
import type { ContentFolder, ContentMeta, ContentNicheOverview } from '@/lib/api-types';

/** "Sep 23" this year, "Sep 23, 2025" otherwise: a card's footer, one line. */
function day(iso: string): string {
  const date = new Date(iso);
  const sameYear = date.getFullYear() === new Date().getFullYear();
  return date.toLocaleDateString(undefined, { month: 'short', day: 'numeric', ...(sameYear ? {} : { year: 'numeric' }) });
}

/** Where the person is: the folders, or inside one of them. */
export type Folder = { kind: 'all' } | { kind: 'none' } | { kind: 'niche'; name: string };

/** Past this many niches the grid gets a search box. */
const FIND_FROM = 9;

/** "Video 22 · Carousel 6 · Blog / Article 4" — counts after labels, so no
 *  label ever needs a plural. Largest first. */
export function typeCounts(folder: ContentFolder, meta: ContentMeta): { id: string; label: string; n: number }[] {
  return Object.entries(folder.byType)
    .map(([id, n]) => ({ id, label: meta.types[id]?.label ?? id, n }))
    .sort((a, b) => b.n - a.n || a.label.localeCompare(b.label));
}

/**
 * The first screen of Content Management: one folder per niche. A niche is a
 * workspace holding as many separate pieces of content as it needs — every one
 * of them its own item in the one pipeline. "All content" is every folder at
 * once; "No niche" holds whatever was handed in without one.
 */
export function NichesView({
  overview, meta, onOpen, onNew,
}: {
  overview: ContentNicheOverview;
  meta: ContentMeta;
  onOpen: (folder: Folder) => void;
  onNew: () => void;
}) {
  const [find, setFind] = useState('');
  const shown = useMemo(() => {
    const q = find.trim().toLowerCase();
    return q ? overview.niches.filter((n) => n.name.toLowerCase().includes(q)) : overview.niches;
  }, [overview.niches, find]);
  const loose = overview.none.total + overview.none.archived + overview.none.binned;

  if (!overview.niches.length && !overview.all.total && !loose) {
    return (
      <EmptyState title="No niches yet."
                  body="A niche is a folder for one topic — Psychology, Fitness, Cooking — holding all of its videos, carousels, images and posts together."
                  action={<Button tone="primary" data-testid="new-niche-empty" onClick={onNew}>+ New niche</Button>} />
    );
  }

  return (
    <div data-testid="niches">
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <FolderRow testid="folder-all" title="All content" folder={overview.all} meta={meta}
                   hint="Every niche together" onClick={() => onOpen({ kind: 'all' })} />
      </div>
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <h2 className="text-[11px] font-semibold uppercase tracking-[0.16em] text-ink-faint">
          Niches <span className="ml-1 font-normal tracking-normal">{overview.niches.length}</span></h2>
        {overview.niches.length >= FIND_FROM && (
          <input data-testid="niche-find" className={`${inputClass} ml-2 max-w-[14rem]`} placeholder="Find a niche…"
                 value={find} onChange={(e) => setFind(e.target.value)} />
        )}
      </div>
      {shown.length === 0 && find ? (
        <p className="mb-4 text-[13px] text-ink-faint">No niche is called anything like “{find}”.</p>
      ) : (
        <ul className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-3" data-testid="niche-grid">
          {shown.map((niche) => (
            <li key={niche.name}>
              <FolderCard name={niche.name} folder={niche} meta={meta}
                          onClick={() => onOpen({ kind: 'niche', name: niche.name })} />
            </li>
          ))}
          {!find && (
            <li>
              <button type="button" data-testid="new-niche-card" onClick={onNew}
                      className="flex h-full min-h-[7.5rem] w-full items-center justify-center rounded-lg border
                                 border-dashed border-surface-border text-[13px] text-ink-muted transition
                                 hover:border-surface-border-strong hover:text-ink">
                + New niche
              </button>
            </li>
          )}
        </ul>
      )}
      {loose > 0 && (
        <div className="mt-4">
          <FolderRow testid="folder-none" title="No niche" folder={overview.none} meta={meta}
                     hint="Handed in without a niche — open one and give it a niche to file it"
                     onClick={() => onOpen({ kind: 'none' })} />
        </div>
      )}
    </div>
  );
}

function FolderCard({
  name, folder, meta, onClick,
}: { name: string; folder: ContentFolder; meta: ContentMeta; onClick: () => void }) {
  const types = typeCounts(folder, meta);
  const aside = [folder.archived ? `${folder.archived} archived` : '',
                 folder.binned ? `${folder.binned} in the Recycle Bin` : ''].filter(Boolean).join(' · ');
  return (
    <Card interactive onClick={onClick} data-testid="niche-card" data-niche={name} data-total={folder.total}
          className="flex h-full min-h-[7.5rem] flex-col p-4">
      <div className="flex items-baseline gap-2">
        <span className="min-w-0 flex-1 truncate text-[15px] font-medium text-ink" title={name}>{name}</span>
        {folder.review > 0 && (
          <span className="shrink-0 rounded-pill bg-state-warn/15 px-2 py-px text-[11px] text-state-warn"
                data-testid="niche-review">{folder.review} in Review</span>
        )}
      </div>
      <p className="mt-0.5 text-[12px] text-ink-muted" data-testid="niche-total">
        {folder.total > 0 ? `${folder.total.toLocaleString()} item${folder.total === 1 ? '' : 's'}`
          : folder.archived ? 'Everything here is archived' : folder.binned ? 'Only deleted content' : 'Empty'}
      </p>
      {types.length > 0 && (
        <p className="mt-2 flex flex-wrap gap-1 text-[11px]" data-testid="niche-types">
          {types.map((t) => (
            <span key={t.id} className="rounded-pill bg-white/[0.06] px-2 py-px text-ink-muted">
              {t.label} <span className="text-ink">{t.n}</span></span>
          ))}
        </p>
      )}
      <p className="mt-auto pt-2 text-[11px] text-ink-faint">
        {[aside, folder.updatedAt ? `Updated ${day(folder.updatedAt)}` : 'Open it to add content']
          .filter(Boolean).join(' · ')}
      </p>
    </Card>
  );
}

/** "All content" and "No niche": a full-width row rather than one card among the niches. */
function FolderRow({
  testid, title, hint, folder, meta, onClick,
}: {
  testid: string; title: string; hint: string; folder: ContentFolder; meta: ContentMeta; onClick: () => void;
}) {
  const types = typeCounts(folder, meta);
  return (
    <Card interactive onClick={onClick} data-testid={testid} data-total={folder.total}
          className="flex w-full flex-wrap items-center gap-x-4 gap-y-1 px-4 py-3">
      <span className="text-[14px] font-medium text-ink">{title}</span>
      <span className="text-[12px] text-ink-muted">
        {folder.total.toLocaleString()} item{folder.total === 1 ? '' : 's'}
        {folder.review > 0 && <span className="text-state-warn"> · {folder.review} in Review</span>}
      </span>
      <span className="hidden min-w-0 flex-1 truncate text-[12px] text-ink-faint md:block">
        {types.map((t) => `${t.label} ${t.n}`).join(' · ') || hint}</span>
      <span className="ml-auto text-[12px] text-accent">Open →</span>
    </Card>
  );
}

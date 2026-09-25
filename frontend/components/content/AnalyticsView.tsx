'use client';

import { useEffect, useMemo, useState } from 'react';

import { EmptyState } from '@/components/ui/EmptyState';
import { api, ApiRequestError } from '@/lib/api';
import type { ContentAnalytics, ContentFilters, ContentMeta } from '@/lib/api-types';

import { actorLabel, metricText, when } from './format';

const COLUMNS = ['views', 'likes', 'comments', 'shares', 'saves', 'reach'];

/**
 * What went out, where, when, its link — and the numbers someone REPORTED for
 * it. Nothing here is estimated: a post nobody reported numbers for shows a
 * dash, never a zero, and the totals add up only what was reported.
 */
export function AnalyticsView({
  meta, filters, from, to, refreshKey, onOpen,
}: {
  meta: ContentMeta;
  filters: ContentFilters;
  from?: string;
  to?: string;
  refreshKey: number;
  onOpen: (itemId: string) => void;
}) {
  const [data, setData] = useState<ContentAnalytics | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [sort, setSort] = useState<string>('publishedAt');

  const { niche, noNiche, type, platform, q } = filters;
  useEffect(() => {
    api.content.analytics({ niche, noNiche, type, platform, q, from, to })
      .then((d) => {
        setData(d);
        setError(null);
      })
      .catch((err) => setError(err instanceof ApiRequestError ? err.message : 'Could not read the numbers.'));
  }, [niche, noNiche, type, platform, q, from, to, refreshKey]);

  const posts = useMemo(() => {
    if (!data) return [];
    if (sort === 'publishedAt') return data.posts;
    return [...data.posts].sort((a, b) => (b.metrics?.values[sort] ?? -1) - (a.metrics?.values[sort] ?? -1));
  }, [data, sort]);

  if (error) return <p className="text-[13px] text-state-danger">{error}</p>;
  if (!data) return <p className="text-[13px] text-ink-faint">Reading…</p>;
  if (data.posts.length === 0) {
    return <EmptyState title="Nothing published yet."
                       body="Once something goes out, it is listed here with its link and any numbers reported for it." />;
  }

  const extra = Object.keys(data.totals).filter((k) => !COLUMNS.includes(k));
  return (
    <div data-testid="content-analytics">
      <div className="mb-4 grid grid-cols-2 gap-3 sm:grid-cols-4 xl:grid-cols-8">
        <Tile label="Posts out" value={data.posts.length.toLocaleString()} />
        <Tile label="Reported" value={`${data.reported.toLocaleString()}`}
              hint={data.reported < data.posts.length ? `${data.posts.length - data.reported} without numbers` : undefined} />
        {COLUMNS.map((key) => (
          <Tile key={key} label={meta.metrics[key]?.label ?? key}
                value={data.totals[key] !== undefined ? metricText(key, data.totals[key]!) : '—'} />
        ))}
      </div>
      {extra.length > 0 && (
        <p className="mb-3 text-[12px] text-ink-muted">
          Also reported: {extra.map((k) => `${meta.metrics[k]?.label ?? k} ${metricText(k, data.totals[k]!)}`).join(' · ')}
        </p>
      )}

      {data.byPlatform.length > 1 && (
        <div className="mb-4 flex flex-wrap gap-2" data-testid="analytics-by-platform">
          {data.byPlatform.map((g) => (
            <span key={g.platform} className="rounded-pill border border-surface-border px-3 py-1 text-[12px] text-ink-muted">
              <span className="text-ink">{g.platformLabel}</span> · {g.posts} post{g.posts === 1 ? '' : 's'}
              {g.totals.views !== undefined ? ` · ${metricText('views', g.totals.views)} views` : ''}
            </span>
          ))}
        </div>
      )}

      <div className="overflow-x-auto rounded border border-surface-border">
        <table className="w-full min-w-[760px] text-left text-[13px]" data-testid="analytics-table">
          <thead className="bg-white/[0.03] text-[11px] uppercase tracking-wider text-ink-faint">
            <tr>
              <th className="px-3 py-2 font-medium">Content</th>
              <th className="px-3 py-2 font-medium">Platform</th>
              <th className="px-3 py-2 font-medium">
                <button type="button" onClick={() => setSort('publishedAt')}
                        className={sort === 'publishedAt' ? 'text-accent' : ''}>Published</button>
              </th>
              {COLUMNS.map((key) => (
                <th key={key} className="px-3 py-2 text-right font-medium">
                  <button type="button" onClick={() => setSort(key)} className={sort === key ? 'text-accent' : ''}>
                    {meta.metrics[key]?.label ?? key}</button>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {posts.map((post) => {
              const safe = post.publishedUrl && /^https?:\/\//i.test(post.publishedUrl) ? post.publishedUrl : null;
              return (
                <tr key={post.placementId} className="border-t border-surface-border hover:bg-white/[0.02]"
                    data-testid="analytics-row">
                  <td className="max-w-[18rem] px-3 py-2">
                    <button type="button" className="block max-w-full truncate text-left text-ink hover:underline"
                            title={post.name} onClick={() => onOpen(post.itemId)}>{post.name}</button>
                    <span className="text-[11px] text-ink-faint">{post.typeLabel}{post.niche ? ` · ${post.niche}` : ''}</span>
                  </td>
                  <td className="px-3 py-2 text-ink-muted">
                    {post.platformLabel}{post.destination ? ` · ${post.destination}` : ''}
                    {safe && <a className="block text-[11px] text-accent underline" href={safe} target="_blank"
                                rel="noopener noreferrer">Open post</a>}
                  </td>
                  <td className="whitespace-nowrap px-3 py-2 text-ink-muted">
                    {when(post.publishedAt)}
                    {post.metrics && (
                      <span className="block text-[11px] text-ink-faint">
                        numbers {when(post.metrics.capturedAt)} · {actorLabel(post.metrics.source)}
                      </span>
                    )}
                  </td>
                  {COLUMNS.map((key) => {
                    const value = post.metrics?.values[key];
                    return (
                      <td key={key} className="px-3 py-2 text-right tabular-nums text-ink">
                        {value !== undefined ? metricText(key, value) : <span className="text-ink-faint">—</span>}
                      </td>
                    );
                  })}
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <p className="mt-2 text-[12px] text-ink-faint">
        Numbers come from whatever reports them — a publishing tool, an agent, Jarvis, or you on a post&apos;s page.
        Nothing is fetched from the platforms by itself yet.
      </p>
    </div>
  );
}

function Tile({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="rounded border border-surface-border p-3">
      <div className="text-[11px] uppercase tracking-wider text-ink-faint">{label}</div>
      <div className="mt-1 text-[20px] tabular-nums text-ink">{value}</div>
      {hint && <div className="text-[11px] text-ink-faint">{hint}</div>}
    </div>
  );
}

'use client';

import { useEffect, useMemo, useState } from 'react';

import { Button } from '@/components/ui/Button';
import { inputClass } from '@/components/ui/Field';
import { Modal } from '@/components/ui/Modal';
import { api, ApiRequestError } from '@/lib/api';
import type { ContentCalendarEntry, ContentFilters } from '@/lib/api-types';

import { PLACEMENT_TONE } from './format';

const DAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
/** A busy day shows this many posts in its cell; the rest are one click away. */
const PER_DAY = 4;
/** The year picker offers this many years back, and ahead, of this year. */
const YEARS_BACK = 10;
const YEARS_AHEAD = 5;

function ymd(d: Date): string {
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

/**
 * A month of posts, in your own local time: what is scheduled, what went out.
 * Click a post to open it; click an empty future day to schedule something that
 * is ready to post for that day.
 */
export function CalendarView({
  filters, refreshKey, onOpen, onPickDay,
}: {
  filters: ContentFilters;
  refreshKey: number;
  onOpen: (itemId: string) => void;
  onPickDay: (date: string) => void;
}) {
  const [month, setMonth] = useState(() => {
    const now = new Date();
    return new Date(now.getFullYear(), now.getMonth(), 1);
  });
  const [entries, setEntries] = useState<ContentCalendarEntry[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [dayOpen, setDayOpen] = useState<string | null>(null);

  const gridStart = useMemo(() => {
    const first = new Date(month);
    const shift = (first.getDay() + 6) % 7; // Monday first
    return new Date(first.getFullYear(), first.getMonth(), 1 - shift);
  }, [month]);
  const days = useMemo(() => Array.from({ length: 42 }, (_, i) =>
    new Date(gridStart.getFullYear(), gridStart.getMonth(), gridStart.getDate() + i)), [gridStart]);

  const { niche, noNiche, type, platform, q } = filters;
  useEffect(() => {
    const end = new Date(gridStart.getFullYear(), gridStart.getMonth(), gridStart.getDate() + 42);
    api.content.calendar(gridStart.toISOString(), end.toISOString(), { niche, noNiche, type, platform, q })
      .then((r) => {
        setEntries(r.entries);
        setError(null);
      })
      .catch((err) => setError(err instanceof ApiRequestError ? err.message : 'Could not read the calendar.'));
  }, [gridStart, niche, noNiche, type, platform, q, refreshKey]);

  const byDay = useMemo(() => {
    const map = new Map<string, ContentCalendarEntry[]>();
    for (const entry of entries) {
      const at = entry.publishedAt ?? entry.scheduledAt;
      if (!at) continue;
      const key = ymd(new Date(at));
      map.set(key, [...(map.get(key) ?? []), entry]);
    }
    return map;
  }, [entries]);

  const today = ymd(new Date());
  // Years to jump to: around this one — and the one on screen, however far the arrows went.
  const years = useMemo(() => {
    const now = new Date().getFullYear();
    const shown = month.getFullYear();
    const first = Math.min(now - YEARS_BACK, shown);
    const last = Math.max(now + YEARS_AHEAD, shown);
    return Array.from({ length: last - first + 1 }, (_, i) => first + i);
  }, [month]);

  return (
    <div data-testid="content-calendar">
      <div className="mb-3 flex items-center gap-2">
        <Button data-testid="cal-prev" onClick={() => setMonth(new Date(month.getFullYear(), month.getMonth() - 1, 1))}>
          ‹</Button>
        <h3 className="min-w-[6.5rem] text-center text-[15px] font-medium text-ink" data-testid="cal-month">
          {month.toLocaleDateString(undefined, { month: 'long' })}
        </h3>
        {/* The same month, in another year — without clicking through every month between. */}
        <select data-testid="cal-year" aria-label="Year" value={month.getFullYear()}
                className={`${inputClass.replace('w-full', 'w-auto')} py-1`}
                onChange={(e) => setMonth(new Date(Number(e.target.value), month.getMonth(), 1))}>
          {years.map((y) => <option key={y} value={y}>{y}</option>)}
        </select>
        <Button data-testid="cal-next" onClick={() => setMonth(new Date(month.getFullYear(), month.getMonth() + 1, 1))}>
          ›</Button>
        <Button onClick={() => {
          const now = new Date();
          setMonth(new Date(now.getFullYear(), now.getMonth(), 1));
        }}>Today</Button>
        <span className="ml-auto text-[12px] text-ink-faint">Times are your local time.</span>
      </div>
      {error && <p className="mb-2 text-[13px] text-state-danger">{error}</p>}
      <div className="grid grid-cols-7 gap-px overflow-hidden rounded border border-surface-border bg-surface-border">
        {DAYS.map((d) => (
          <div key={d} className="bg-surface-raised px-2 py-1 text-[11px] uppercase tracking-wider text-ink-faint">{d}</div>
        ))}
        {days.map((day) => {
          const key = ymd(day);
          const inMonth = day.getMonth() === month.getMonth();
          const list = byDay.get(key) ?? [];
          const future = key >= today;
          return (
            <div
              key={key}
              data-testid="cal-day"
              data-date={key}
              className={`min-h-[92px] bg-surface p-1.5 ${inMonth ? '' : 'opacity-45'}
                          ${future ? 'cursor-pointer hover:bg-white/[0.03]' : ''}`}
              onClick={() => future && onPickDay(key)}
            >
              <div className={`mb-1 text-[11px] ${key === today ? 'font-semibold text-accent' : 'text-ink-faint'}`}>
                {day.getDate()}
              </div>
              <ul className="space-y-1">
                {list.slice(0, PER_DAY).map((e) => (
                  <li key={e.id}><Entry entry={e} onOpen={onOpen} /></li>
                ))}
              </ul>
              {list.length > PER_DAY && (
                <button type="button" data-testid="cal-more"
                        className="mt-1 w-full rounded px-1.5 py-0.5 text-left text-[11px] text-accent hover:bg-white/[0.06]"
                        onClick={(ev) => {
                          ev.stopPropagation();
                          setDayOpen(key);
                        }}>
                  +{list.length - PER_DAY} more
                </button>
              )}
            </div>
          );
        })}
      </div>
      {dayOpen && (
        <Modal open title={new Date(`${dayOpen}T12:00:00`).toLocaleDateString(undefined, { dateStyle: 'full' })}
               onClose={() => setDayOpen(null)}>
          <p className="mb-2 text-[12px] text-ink-faint">{(byDay.get(dayOpen) ?? []).length} posts</p>
          <ul className="max-h-[60vh] space-y-1 overflow-y-auto" data-testid="cal-day-list">
            {(byDay.get(dayOpen) ?? []).map((e) => (
              <li key={e.id}><Entry entry={e} onOpen={(id) => {
                setDayOpen(null);
                onOpen(id);
              }} wide /></li>
            ))}
          </ul>
        </Modal>
      )}
    </div>
  );
}

function Entry({ entry: e, onOpen, wide = false }: {
  entry: ContentCalendarEntry; onOpen: (itemId: string) => void; wide?: boolean;
}) {
  const at = new Date(e.publishedAt ?? e.scheduledAt ?? '');
  return (
    <button
      type="button"
      data-testid="cal-entry"
      data-item-id={e.itemId}
      data-status={e.status}
      onClick={(ev) => {
        ev.stopPropagation();
        onOpen(e.itemId);
      }}
      className={`w-full truncate rounded bg-white/[0.05] px-1.5 py-0.5 text-left hover:bg-white/[0.1]
                  ${wide ? 'py-1.5 text-[13px]' : 'text-[11px]'}`}
      title={`${e.itemName} — ${e.platformLabel}`}
    >
      <span className={e.due ? 'text-state-warn' : PLACEMENT_TONE[e.status]}>
        {at.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })}
      </span>{' '}
      <span className="text-ink-muted">{e.platformLabel}</span>{' '}
      <span className="text-ink">{e.itemName}</span>
    </button>
  );
}

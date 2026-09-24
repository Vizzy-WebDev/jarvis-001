import type { ContentItem, ContentMedia, ContentStage, PlacementStatus } from '@/lib/api-types';

/** The lifecycle, in order. Approved and Ready to Post are one state. */
export const FLOW: { id: ContentStage; label: string }[] = [
  { id: 'review', label: 'Review' },
  { id: 'changes_requested', label: 'Changes Requested' },
  { id: 'approved', label: 'Ready to Post' },
  { id: 'scheduling', label: 'Scheduling' },
  { id: 'published', label: 'Published' },
];

export const STAGE_LABEL: Record<ContentStage | 'bin', string> = {
  review: 'Review',
  changes_requested: 'Changes Requested',
  approved: 'Ready to Post',
  scheduling: 'Scheduling',
  published: 'Published',
  archived: 'Archived',
  bin: 'Recycle Bin',
};

export const STAGE_TONE: Record<ContentStage | 'bin', string> = {
  review: 'text-accent',
  changes_requested: 'text-state-warn',
  approved: 'text-state-ok',
  scheduling: 'text-accent',
  published: 'text-state-ok',
  archived: 'text-ink-faint',
  bin: 'text-state-danger',
};

export const PLACEMENT_LABEL: Record<PlacementStatus, string> = {
  draft: 'Not scheduled',
  scheduled: 'Scheduled',
  queued: 'Waiting for publisher',
  publishing: 'Being posted',
  published: 'Published',
  failed: 'Failed',
};

export const PLACEMENT_TONE: Record<PlacementStatus, string> = {
  draft: 'text-ink-faint',
  scheduled: 'text-accent',
  queued: 'text-state-warn',
  publishing: 'text-state-warn',
  published: 'text-state-ok',
  failed: 'text-state-danger',
};

export function when(iso: string | null | undefined): string {
  if (!iso) return '';
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return '';
  return date.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' });
}

/** A moment shown in the timezone it was scheduled in, with that zone named. */
export function inZone(iso: string | null | undefined, zone: string | null | undefined): string {
  if (!iso) return '';
  const date = new Date(iso);
  try {
    return date.toLocaleString(undefined, {
      dateStyle: 'medium', timeStyle: 'short', timeZone: zone || undefined, timeZoneName: 'short',
    });
  } catch {
    return when(iso);
  }
}

export function browserZone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
  } catch {
    return 'UTC';
  }
}

export function allZones(): string[] {
  const withIntl = Intl as unknown as { supportedValuesOf?: (key: string) => string[] };
  try {
    const zones = withIntl.supportedValuesOf?.('timeZone');
    if (zones?.length) return zones.includes('UTC') ? zones : ['UTC', ...zones];
  } catch {
    /* fall through */
  }
  return ['UTC', 'Europe/London', 'Europe/Paris', 'Africa/Lagos', 'America/New_York',
    'America/Chicago', 'America/Los_Angeles', 'Asia/Dubai', 'Asia/Kolkata', 'Asia/Tokyo',
    'Australia/Sydney'];
}

/** How far `zone` is ahead of UTC at the instant `utcMs`, in ms. */
function offsetAt(utcMs: number, zone: string): number {
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone: zone, hourCycle: 'h23', year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit',
  }).formatToParts(new Date(utcMs));
  const get = (type: string) => Number(parts.find((p) => p.type === type)?.value);
  const asUtc = Date.UTC(get('year'), get('month') - 1, get('day'), get('hour'), get('minute'), get('second'));
  return asUtc - utcMs;
}

/**
 * A wall-clock date and time in a named timezone, as the UTC instant it means.
 * Done here with `Intl` because the backend runs on Windows too, where Python
 * has no timezone database: the server only ever compares UTC instants.
 */
export function zonedToUtc(date: string, time: string, zone: string): string | null {
  const [y, m, d] = date.split('-').map(Number);
  const [hh, mm] = time.split(':').map(Number);
  if (!y || !m || !d || Number.isNaN(hh) || Number.isNaN(mm)) return null;
  const wall = Date.UTC(y, m - 1, d, hh, mm);
  let utc = wall - offsetAt(wall, zone);
  const second = wall - offsetAt(utc, zone);
  if (second !== utc) utc = second;
  return new Date(utc).toISOString();
}

/** The date and time an instant shows as on a clock in `zone`. */
export function utcToZoned(iso: string, zone: string): { date: string; time: string } {
  const ms = new Date(iso).getTime();
  const local = new Date(ms + offsetAt(ms, zone));
  const pad = (n: number) => String(n).padStart(2, '0');
  return {
    date: `${local.getUTCFullYear()}-${pad(local.getUTCMonth() + 1)}-${pad(local.getUTCDate())}`,
    time: `${pad(local.getUTCHours())}:${pad(local.getUTCMinutes())}`,
  };
}

export function actorLabel(actor: string): string {
  if (actor === 'you') return 'You';
  if (actor === 'jarvis') return 'Jarvis';
  return actor;
}

/** The image that best represents an item in a list, if it has one. */
export function previewImage(item: { media: ContentMedia[] }): ContentMedia | null {
  const order = ['thumbnail', 'cover', 'primary', 'slide'];
  for (const role of order) {
    const found = item.media.find((m) => m.role === role && m.kind === 'image');
    if (found) return found;
  }
  return null;
}

export function asList(value: string | string[] | undefined): string[] {
  if (Array.isArray(value)) return value;
  if (!value) return [];
  return [value];
}

export function hasPending(item: ContentItem): boolean {
  return item.placements.some((p) => ['scheduled', 'queued'].includes(p.status));
}

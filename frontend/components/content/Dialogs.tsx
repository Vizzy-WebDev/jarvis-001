'use client';

import { useMemo, useState } from 'react';

import { Button } from '@/components/ui/Button';
import { Field, inputClass } from '@/components/ui/Field';
import { Modal } from '@/components/ui/Modal';
import { api, ApiRequestError } from '@/lib/api';
import type { ContentAccount, ContentItem, ContentMeta, ContentPlacement } from '@/lib/api-types';

import { allZones, browserZone, inZone, utcToZoned, zonedToUtc } from './format';

function message(err: unknown, fallback: string): string {
  return err instanceof ApiRequestError ? err.message : fallback;
}

/** A yes/no in front of anything that cannot be taken back, or that cancels
 *  something already set up. Always nested: it opens over the thing it asks about. */
export function ConfirmDialog({
  title, body, confirm, tone = 'danger', onConfirm, onClose,
}: {
  title: string;
  body: React.ReactNode;
  confirm: string;
  tone?: 'danger' | 'primary';
  onConfirm: () => Promise<void> | void;
  onClose: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  return (
    <Modal open nested title={title} onClose={onClose} footer={
      <>
        <Button tone={tone} data-testid="confirm-yes" disabled={busy} onClick={async () => {
          setBusy(true);
          try {
            await onConfirm();
            onClose();
          } catch (err) {
            setError(message(err, 'That did not work.'));
            setBusy(false);
          }
        }}>{confirm}</Button>
        <Button onClick={onClose} disabled={busy}>Cancel</Button>
      </>
    }>
      <div className="text-[14px] leading-relaxed text-ink">{body}</div>
      {error && <p className="mt-3 text-[13px] text-state-danger">{error}</p>}
    </Modal>
  );
}

/** Asking for changes: what, why, and who does the revision. */
export function RequestChangesDialog({
  item, meta, onDone, onClose,
}: {
  item: ContentItem;
  meta: ContentMeta;
  onDone: () => void;
  onClose: () => void;
}) {
  const textOnly = meta.types[item.contentType]?.render === 'text';
  const [what, setWhat] = useState('');
  const [why, setWhy] = useState('');
  const [assignee, setAssignee] = useState<'agent' | 'jarvis'>(textOnly ? 'jarvis' : 'agent');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const cancels = item.placements.filter((p) => p.status === 'scheduled' || p.status === 'queued').length;

  return (
    <Modal open nested title="Request changes" onClose={onClose} footer={
      <>
        <Button tone="primary" data-testid="send-changes" disabled={busy || !what.trim()} onClick={async () => {
          setBusy(true);
          setError(null);
          try {
            await api.content.requestChanges(item.id, { what, why, assignee });
            onDone();
            onClose();
          } catch (err) {
            setError(message(err, 'Could not send that.'));
            setBusy(false);
          }
        }}>Send back for changes</Button>
        <Button onClick={onClose}>Cancel</Button>
      </>
    }>
      <div className="space-y-4">
        <Field label="What needs to change">
          <textarea data-testid="changes-what" className={`${inputClass} min-h-[84px]`} value={what}
                    onChange={(e) => setWhat(e.target.value)}
                    placeholder="e.g. Make the first 2 seconds a stronger hook" />
        </Field>
        <Field label="Why" hint="Optional — it helps whoever revises it get it right.">
          <textarea data-testid="changes-why" className={`${inputClass} min-h-[64px]`} value={why}
                    onChange={(e) => setWhy(e.target.value)} placeholder="e.g. Viewers drop off at 0:02" />
        </Field>
        <Field label="Who revises it">
          <div className="space-y-2 text-[13px]">
            <label className="flex items-start gap-2">
              <input type="radio" className="mt-1 accent-accent" name="assignee" data-testid="assign-agent"
                     checked={assignee === 'agent'} onChange={() => setAssignee('agent')} />
              <span><span className="text-ink">{item.producer || 'The agent that made it'}</span>
                <span className="block text-ink-faint">It picks the request up and hands back a new version.</span></span>
            </label>
            <label className="flex items-start gap-2">
              <input type="radio" className="mt-1 accent-accent" name="assignee" data-testid="assign-jarvis"
                     checked={assignee === 'jarvis'} onChange={() => setAssignee('jarvis')} />
              <span><span className="text-ink">Jarvis</span>
                <span className="block text-ink-faint">
                  Starts a background job right away. Jarvis can rewrite text — titles, captions, a post&apos;s
                  words — but can&apos;t re-edit video, images or audio.
                </span></span>
            </label>
          </div>
        </Field>
        {cancels > 0 && (
          <p className="rounded border border-state-warn/30 bg-state-warn/[0.06] p-3 text-[13px] text-state-warn">
            This cancels {cancels} scheduled post{cancels === 1 ? '' : 's'}. It goes back to Review once the
            revision is in.
          </p>
        )}
        {error && <p className="text-[13px] text-state-danger">{error}</p>}
      </div>
    </Modal>
  );
}

/** "I posted it myself": recorded as published, with its link. */
export function MarkPostedDialog({
  placement, onDone, onClose,
}: { placement: ContentPlacement; onDone: () => void; onClose: () => void }) {
  const [url, setUrl] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  return (
    <Modal open nested title={`Posted to ${placement.platformLabel} yourself?`} onClose={onClose} footer={
      <>
        <Button tone="primary" data-testid="mark-posted-save" disabled={busy} onClick={async () => {
          setBusy(true);
          try {
            await api.content.markPosted(placement.id, url.trim());
            onDone();
            onClose();
          } catch (err) {
            setError(message(err, 'Could not record that.'));
            setBusy(false);
          }
        }}>Mark as published</Button>
        <Button onClick={onClose}>Cancel</Button>
      </>
    }>
      <Field label="Link to the post" hint="Optional, but it's how you'll find it again from Published.">
        <input data-testid="mark-posted-url" className={inputClass} value={url} placeholder="https://…"
               onChange={(e) => setUrl(e.target.value)} />
      </Field>
      {error && <p className="mt-3 text-[13px] text-state-danger">{error}</p>}
    </Modal>
  );
}

/**
 * Choosing where it goes and when. With no `placement`, it first adds one
 * (platform, account, destination); with `scheduleIt` off it only adds.
 */
export function ScheduleDialog({
  item, meta, placement, initialDate, scheduleIt = true, onDone, onClose, onAccountsChanged,
}: {
  item: ContentItem;
  meta: ContentMeta;
  placement?: ContentPlacement | null;
  initialDate?: string;
  scheduleIt?: boolean;
  onDone: () => void;
  onClose: () => void;
  onAccountsChanged: () => void;
}) {
  const platforms = Object.entries(meta.platforms).filter(([, p]) => p.accepts.includes(item.contentType));
  // Scheduling with no particular post in mind: offer the platforms it already
  // has that aren't scheduled yet, before offering to add another.
  const unscheduled = placement || !scheduleIt ? []
    : item.placements.filter((p) => p.status === 'draft' || p.status === 'failed');
  const [target, setTarget] = useState<string>(placement?.id ?? unscheduled[0]?.id ?? 'new');
  const [platform, setPlatform] = useState(placement?.platform ?? platforms[0]?.[0] ?? '');
  const accounts = meta.accounts.filter((a) => a.platform === platform);
  const [accountId, setAccountId] = useState<string>(placement?.accountId ?? accounts[0]?.id ?? '');
  const [newHandle, setNewHandle] = useState('');
  const [destination, setDestination] = useState(placement?.destination ?? '');
  const zone0 = placement?.timezone || browserZone();
  const start = placement?.scheduledAt ? utcToZoned(placement.scheduledAt, zone0) : null;
  const [zone, setZone] = useState(zone0);
  const [date, setDate] = useState(start?.date ?? initialDate ?? tomorrow());
  const [time, setTime] = useState(start?.time ?? '09:00');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const zones = useMemo(() => allZones(), []);
  const account: ContentAccount | undefined = meta.accounts.find((a) => a.id === accountId);
  const utc = scheduleIt ? zonedToUtc(date, time, zone) : null;
  const rescheduling = placement?.status === 'scheduled';

  async function go(now: boolean) {
    setBusy(true);
    setError(null);
    try {
      let chosenPlacement = placement ?? item.placements.find((p) => p.id === target) ?? null;
      if (!chosenPlacement) {
        let chosen: string | null = accountId === '__new' ? null : accountId || null;
        if (accountId === '__new') {
          if (!newHandle.trim()) throw new ApiRequestError(400, 'Type the account handle, or pick one.');
          const created = await api.content.accounts.create({
            platform, handle: newHandle.trim(), destinations: destination.trim() ? [destination.trim()] : [],
            defaultNiche: item.niche,
          });
          chosen = created.account.id;
          onAccountsChanged();
        }
        chosenPlacement = (await api.content.addPlacement(item.id, { platform, accountId: chosen, destination }))
          .placement;
      }
      if (now) await api.content.postNow(chosenPlacement.id);
      else if (scheduleIt) {
        if (!utc) throw new ApiRequestError(400, 'Pick a date and time.');
        await api.content.schedule(chosenPlacement.id, utc, zone);
      }
      onDone();
      onClose();
    } catch (err) {
      setError(message(err, 'Could not schedule that.'));
      setBusy(false);
    }
  }

  const title = !scheduleIt ? 'Add a platform'
    : placement ? `${rescheduling ? 'Reschedule' : 'Schedule'} — ${placement.platformLabel}` : 'Schedule it';

  return (
    <Modal open nested title={title} onClose={onClose} footer={
      <>
        <Button tone="primary" data-testid="schedule-save" disabled={busy || (scheduleIt && !utc)}
                onClick={() => void go(false)}>
          {!scheduleIt ? 'Add platform' : rescheduling ? 'Reschedule' : 'Schedule'}
        </Button>
        {scheduleIt && (
          <Button data-testid="schedule-post-now" disabled={busy} onClick={() => void go(true)}>Post now instead</Button>
        )}
        <Button onClick={onClose}>Cancel</Button>
      </>
    }>
      <div className="space-y-4">
        {unscheduled.length > 0 && (
          <Field label="What to schedule">
            <select data-testid="schedule-target" className={inputClass} value={target}
                    onChange={(e) => setTarget(e.target.value)}>
              {unscheduled.map((p) => (
                <option key={p.id} value={p.id}>
                  {[p.platformLabel, p.accountLabel, p.destination].filter(Boolean).join(' · ')}
                </option>
              ))}
              <option value="new">Another platform…</option>
            </select>
          </Field>
        )}
        {!placement && target === 'new' && (
          <>
            <Field label="Platform">
              <select data-testid="schedule-platform" className={inputClass} value={platform} onChange={(e) => {
                setPlatform(e.target.value);
                const first = meta.accounts.find((a) => a.platform === e.target.value);
                setAccountId(first?.id ?? '');
              }}>
                {platforms.map(([id, p]) => <option key={id} value={id}>{p.label}</option>)}
              </select>
            </Field>
            <Field label="Account" hint="The account it goes out on. Type the handle exactly as it appears there.">
              <select data-testid="schedule-account" className={inputClass} value={accountId}
                      onChange={(e) => setAccountId(e.target.value)}>
                {accounts.map((a) => <option key={a.id} value={a.id}>{a.handle}</option>)}
                <option value="">No specific account</option>
                <option value="__new">+ Add an account…</option>
              </select>
              {accountId === '__new' && (
                <input data-testid="schedule-new-account" className={`${inputClass} mt-2`} value={newHandle}
                       placeholder="@handle" onChange={(e) => setNewHandle(e.target.value)} />
              )}
            </Field>
            <Field label="Where on it" hint="Optional — e.g. Shorts, a Page, a playlist, a board.">
              <input data-testid="schedule-destination" className={inputClass} value={destination}
                     list="content-destinations" onChange={(e) => setDestination(e.target.value)} />
              <datalist id="content-destinations">
                {(account?.destinations ?? []).map((d) => <option key={d} value={d} />)}
              </datalist>
            </Field>
          </>
        )}
        {scheduleIt && (
          <>
            <div className="grid grid-cols-2 gap-3">
              <Field label="Date">
                <input type="date" data-testid="schedule-date" className={inputClass} value={date}
                       onChange={(e) => setDate(e.target.value)} />
              </Field>
              <Field label="Time">
                <input type="time" data-testid="schedule-time" className={inputClass} value={time}
                       onChange={(e) => setTime(e.target.value)} />
              </Field>
            </div>
            <Field label="Timezone">
              <select data-testid="schedule-zone" className={inputClass} value={zone}
                      onChange={(e) => setZone(e.target.value)}>
                {zones.map((z) => <option key={z} value={z}>{z}</option>)}
              </select>
            </Field>
            {utc && zone !== browserZone() && (
              <p className="text-[12px] text-ink-faint">That&apos;s {inZone(utc, browserZone())} where you are.</p>
            )}
          </>
        )}
        {error && <p data-testid="schedule-error" className="text-[13px] text-state-danger">{error}</p>}
      </div>
    </Modal>
  );
}

/** The accounts content goes out on — a simple list, per platform. */
export function AccountsDialog({
  meta, onChanged, onClose,
}: { meta: ContentMeta; onChanged: () => void; onClose: () => void }) {
  const [platform, setPlatform] = useState(Object.keys(meta.platforms)[0] ?? '');
  const [handle, setHandle] = useState('');
  const [destinations, setDestinations] = useState('');
  const [niche, setNiche] = useState('');
  const [error, setError] = useState<string | null>(null);

  return (
    <Modal open title="Accounts" onClose={onClose}>
      <p className="mb-4 text-[13px] text-ink-muted">
        The accounts your content goes out on. A publisher (an agent, or a posting service you connect later)
        is handed the platform, the handle and where on it — so type the handle exactly as it appears there.
      </p>
      <ul className="mb-5 space-y-2" data-testid="accounts-list">
        {meta.accounts.length === 0 && <li className="text-[13px] text-ink-faint">No accounts yet.</li>}
        {meta.accounts.map((a) => (
          <li key={a.id} className="flex items-center gap-3 rounded border border-surface-border px-3 py-2 text-[13px]">
            <span className="min-w-0 flex-1">
              <span className="text-ink">{a.handle}</span>
              <span className="text-ink-faint"> · {a.platformLabel}</span>
              {a.destinations.length > 0 && <span className="text-ink-faint"> · {a.destinations.join(', ')}</span>}
              {a.defaultNiche && <span className="text-ink-faint"> · {a.defaultNiche}</span>}
            </span>
            <Button tone="danger" onClick={async () => {
              try {
                await api.content.accounts.remove(a.id);
                onChanged();
              } catch (err) {
                setError(message(err, 'Could not remove it.'));
              }
            }}>Remove</Button>
          </li>
        ))}
      </ul>
      <div className="space-y-3 rounded border border-surface-border p-3">
        <div className="grid grid-cols-2 gap-3">
          <Field label="Platform">
            <select data-testid="account-platform" className={inputClass} value={platform}
                    onChange={(e) => setPlatform(e.target.value)}>
              {Object.entries(meta.platforms).map(([id, p]) => <option key={id} value={id}>{p.label}</option>)}
            </select>
          </Field>
          <Field label="Handle">
            <input data-testid="account-handle" className={inputClass} value={handle} placeholder="@handle"
                   onChange={(e) => setHandle(e.target.value)} />
          </Field>
        </div>
        <div className="grid grid-cols-2 gap-3">
          <Field label="Destinations" hint="Comma-separated, optional.">
            <input data-testid="account-destinations" className={inputClass} value={destinations}
                   placeholder="Shorts, Main feed" onChange={(e) => setDestinations(e.target.value)} />
          </Field>
          <Field label="Usual niche" hint="Optional.">
            <input data-testid="account-niche" className={inputClass} value={niche} list="content-niches"
                   onChange={(e) => setNiche(e.target.value)} />
          </Field>
        </div>
        <Button tone="primary" data-testid="account-add" disabled={!handle.trim()} onClick={async () => {
          setError(null);
          try {
            await api.content.accounts.create({
              platform, handle: handle.trim(), defaultNiche: niche.trim(),
              destinations: destinations.split(',').map((d) => d.trim()).filter(Boolean),
            });
            setHandle('');
            setDestinations('');
            setNiche('');
            onChanged();
          } catch (err) {
            setError(message(err, 'Could not add it.'));
          }
        }}>Add account</Button>
        {error && <p className="text-[13px] text-state-danger">{error}</p>}
      </div>
      <datalist id="content-niches">{meta.niches.map((n) => <option key={n} value={n} />)}</datalist>
    </Modal>
  );
}

function tomorrow(): string {
  const d = new Date(Date.now() + 24 * 3600 * 1000);
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

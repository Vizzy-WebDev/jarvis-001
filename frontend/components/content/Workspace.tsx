'use client';

import { useCallback, useEffect, useState } from 'react';

import { Button } from '@/components/ui/Button';
import { Field, inputClass } from '@/components/ui/Field';
import { Modal } from '@/components/ui/Modal';
import { api, ApiRequestError } from '@/lib/api';
import type { ContentItemDetail, ContentMeta, ContentPlacement } from '@/lib/api-types';

import { ContentPreview } from './ContentPreview';
import { ConfirmDialog, MarkPostedDialog, RequestChangesDialog, ScheduleDialog } from './Dialogs';
import {
  actorLabel, asList, FLOW, inZone, PLACEMENT_LABEL, PLACEMENT_TONE, STAGE_LABEL, when,
} from './format';

type Confirm = { title: string; body: React.ReactNode; confirm: string; tone?: 'danger' | 'primary';
                 run: () => Promise<unknown> };
type Scheduling = { placement: ContentPlacement | null; scheduleIt: boolean; date?: string };

const EDITABLE = ['review', 'approved'];

/**
 * One content item, whole: the actual content first, its supporting
 * information and assets beside it, where it is in the lifecycle, what happens
 * next and who does it — and only the actions that make sense right now.
 */
export function Workspace({
  itemId, meta, refreshKey, initialSchedule, onClose, onChanged, onAccountsChanged, onNavigate,
}: {
  itemId: string;
  meta: ContentMeta;
  /** Bumped by the screen whenever this item changes anywhere. */
  refreshKey: number;
  /** Open straight into scheduling it for this date (from the calendar). */
  initialSchedule?: string;
  onClose: () => void;
  onChanged: () => void;
  onAccountsChanged: () => void;
  onNavigate: (section: string) => void;
}) {
  const [item, setItem] = useState<ContentItemDetail | null>(null);
  const [gone, setGone] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [name, setName] = useState('');
  const [niche, setNiche] = useState('');
  const [viewing, setViewing] = useState<number | null>(null);
  const [asking, setAsking] = useState(false);
  const [scheduling, setScheduling] = useState<Scheduling | null>(
    initialSchedule ? { placement: null, scheduleIt: true, date: initialSchedule } : null);
  const [posting, setPosting] = useState<ContentPlacement | null>(null);
  const [confirm, setConfirm] = useState<Confirm | null>(null);
  const [versionOf, setVersionOf] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const { item: fresh } = await api.content.get(itemId);
      setItem(fresh);
      setName(fresh.name);
      setNiche(fresh.niche);
      setDraft(toDraft(fresh.fields, meta));
    } catch (err) {
      if (err instanceof ApiRequestError && err.status === 404) setGone(true);
      else setError(err instanceof ApiRequestError ? err.message : 'Could not open it.');
    }
  }, [itemId, meta]);

  useEffect(() => {
    void load();
  }, [load, refreshKey]);

  async function run(action: () => Promise<unknown>, after?: () => void) {
    setBusy(true);
    setError(null);
    try {
      await action();
      onChanged();
      await load();
      after?.();
    } catch (err) {
      setError(err instanceof ApiRequestError ? err.message : 'That did not work.');
    } finally {
      setBusy(false);
    }
  }

  if (gone) {
    return (
      <Modal open title="Gone" onClose={onClose}>
        <p className="text-[14px] text-ink-muted">That item was deleted for good.</p>
      </Modal>
    );
  }
  if (!item) {
    return (
      <Modal open title="Opening…" onClose={onClose}>
        {error ? <p className="text-[13px] text-state-danger">{error}</p>
          : <p className="text-[13px] text-ink-faint">Reading…</p>}
      </Modal>
    );
  }

  const info = meta.types[item.contentType];
  const deleted = Boolean(item.deletedAt);
  const editable = !deleted && EDITABLE.includes(item.stage) && viewing === null;
  const revision = viewing !== null ? item.revisions.find((r) => r.revision === viewing) : null;
  const shownFields = revision ? revision.fields : item.fields;
  const shownMedia = revision ? revision.media : item.media;
  const pending = item.placements.filter((p) => p.status === 'scheduled' || p.status === 'queued').length;
  const approvedish = ['approved', 'scheduling', 'published'].includes(item.stage) && !deleted;
  const dirty = name !== item.name || niche !== item.niche
    || JSON.stringify(draft) !== JSON.stringify(toDraft(item.fields, meta));
  const assets = shownMedia.filter((m) => !['primary', 'slide'].includes(m.role));

  const cancelsNote = pending
    ? <> It cancels {pending} scheduled post{pending === 1 ? '' : 's'} first — nothing goes out while it&apos;s there.</>
    : null;

  function archiveIt() {
    const action = () => api.content.archive(item!.id);
    if (!pending) return void run(action);
    setConfirm({ title: 'Archive it?', confirm: 'Archive', tone: 'primary', run: action,
                 body: <>It stays searchable in Archived.{cancelsNote}</> });
  }

  function deleteIt() {
    const action = () => api.content.remove(item!.id);
    if (!pending) return void run(action, onClose);
    setConfirm({ title: 'Move it to the Recycle Bin?', confirm: 'Move to Recycle Bin', run: action,
                 body: <>You can restore it from the Recycle Bin.{cancelsNote}</> });
  }

  const footer = (
    <>
      {deleted && (
        <>
          <Button tone="primary" data-testid="ws-restore" disabled={busy}
                  onClick={() => void run(() => api.content.restore(item.id))}>Restore</Button>
          <Button tone="danger" data-testid="ws-purge" disabled={busy} className="ml-auto" onClick={() => setConfirm({
            title: 'Delete forever?', confirm: 'Delete forever',
            body: <>“{item.name}” and every file, version and note that belongs to it will be permanently
              deleted. This can&apos;t be undone.</>,
            run: async () => {
              await api.content.purge(item.id);
              onChanged();
              onClose();
            },
          })}>Delete forever</Button>
        </>
      )}
      {!deleted && item.stage === 'review' && viewing === null && (
        <>
          <Button tone="primary" data-testid="ws-approve" disabled={busy || dirty}
                  title={dirty ? 'Save your edits first' : undefined}
                  onClick={() => void run(() => api.content.approve(item.id))}>Approve</Button>
          <Button data-testid="ws-request-changes" disabled={busy} onClick={() => setAsking(true)}>
            Request changes</Button>
        </>
      )}
      {!deleted && item.stage === 'approved' && (
        <>
          <Button tone="primary" data-testid="ws-schedule" disabled={busy}
                  onClick={() => setScheduling({ placement: null, scheduleIt: true })}>Schedule…</Button>
          <Button data-testid="ws-request-changes" disabled={busy} onClick={() => setAsking(true)}>
            Request changes</Button>
        </>
      )}
      {!deleted && (item.stage === 'scheduling' || item.stage === 'published') && (
        <Button data-testid="ws-schedule" disabled={busy}
                onClick={() => setScheduling({ placement: null, scheduleIt: true })}>
          {item.stage === 'published' ? 'Post to another platform…' : 'Schedule another platform…'}
        </Button>
      )}
      {!deleted && item.stage === 'archived' && (
        <Button tone="primary" data-testid="ws-unarchive" disabled={busy}
                onClick={() => void run(() => api.content.unarchive(item.id))}>Unarchive</Button>
      )}
      {!deleted && item.stage !== 'archived' && (
        <Button data-testid="ws-archive" disabled={busy} className="ml-auto" onClick={archiveIt}>Archive</Button>
      )}
      {!deleted && (
        <Button tone="danger" data-testid="ws-delete" disabled={busy}
                className={item.stage === 'archived' ? 'ml-auto' : ''} onClick={deleteIt}>Delete</Button>
      )}
    </>
  );

  return (
    <Modal open size="wide" title={item.name} onClose={onClose} footer={footer}>
      <div data-testid="workspace" data-stage={deleted ? 'bin' : item.stage}>
        <Progress item={item} />

        {error && <p data-testid="ws-error" className="mb-3 text-[13px] text-state-danger">{error}</p>}
        {revision && (
          <div className="mb-3 flex items-center gap-3 rounded border border-accent/30 bg-accent/[0.06] px-3 py-2
                          text-[13px] text-accent" data-testid="viewing-revision">
            Viewing revision {revision.revision} — the current one is {item.revision}.
            <button type="button" className="ml-auto underline" onClick={() => setViewing(null)}>Back to current</button>
          </div>
        )}

        <div className="grid grid-cols-1 gap-6 lg:grid-cols-[minmax(0,1.15fr)_minmax(0,1fr)]">
          {/* The content itself, first. */}
          <section>
            <SectionTitle>{info?.label ?? item.typeLabel}</SectionTitle>
            <ContentPreview item={item} info={info} fields={shownFields} media={shownMedia} />
            {assets.length > 0 && (
              <div className="mt-4">
                <SectionTitle>Supporting assets</SectionTitle>
                <ul className="flex flex-wrap gap-3" data-testid="ws-assets">
                  {assets.map((a) => (
                    <li key={a.fileId} className="w-36">
                      {a.kind === 'image' ? (
                        // eslint-disable-next-line @next/next/no-img-element
                        <img src={a.url} alt={a.role} className="h-20 w-36 rounded border border-surface-border
                                                                 object-cover" data-testid={`asset-${a.role}`} />
                      ) : (
                        <div className="flex h-20 w-36 items-center justify-center rounded border
                                        border-surface-border text-[11px] text-ink-faint">{a.name}</div>
                      )}
                      <span className="mt-1 block text-[11px] text-ink-faint">
                        {meta.assets[a.role] ?? a.role} · <a className="underline" href={a.url} download>Download</a>
                      </span>
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </section>

          <section className="space-y-5">
            {item.openRequest && !deleted && (
              <RequestPanel item={item} busy={busy} run={run} onNavigate={onNavigate} />
            )}

            {item.findings.length > 0 && (
              <div>
                <SectionTitle>AI review findings</SectionTitle>
                <ul className="space-y-1.5" data-testid="ws-findings">
                  {item.findings.map((f, i) => (
                    <li key={i} className={`text-[13px] ${f.level === 'problem' ? 'text-state-danger'
                      : f.level === 'warning' ? 'text-state-warn' : 'text-ink-muted'}`}>• {f.text}</li>
                  ))}
                </ul>
              </div>
            )}

            <div>
              <SectionTitle>Supporting information</SectionTitle>
              <div className="grid grid-cols-2 gap-x-3">
                <Field label="Name (only you see this)">
                  <input data-testid="ws-name" className={inputClass} value={name} disabled={deleted}
                         onChange={(e) => setName(e.target.value)} />
                </Field>
                <Field label="Niche">
                  <input data-testid="ws-niche" className={inputClass} value={niche} disabled={deleted}
                         list="ws-niches" onChange={(e) => setNiche(e.target.value)} />
                  <datalist id="ws-niches">{meta.niches.map((n) => <option key={n} value={n} />)}</datalist>
                </Field>
              </div>
              {(info?.fields ?? []).map((field) => {
                const spec = meta.fields[field];
                const value = viewing !== null ? listText(field, shownFields[field]) : draft[field] ?? '';
                const common = {
                  'data-testid': `field-${field}`, className: inputClass, value, disabled: !editable,
                  onChange: (e: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement>) =>
                    setDraft((d) => ({ ...d, [field]: e.target.value })),
                };
                return (
                  <Field key={field} label={spec?.label ?? field}
                         hint={spec?.kind === 'list' ? (field === 'hashtags' ? 'Separated by spaces.' : 'Separated by commas.') : undefined}>
                    {spec?.kind === 'longtext'
                      ? <textarea {...common} className={`${inputClass} ${field === 'body' ? 'min-h-[140px]' : 'min-h-[72px]'}`} />
                      : <input {...common} />}
                  </Field>
                );
              })}
              {!editable && !deleted && viewing === null && (
                <p className="text-[12px] text-ink-faint">
                  {item.stage === 'changes_requested' ? 'Waiting on the revision — edit it once it is back in Review.'
                    : 'The content is locked once it is scheduled or published.'}
                </p>
              )}
              {dirty && !deleted && (
                <div className="mt-2 flex gap-2">
                  <Button tone="primary" data-testid="ws-save" disabled={busy} onClick={() => void run(() =>
                    api.content.edit(item.id, {
                      ...(name !== item.name ? { name } : {}),
                      ...(niche !== item.niche ? { niche } : {}),
                      ...(editable ? { fields: fromDraft(draft, item.fields, meta, info?.fields ?? []) } : {}),
                    }))}>Save changes</Button>
                  <Button onClick={() => {
                    setName(item.name);
                    setNiche(item.niche);
                    setDraft(toDraft(item.fields, meta));
                  }}>Discard</Button>
                </div>
              )}
            </div>

            <div>
              <SectionTitle>Where it goes</SectionTitle>
              {item.placements.length === 0 && (
                <p className="mb-2 text-[13px] text-ink-faint">No platforms yet.</p>
              )}
              <ul className="space-y-2" data-testid="ws-placements">
                {item.placements.map((p) => (
                  <PlacementRow key={p.id} p={p} item={item} meta={meta} busy={busy} approvedish={approvedish}
                    open={versionOf === p.id} onToggle={() => setVersionOf(versionOf === p.id ? null : p.id)}
                    run={run}
                    onSchedule={() => setScheduling({ placement: p, scheduleIt: true })}
                    onMarkPosted={() => setPosting(p)} />
                ))}
              </ul>
              {!deleted && !['archived', 'changes_requested'].includes(item.stage) && (
                <Button className="mt-2" data-testid="ws-add-platform" disabled={busy}
                        onClick={() => setScheduling({ placement: null, scheduleIt: false })}>+ Add a platform</Button>
              )}
            </div>
          </section>
        </div>

        <div className="mt-6 grid grid-cols-1 gap-6 lg:grid-cols-2">
          <div>
            <SectionTitle>Revisions</SectionTitle>
            <ul className="space-y-1.5" data-testid="ws-revisions">
              {item.revisions.map((r) => (
                <li key={r.revision} className="flex items-baseline gap-2 text-[13px]">
                  <span className="text-ink">Revision {r.revision}</span>
                  <span className="min-w-0 flex-1 truncate text-ink-faint">
                    {r.by ? `${actorLabel(r.by)} · ` : ''}{r.note ? `${r.note} · ` : ''}{when(r.createdAt)}
                  </span>
                  {r.revision === item.revision && viewing === null ? (
                    <span className="text-[11px] text-accent">current</span>
                  ) : (
                    <button type="button" className="text-[12px] text-accent underline"
                            data-testid={`view-revision-${r.revision}`}
                            onClick={() => setViewing(r.revision === item.revision ? null : r.revision)}>
                      {r.revision === item.revision ? 'Back to current' : 'View'}
                    </button>
                  )}
                </li>
              ))}
            </ul>
            {item.requests.filter((r) => r.status !== 'open' && r.status !== 'in_progress').length > 0 && (
              <>
                <SectionTitle className="mt-4">Earlier change requests</SectionTitle>
                <ul className="space-y-1.5">
                  {item.requests.filter((r) => r.status === 'resolved' || r.status === 'cancelled').map((r) => (
                    <li key={r.id} className="text-[13px] text-ink-muted">
                      “{r.what}” — {r.status === 'resolved' ? `answered in revision ${r.resolvedRevision}` : 'withdrawn'}
                    </li>
                  ))}
                </ul>
              </>
            )}
          </div>
          <div>
            <SectionTitle>History</SectionTitle>
            <ol className="space-y-1.5" data-testid="ws-history">
              {item.events.map((e, i) => (
                <li key={i} className="text-[13px]">
                  <span className="text-ink-faint">{when(e.at)} · </span>
                  <span className="text-ink">{actorLabel(e.actor)}</span>
                  <span className="text-ink-muted"> — {e.note || e.kind}</span>
                </li>
              ))}
            </ol>
          </div>
        </div>
      </div>

      {asking && (
        <RequestChangesDialog item={item} meta={meta} onClose={() => setAsking(false)} onDone={() => {
          onChanged();
          void load();
        }} />
      )}
      {scheduling && (
        <ScheduleDialog item={item} meta={meta} placement={scheduling.placement} scheduleIt={scheduling.scheduleIt}
                        initialDate={scheduling.date} onAccountsChanged={onAccountsChanged}
                        onClose={() => setScheduling(null)} onDone={() => {
                          onChanged();
                          void load();
                        }} />
      )}
      {posting && (
        <MarkPostedDialog placement={posting} onClose={() => setPosting(null)} onDone={() => {
          onChanged();
          void load();
        }} />
      )}
      {confirm && (
        <ConfirmDialog title={confirm.title} body={confirm.body} confirm={confirm.confirm} tone={confirm.tone}
                       onClose={() => setConfirm(null)} onConfirm={async () => {
                         await confirm.run();
                         onChanged();
                         await load().catch(() => undefined);
                       }} />
      )}
    </Modal>
  );
}

function SectionTitle({ children, className = '' }: { children: React.ReactNode; className?: string }) {
  return (
    <h3 className={`mb-2 text-[11px] font-semibold uppercase tracking-[0.16em] text-ink-faint ${className}`}>
      {children}
    </h3>
  );
}

/** Where this item is in the lifecycle, what happens next, and who does it. */
function Progress({ item }: { item: ContentItemDetail }) {
  const deleted = Boolean(item.deletedAt);
  const current = item.stage === 'archived' ? item.archivedFrom : item.stage;
  const index = FLOW.findIndex((s) => s.id === current);
  return (
    <div className="mb-5">
      <ol className="flex flex-wrap items-center gap-1.5 text-[12px]" data-testid="ws-progress">
        {FLOW.map((stage, i) => (
          <li key={stage.id} className="flex items-center gap-1.5">
            <span className={`rounded-pill px-2.5 py-1 ${i === index && !deleted && item.stage !== 'archived'
              ? 'bg-accent/15 text-accent' : i < index ? 'text-ink-muted' : 'text-ink-faint'}`}
                  data-current={i === index ? 'true' : undefined}>
              {i < index ? '✓ ' : ''}{stage.label}
            </span>
            {i < FLOW.length - 1 && <span className="text-ink-faint">→</span>}
          </li>
        ))}
        {(item.stage === 'archived' || deleted) && (
          <li className="ml-2 rounded-pill bg-white/[0.06] px-2.5 py-1 text-ink-muted">
            {deleted ? 'In the Recycle Bin' : 'Archived'}
          </li>
        )}
      </ol>
      <p className="mt-2 text-[13px]" data-testid="ws-next">
        <span className="text-ink-faint">Next: </span><span className="text-ink">{item.next.step}</span>
        {item.next.who && item.next.who !== '—' && (
          <><span className="text-ink-faint"> · </span><span className="text-accent">{item.next.who}</span></>
        )}
      </p>
      {item.attention.length > 0 && (
        <p className="mt-1 text-[12px] text-state-warn">{item.attention.join(' · ')}</p>
      )}
      <p className="mt-1 text-[12px] text-ink-faint">
        {item.typeLabel}{item.niche ? ` · ${item.niche}` : ''}{item.producer ? ` · made by ${item.producer}` : ''}
        {' · '}revision {item.revision} · {deleted ? `deleted ${when(item.deletedAt)}` : STAGE_LABEL[item.stage]}
      </p>
    </div>
  );
}

function RequestPanel({
  item, busy, run, onNavigate,
}: {
  item: ContentItemDetail;
  busy: boolean;
  run: (action: () => Promise<unknown>) => Promise<void>;
  onNavigate: (section: string) => void;
}) {
  const request = item.openRequest!;
  const [editing, setEditing] = useState(false);
  const [what, setWhat] = useState(request.what);
  const [why, setWhy] = useState(request.why);
  const job = request.job;
  const jobEnded = job && ['done', 'cancelled', 'orphaned', 'stalled'].includes(job.status);

  let status: React.ReactNode;
  if (request.startError) {
    status = <span className="text-state-warn">{request.startError}</span>;
  } else if (request.assignee === 'jarvis' && job) {
    status = jobEnded
      ? <span className="text-state-warn">Jarvis&apos;s job ended ({job.status}) without handing back a revision
          {job.error ? `: ${job.error}` : ''}.</span>
      : <span>Jarvis is working on it{job.currentStep ? ` — ${job.currentStep}` : ''}.</span>;
  } else if (request.status === 'in_progress') {
    status = <span>{request.pickedUpBy} is working on it, since {when(request.pickedUpAt)}.</span>;
  } else {
    status = <span>Waiting for {request.assignee === 'jarvis' ? 'Jarvis' : item.producer || 'the agent that made it'} to
      pick it up.</span>;
  }

  return (
    <div className="rounded border border-state-warn/30 bg-state-warn/[0.05] p-3" data-testid="ws-request">
      <SectionTitle>Changes requested · on revision {request.revision}</SectionTitle>
      {editing ? (
        <>
          <textarea className={`${inputClass} mb-2 min-h-[64px]`} value={what} onChange={(e) => setWhat(e.target.value)} />
          <textarea className={`${inputClass} min-h-[48px]`} value={why} placeholder="Why"
                    onChange={(e) => setWhy(e.target.value)} />
          <div className="mt-2 flex gap-2">
            <Button tone="primary" disabled={busy || !what.trim()} onClick={() => void run(async () => {
              await api.content.updateRequest(request.id, { what, why });
              setEditing(false);
            })}>Save</Button>
            <Button onClick={() => setEditing(false)}>Cancel</Button>
          </div>
        </>
      ) : (
        <>
          <p className="text-[14px] text-ink" data-testid="ws-request-what">{request.what}</p>
          {request.why && <p className="mt-1 text-[13px] text-ink-muted">Why: {request.why}</p>}
        </>
      )}
      <p className="mt-2 text-[13px] text-ink-muted" data-testid="ws-request-status">{status}</p>
      {!editing && (
        <div className="mt-3 flex flex-wrap gap-2">
          {(request.startError || jobEnded) && (
            <Button tone="primary" data-testid="ws-retry-jarvis" disabled={busy}
                    onClick={() => void run(() => api.content.retryJarvis(request.id))}>Try again</Button>
          )}
          {request.jobId && <Button onClick={() => onNavigate('jobs')}>Open the job</Button>}
          <Button disabled={busy} data-testid="ws-reassign" onClick={() => void run(() => api.content.updateRequest(
            request.id, { assignee: request.assignee === 'jarvis' ? 'agent' : 'jarvis' }))}>
            {request.assignee === 'jarvis' ? 'Send to the agent instead' : 'Have Jarvis do it'}
          </Button>
          <Button disabled={busy} onClick={() => setEditing(true)}>Edit request</Button>
          <Button disabled={busy} data-testid="ws-withdraw"
                  onClick={() => void run(() => api.content.cancelRequest(request.id))}>Withdraw</Button>
        </div>
      )}
    </div>
  );
}

function PlacementRow({
  p, item, meta, busy, approvedish, open, onToggle, run, onSchedule, onMarkPosted,
}: {
  p: ContentPlacement;
  item: ContentItemDetail;
  meta: ContentMeta;
  busy: boolean;
  approvedish: boolean;
  open: boolean;
  onToggle: () => void;
  run: (action: () => Promise<unknown>) => Promise<void>;
  onSchedule: () => void;
  onMarkPosted: () => void;
}) {
  const typeFields = meta.types[item.contentType]?.fields ?? [];
  const fields = typeFields.filter((f) => (meta.platforms[p.platform]?.fields ?? []).includes(f));
  const [over, setOver] = useState<Record<string, string>>(() => toDraft(p.overrides, meta));
  const locked = ['queued', 'publishing', 'published'].includes(p.status) || Boolean(item.deletedAt);
  const safeUrl = p.publishedUrl && /^https?:\/\//i.test(p.publishedUrl) ? p.publishedUrl : null;
  const status = p.due ? 'Due — waiting for publisher' : p.stale ? `Gone quiet (claimed by ${p.claimedBy})`
    : p.status === 'publishing' ? `Being posted by ${p.claimedBy}` : PLACEMENT_LABEL[p.status];

  return (
    <li className="rounded border border-surface-border p-3" data-testid="ws-placement" data-platform={p.platform}
        data-status={p.status}>
      <div className="flex flex-wrap items-baseline gap-x-2 text-[13px]">
        <span className="font-medium text-ink">{p.platformLabel}</span>
        {p.accountLabel && <span className="text-ink-muted">{p.accountLabel}</span>}
        {p.destination && <span className="text-ink-faint">· {p.destination}</span>}
        <span className={`ml-auto ${p.due || p.stale ? 'text-state-warn' : PLACEMENT_TONE[p.status]}`}
              data-testid="placement-status">{status}</span>
      </div>
      {(p.status === 'scheduled' || p.status === 'queued') && p.scheduledAt && (
        <p className="mt-1 text-[12px] text-ink-muted" data-testid="placement-time">
          {inZone(p.scheduledAt, p.timezone)}{p.timezone ? ` (${p.timezone})` : ''}
        </p>
      )}
      {p.status === 'published' && (
        <p className="mt-1 text-[12px] text-ink-muted">
          Published {when(p.publishedAt)}
          {safeUrl && <> · <a className="text-accent underline" href={safeUrl} target="_blank"
                              rel="noopener noreferrer" data-testid="placement-link">{safeUrl}</a></>}
        </p>
      )}
      {p.status === 'failed' && p.failure && (
        <p className="mt-1 text-[12px] text-state-danger" data-testid="placement-failure">{p.failure}</p>
      )}

      <div className="mt-2 flex flex-wrap gap-2">
        {approvedish && ['draft', 'failed'].includes(p.status) && (
          <>
            <Button data-testid="pl-schedule" disabled={busy} onClick={onSchedule}>Schedule…</Button>
            <Button data-testid="pl-post-now" disabled={busy}
                    onClick={() => void run(() => api.content.postNow(p.id))}>Post now</Button>
          </>
        )}
        {approvedish && p.status === 'failed' && (
          <Button data-testid="pl-retry" disabled={busy}
                  onClick={() => void run(() => api.content.requeue(p.id))}>Retry</Button>
        )}
        {p.status === 'scheduled' && !item.deletedAt && (
          <>
            <Button data-testid="pl-reschedule" disabled={busy} onClick={onSchedule}>Reschedule…</Button>
            <Button data-testid="pl-post-now" disabled={busy}
                    onClick={() => void run(() => api.content.postNow(p.id))}>Post now</Button>
          </>
        )}
        {(p.status === 'scheduled' || p.status === 'queued') && !item.deletedAt && (
          <Button data-testid="pl-cancel" disabled={busy}
                  onClick={() => void run(() => api.content.unschedule(p.id))}>Cancel schedule</Button>
        )}
        {p.stale && (
          <Button data-testid="pl-requeue" disabled={busy}
                  onClick={() => void run(() => api.content.requeue(p.id))}>Put back in queue</Button>
        )}
        {approvedish && p.status !== 'published' && (
          <Button data-testid="pl-mark-posted" disabled={busy} onClick={onMarkPosted}>I posted it myself</Button>
        )}
        {fields.length > 0 && (
          <Button data-testid="pl-version" onClick={onToggle}>{open ? 'Hide' : 'Show'} {p.platformLabel} version</Button>
        )}
        {['draft', 'failed'].includes(p.status) && !item.deletedAt && item.stage !== 'archived' && (
          <Button tone="danger" data-testid="pl-remove" disabled={busy}
                  onClick={() => void run(() => api.content.removePlacement(p.id))}>Remove</Button>
        )}
      </div>

      {open && (
        <div className="mt-3 border-t border-surface-border pt-2" data-testid="pl-version-editor">
          <p className="text-[12px] text-ink-faint">
            What {p.platformLabel} gets. Leave a field empty to use the main version (shown faintly).
          </p>
          {fields.map((f) => (
            <Field key={f} label={meta.fields[f]?.label ?? f}>
              {meta.fields[f]?.kind === 'longtext' ? (
                <textarea data-testid={`version-${f}`} className={`${inputClass} min-h-[64px]`} disabled={locked}
                          value={over[f] ?? ''} placeholder={listText(f, item.fields[f])}
                          onChange={(e) => setOver((o) => ({ ...o, [f]: e.target.value }))} />
              ) : (
                <input data-testid={`version-${f}`} className={inputClass} disabled={locked} value={over[f] ?? ''}
                       placeholder={listText(f, item.fields[f])}
                       onChange={(e) => setOver((o) => ({ ...o, [f]: e.target.value }))} />
              )}
            </Field>
          ))}
          {!locked && (
            <Button tone="primary" data-testid="version-save" disabled={busy} onClick={() => void run(() =>
              api.content.updatePlacement(p.id, { overrides: fromDraft(over, {}, meta, fields, true) }))}>
              Save {p.platformLabel} version
            </Button>
          )}
        </div>
      )}
    </li>
  );
}

function listText(field: string, value: string | string[] | undefined): string {
  if (Array.isArray(value)) return value.join(field === 'hashtags' ? ' ' : ', ');
  return value ?? '';
}

function toDraft(fields: Record<string, string | string[]>, meta: ContentMeta): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [key, value] of Object.entries(fields)) {
    out[key] = meta.fields[key]?.kind === 'list' ? listText(key, asList(value)) : String(value ?? '');
  }
  return out;
}

/** Only fields that changed (or, for a platform version, every field — blank clears it). */
function fromDraft(draft: Record<string, string>, base: Record<string, string | string[]>, meta: ContentMeta,
                   allowed: string[], everything = false): Record<string, string | string[]> {
  const out: Record<string, string | string[]> = {};
  for (const field of allowed) {
    const text = draft[field] ?? '';
    const before = listText(field, base[field]);
    if (!everything && text === before) continue;
    if (meta.fields[field]?.kind === 'list') {
      out[field] = field === 'hashtags'
        ? text.split(/[\s,]+/).map((t) => t.trim()).filter(Boolean)
        : text.split(',').map((t) => t.trim()).filter(Boolean);
    } else {
      out[field] = text;
    }
  }
  return out;
}

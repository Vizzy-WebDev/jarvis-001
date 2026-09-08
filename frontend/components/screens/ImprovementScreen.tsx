'use client';

import { useCallback, useEffect, useState } from 'react';

import { Button } from '@/components/ui/Button';
import { Card } from '@/components/ui/Card';
import { EmptyState } from '@/components/ui/EmptyState';
import { Field, inputClass } from '@/components/ui/Field';
import { Modal } from '@/components/ui/Modal';
import { Toggle } from '@/components/ui/Toggle';
import { api, ApiRequestError } from '@/lib/api';
import type {
  Change, ImprovementStatus, Lesson, Outcome, Prefs, Proposal, Rule,
} from '@/lib/api-types';

/**
 * What Jarvis has learned about its OWN work — distinct from Memory, which is
 * what it has learned about the person.
 *
 * **Four tabs, because these are four different questions.** What is being
 * suggested; what has actually changed and can be taken back; what it has
 * concluded; and how freely it may act on any of it. Folding them into one list
 * would put a pending suggestion next to a change made a week ago, which reads
 * as the same kind of thing and is not.
 *
 * **A single failure never becomes a rule.** A proposal only exists once at
 * least two lessons backed by two distinct outcomes agree, which is what makes
 * this pattern-finding rather than patching one bad day. Nothing on this screen
 * can shortcut that.
 *
 * **Jarvis never edits its own code.** For an idea that needs real work, the
 * approval action is generating a brief for whichever coding assistant you name
 * — there is no button here that would write code, because there is no code path
 * behind one.
 */

type Tab = 'suggestions' | 'changes' | 'learned' | 'settings';

const TABS: [Tab, string][] = [
  ['suggestions', 'Suggestions'],
  ['changes', 'Changes'],
  ['learned', 'Learned'],
  ['settings', 'Settings'],
];

/** Which kinds can actually be applied. The policy decides this too — this is
 *  only about what the button should say. */
const APPLIABLE = new Set(['rule', 'setting']);

const KIND_LABEL: Record<string, string> = {
  rule: 'A rule for itself',
  setting: 'A setting change',
  skill: 'A new skill',
  code: 'Something that needs code',
  idea: 'An idea',
};

export function ImprovementScreen() {
  const [tab, setTab] = useState<Tab>('suggestions');
  const [status, setStatus] = useState<ImprovementStatus | null>(null);

  const loadStatus = useCallback(async () => {
    setStatus(await api.improvement.status().catch(() => null));
  }, []);

  useEffect(() => {
    void loadStatus();
  }, [loadStatus]);

  return (
    <>

      <div className="mb-5 inline-flex rounded-pill border border-surface-border p-0.5"
           data-testid="improvement-tabs">
        {TABS.map(([id, label]) => (
          <button
            key={id}
            type="button"
            data-testid={`tab-${id}`}
            aria-pressed={tab === id}
            onClick={() => setTab(id)}
            className={[
              'rounded-pill px-3.5 py-1.5 text-[13px] transition duration-150 ease-out',
              tab === id ? 'bg-accent/15 text-accent' : 'text-ink-muted hover:text-ink',
            ].join(' ')}
          >
            {label}
            {id === 'suggestions' && status && status.pendingProposals > 0 && (
              <span className="ml-1.5 text-[11px] text-accent">{status.pendingProposals}</span>
            )}
          </button>
        ))}
      </div>

      {tab === 'suggestions' && <Suggestions onChanged={loadStatus} />}
      {tab === 'changes' && <Changes />}
      {tab === 'learned' && <Learned />}
      {tab === 'settings' && <Settings status={status} onChanged={loadStatus} />}
    </>
  );
}

// --- suggestions -----------------------------------------------------------------

function Suggestions({ onChanged }: { onChanged: () => Promise<void> }) {
  const [rows, setRows] = useState<Proposal[] | null>(null);
  const [rejected, setRejected] = useState(false);
  const [open, setOpen] = useState<Proposal | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setRows((await api.improvement.proposals(rejected ? 'rejected' : 'pending')).proposals);
      setError(null);
    } catch (err) {
      setError(err instanceof ApiRequestError ? err.message : 'Could not read the suggestions.');
      setRows([]);
    }
  }, [rejected]);

  useEffect(() => {
    void load();
  }, [load]);

  async function refresh() {
    setOpen(null);
    await load();
    await onChanged();
  }

  return (
    <>
      <div className="mb-4 flex justify-end">
        <Button data-testid="toggle-rejected" onClick={() => setRejected((on) => !on)}>
          {rejected ? 'Back to pending' : 'Show rejected'}
        </Button>
      </div>

      {error && <p className="mb-4 text-[13px] text-state-danger">{error}</p>}

      {rows === null ? null : rows.length === 0 ? (
        <EmptyState
          title={rejected ? 'Nothing rejected' : 'Nothing to suggest yet'}
          body={rejected
            ? 'Suggestions you turn down end up here, and can be brought back.'
            : 'Jarvis proposes something only once at least two separate pieces of evidence agree. One bad day never becomes a rule.'}
        />
      ) : (
        <div className="space-y-2" data-testid="proposal-list">
          {rows.map((proposal) => (
            <Card key={proposal.id} interactive data-testid="proposal-row"
                  onClick={() => setOpen(proposal)}>
              <p className="text-[14px] text-ink">{proposal.title}</p>
              <p className="mt-1 text-[12px] text-ink-faint">
                {KIND_LABEL[proposal.kind] ?? proposal.kind}
                {proposal.sourceTier > 1 && ' · from outside reading'}
              </p>
            </Card>
          ))}
        </div>
      )}

      {open && (
        <ProposalDetail
          proposal={open}
          rejected={rejected}
          onClose={() => setOpen(null)}
          onChanged={refresh}
        />
      )}
    </>
  );
}

function ProposalDetail({ proposal, rejected, onClose, onChanged }: {
  proposal: Proposal;
  rejected: boolean;
  onClose: () => void;
  onChanged: () => Promise<void>;
}) {
  const [evidence, setEvidence] = useState<Outcome[]>([]);
  const [brief, setBrief] = useState<string | null>(proposal.implementationPrompt ?? null);
  const [target, setTarget] = useState(proposal.implementationTarget ?? '');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const appliable = APPLIABLE.has(proposal.kind);

  useEffect(() => {
    if (!proposal.evidence?.length) return;
    api.improvement.lookupOutcomes(proposal.evidence)
      .then((found) => setEvidence(found.outcomes))
      .catch(() => setEvidence([]));
  }, [proposal.evidence]);

  async function run(action: () => Promise<unknown>, thenClose = true) {
    setBusy(true);
    setError(null);
    try {
      await action();
      if (thenClose) await onChanged();
    } catch (err) {
      setError(err instanceof ApiRequestError ? err.message : 'That did not work.');
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal
      open
      title={proposal.title}
      onClose={onClose}
      footer={
        rejected ? (
          <>
            <Button tone="danger" data-testid="delete-proposal" disabled={busy}
                    onClick={() => run(() => api.improvement.deleteProposal(proposal.id))}>
              Delete for good
            </Button>
            <Button tone="primary" data-testid="restore-proposal" disabled={busy}
                    onClick={() => run(() => api.improvement.restoreProposal(proposal.id))}>
              Bring it back
            </Button>
          </>
        ) : (
          <>
            <Button data-testid="reject-proposal" disabled={busy}
                    onClick={() => run(() => api.improvement.reject(proposal.id))}>
              No thanks
            </Button>
            {appliable ? (
              <Button tone="primary" data-testid="approve-proposal" disabled={busy}
                      onClick={() => run(() => api.improvement.approve(proposal.id))}>
                Apply it
              </Button>
            ) : (
              // Generating the brief IS the approval for these kinds. There is no
              // "apply" because there is no code path behind one.
              <Button tone="primary" data-testid="generate-brief" disabled={busy}
                      onClick={() => run(async () => {
                        const built = await api.improvement.brief(
                          proposal.id, target.trim() || 'your coding assistant');
                        setBrief(built.prompt);
                      }, false)}>
                {brief ? 'Rewrite the brief' : 'Write me a brief'}
              </Button>
            )}
          </>
        )
      }
    >
      <p className="text-[13px] text-ink-muted">{KIND_LABEL[proposal.kind] ?? proposal.kind}</p>
      {proposal.rationale && (
        <Field label="Why"><p className="text-[13px] text-ink">{proposal.rationale}</p></Field>
      )}
      {proposal.helpsJarvis && (
        <Field label="What it would change"><p className="text-[13px] text-ink">{proposal.helpsJarvis}</p></Field>
      )}
      {proposal.sourceUrl && (
        <Field label="Read somewhere else">
          <p className="break-all text-[12px] text-ink-faint">{proposal.sourceUrl}</p>
        </Field>
      )}

      {evidence.length > 0 && (
        <Field label="What this rests on"
               hint="A suggestion needs at least two separate outcomes agreeing.">
          <ul className="space-y-1" data-testid="proposal-evidence">
            {evidence.map((outcome) => (
              <li key={outcome.id} className="text-[12px] text-ink-faint">
                · {outcome.title} — {outcome.status}
              </li>
            ))}
          </ul>
        </Field>
      )}

      {!appliable && (
        <>
          <Field
            label="Who is writing this"
            hint="Jarvis never edits its own code, so this becomes a brief for whoever does."
          >
            <input className={inputClass} data-testid="brief-target"
                   placeholder="your coding assistant"
                   value={target} onChange={(event) => setTarget(event.target.value)} />
          </Field>
          {brief && (
            <Field label="Ready to paste">
              <textarea className={`${inputClass} font-mono text-[12px]`} rows={10} readOnly
                        data-testid="brief-text" value={brief} />
            </Field>
          )}
        </>
      )}

      {error && <p className="mt-2 text-[13px] text-state-danger">{error}</p>}
    </Modal>
  );
}

// --- changes -----------------------------------------------------------------------

function Changes() {
  const [rows, setRows] = useState<Change[] | null>(null);
  const [refused, setRefused] = useState<{ change: Change; message: string } | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setRows((await api.improvement.changes()).changes);
      setError(null);
    } catch (err) {
      setError(err instanceof ApiRequestError ? err.message : 'Could not read the change log.');
      setRows([]);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function undo(change: Change, force = false) {
    setError(null);
    try {
      const result = await api.improvement.undo(change.id, force);
      // Not an error: a real question got a real answer. The value changed since,
      // so restoring blind would overwrite a later decision.
      if (result.ok === false) {
        setRefused({ change, message: result.message });
        return;
      }
      setRefused(null);
      await load();
    } catch (err) {
      setError(err instanceof ApiRequestError ? err.message : 'Could not undo that.');
    }
  }

  return (
    <>
      {error && <p className="mb-4 text-[13px] text-state-danger">{error}</p>}

      {rows === null ? null : rows.length === 0 ? (
        <EmptyState
          title="Nothing has changed yet"
          body="Every change Jarvis makes to how it works is written down here, with what it was before, so you can take it back."
        />
      ) : (
        <div className="space-y-2" data-testid="change-list">
          {rows.map((change) => (
            <Card key={change.id} data-testid="change-row">
              <div className="flex items-start justify-between gap-4">
                <div className="min-w-0">
                  <p className="text-[14px] text-ink">{titleOf(change)}</p>
                  <p className="mt-1 text-[12px] text-ink-faint">
                    {change.reason ?? 'Changed'} · {change.appliedAt.slice(0, 10)}
                    {change.undoneAt && ' · already undone'}
                  </p>
                </div>
                {!change.undoneAt && change.kind !== 'undo' && (
                  <Button data-testid="undo-change" onClick={() => void undo(change)}>
                    Undo
                  </Button>
                )}
              </div>
            </Card>
          ))}
        </div>
      )}

      {refused && (
        <Modal
          open
          title="This changed since"
          onClose={() => setRefused(null)}
          footer={
            <Button tone="danger" data-testid="undo-force"
                    onClick={() => void undo(refused.change, true)}>
              Undo it anyway
            </Button>
          }
        >
          <p className="text-[13px] text-ink">{refused.message}</p>
          <p className="mt-3 text-[13px] text-ink-muted">
            Undoing would overwrite what you did to it. Nothing has been changed yet.
          </p>
        </Modal>
      )}
    </>
  );
}

/** A readable title from the change's own snapshot rather than a raw id — the id
 *  means nothing to anyone reading the log. */
function titleOf(change: Change): string {
  const snapshot = (change.after ?? change.before) as { text?: string } | null;
  if (snapshot?.text) return snapshot.text;
  if (change.kind === 'setting') return `Setting: ${change.target}`;
  if (change.kind === 'undo') return 'Took a change back';
  return change.target;
}

// --- learned ------------------------------------------------------------------------

function Learned() {
  const [rules, setRules] = useState<Rule[]>([]);
  const [lessons, setLessons] = useState<Lesson[]>([]);
  const [archived, setArchived] = useState(false);
  const [open, setOpen] = useState<Rule | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const [live, noticed] = await Promise.all([
        api.improvement.rules(archived),
        api.improvement.lessons(archived ? 'archived' : 'active'),
      ]);
      setRules(live.rules);
      setLessons(noticed.lessons);
      setError(null);
    } catch (err) {
      setError(err instanceof ApiRequestError ? err.message : 'Could not read what it has learned.');
    }
  }, [archived]);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <>
      <div className="mb-4 flex justify-end">
        <Button data-testid="toggle-archived" onClick={() => setArchived((on) => !on)}>
          {archived ? 'Back to live' : 'Show archived'}
        </Button>
      </div>

      {error && <p className="mb-4 text-[13px] text-state-danger">{error}</p>}

      <p className="mb-2 text-[12px] font-medium uppercase tracking-[0.12em] text-ink-faint">
        Rules it follows
      </p>
      {rules.length === 0 ? (
        <Card className="mb-6">
          <p className="text-[13px] text-ink-muted">
            {archived ? 'Nothing archived.' : 'No rules yet. One appears only when a pattern repeats.'}
          </p>
        </Card>
      ) : (
        <div className="mb-6 space-y-2" data-testid="rule-list">
          {rules.map((rule) => (
            <Card key={rule.id} interactive data-testid="rule-row" onClick={() => setOpen(rule)}>
              <div className="flex items-start justify-between gap-4">
                <div className="min-w-0">
                  <p className={`text-[14px] ${rule.active ? 'text-ink' : 'text-ink-faint line-through'}`}>
                    {rule.text}
                  </p>
                  <p className="mt-1 text-[12px] text-ink-faint">
                    {rule.scope === 'general' ? 'Everywhere' : rule.scope}
                    {!rule.active && ' · muted'}
                  </p>
                </div>
              </div>
            </Card>
          ))}
        </div>
      )}

      <p className="mb-2 text-[12px] font-medium uppercase tracking-[0.12em] text-ink-faint">
        Things it has noticed
      </p>
      {lessons.length === 0 ? (
        <Card>
          <p className="text-[13px] text-ink-muted">Nothing noticed yet.</p>
        </Card>
      ) : (
        <div className="space-y-2" data-testid="lesson-list">
          {lessons.map((lesson) => (
            <Card key={lesson.id} data-testid="lesson-row">
              <div className="flex items-start justify-between gap-4">
                <div className="min-w-0">
                  <p className="text-[14px] text-ink">{lesson.text}</p>
                  <p className="mt-1 text-[12px] text-ink-faint">
                    {/* An observation, not a behaviour change — worth being explicit
                        about, since the difference is the whole safeguard. */}
                    An observation{lesson.sourceTier > 1 ? ', from outside reading' : ''}
                  </p>
                </div>
                <Button
                  data-testid="lesson-archive"
                  onClick={() => void (archived
                    ? api.improvement.restoreLesson(lesson.id)
                    : api.improvement.archiveLesson(lesson.id)).then(load)}
                >
                  {archived ? 'Restore' : 'Archive'}
                </Button>
              </div>
            </Card>
          ))}
        </div>
      )}

      {open && (
        <RuleDetail
          rule={open}
          onClose={() => setOpen(null)}
          onChanged={async () => {
            setOpen(null);
            await load();
          }}
        />
      )}
    </>
  );
}

function RuleDetail({ rule, onClose, onChanged }: {
  rule: Rule;
  onClose: () => void;
  onChanged: () => Promise<void>;
}) {
  const [text, setText] = useState(rule.text);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function run(action: () => Promise<unknown>) {
    setBusy(true);
    setError(null);
    try {
      await action();
      await onChanged();
    } catch (err) {
      setError(err instanceof ApiRequestError ? err.message : 'That did not work.');
      setBusy(false);
    }
  }

  return (
    <Modal
      open
      title="This rule"
      onClose={onClose}
      footer={
        <>
          {rule.archivedAt ? (
            <>
              <Button data-testid="rule-restore" disabled={busy}
                      onClick={() => run(() => api.improvement.restoreRule(rule.id))}>
                Restore
              </Button>
              {/* Only from here: a change row's undo needs the rule to still exist,
                  so a hard delete always goes through archive first. */}
              <Button tone="danger" data-testid="rule-delete" disabled={busy}
                      onClick={() => run(() => api.improvement.deleteRule(rule.id))}>
                Delete for good
              </Button>
            </>
          ) : (
            <>
              <Button data-testid="rule-archive" disabled={busy}
                      onClick={() => run(() => api.improvement.archiveRule(rule.id))}>
                Archive
              </Button>
              <Button data-testid="rule-toggle" disabled={busy}
                      onClick={() => run(() => api.improvement.toggleRule(rule.id))}>
                {rule.active ? 'Mute it' : 'Unmute it'}
              </Button>
            </>
          )}
          <Button tone="primary" data-testid="rule-save"
                  disabled={busy || !text.trim() || text.trim() === rule.text}
                  onClick={() => run(() => api.improvement.editRule(rule.id, text.trim()))}>
            Save
          </Button>
        </>
      }
    >
      <Field label="What it tells itself to do"
             hint="Editing this is recorded like any other change, so it can be undone.">
        <textarea className={inputClass} rows={3} data-testid="rule-text"
                  value={text} onChange={(event) => setText(event.target.value)} />
      </Field>
      <Field label="Where it applies">
        <p className="text-[13px] text-ink-muted">
          {rule.scope === 'general' ? 'Everywhere' : rule.scope}
        </p>
      </Field>
      {error && <p className="mt-2 text-[13px] text-state-danger">{error}</p>}
    </Modal>
  );
}

// --- settings -------------------------------------------------------------------------

const TRUST: { id: Prefs['improvementTrust']; label: string; blurb: string }[] = [
  { id: 'ask', label: 'Ask me every time',
    blurb: 'Nothing changes until you have approved it.' },
  { id: 'balanced', label: 'Apply what it is sure of',
    blurb: 'A rule backed by enough of its own evidence applies itself. Everything else asks.' },
  { id: 'auto', label: 'Apply without asking',
    blurb: 'Still only its own rules and settings, never code, and everything is undoable here.' },
];

function Settings({ status, onChanged }: {
  status: ImprovementStatus | null;
  onChanged: () => Promise<void>;
}) {
  const [prefs, setPrefs] = useState<Prefs | null>(null);

  useEffect(() => {
    api.prefs.get().then(setPrefs).catch(() => setPrefs(null));
  }, []);

  async function update(patch: Partial<Prefs>) {
    setPrefs((current) => (current ? { ...current, ...patch } : current));
    try {
      setPrefs(await api.prefs.update(patch));
      await onChanged();
    } catch {
      const live = await api.prefs.get().catch(() => null);
      if (live) setPrefs(live);  // it did not take; do not claim it did
    }
  }

  if (!prefs) return null;

  return (
    <div className="space-y-4" data-testid="improvement-settings">
      <Card>
        <div className="flex items-center justify-between gap-4">
          <div>
            <p className="text-[14px] text-ink">Learn from its own work</p>
            <p className="mt-0.5 text-[12px] text-ink-faint">
              Off means it stops noticing and stops suggesting. What it has already learned stays.
            </p>
          </div>
          <Toggle label="Learn from its own work" checked={prefs.improvementEnabled}
                  onChange={(on) => void update({ improvementEnabled: on })} />
        </div>
      </Card>

      <Card>
        <p className="text-[13px] font-medium text-ink">When it works out how to do better</p>
        <div className="mt-3 space-y-1" data-testid="improvement-trust">
          {TRUST.map((option) => (
            <button
              key={option.id}
              type="button"
              data-testid={`improvement-trust-${option.id}`}
              aria-pressed={prefs.improvementTrust === option.id}
              onClick={() => void update({ improvementTrust: option.id })}
              className={[
                'block w-full rounded border px-3 py-2 text-left transition duration-150 ease-out',
                prefs.improvementTrust === option.id
                  ? 'border-accent/40 bg-accent/10'
                  : 'border-surface-border hover:border-surface-border-strong',
              ].join(' ')}
            >
              <span className={`block text-[13px] ${
                prefs.improvementTrust === option.id ? 'text-ink' : 'text-ink-muted'}`}>
                {option.label}
              </span>
              <span className="mt-0.5 block text-[12px] text-ink-faint">{option.blurb}</span>
            </button>
          ))}
        </div>
        <p className="mt-3 text-[12px] text-ink-faint">
          {/* The hard floor, stated because it is the reassuring part: it is not a
              level of the dial, and no setting reaches it. */}
          Whatever this is set to, anything it read somewhere else always asks first,
          and so does anything that would contradict a rule you already have.
        </p>
      </Card>

      <Card>
        <div className="flex items-center justify-between gap-4">
          <div>
            <p className="text-[14px] text-ink">Read up on things outside itself</p>
            <p className="mt-0.5 text-[12px] text-ink-faint">
              Looks things up on the web once a week. Anything it finds needs a second
              source before it counts, and always asks before changing anything.
            </p>
          </div>
          <Toggle label="Read up on things outside itself"
                  checked={prefs.improvementResearch === 'weekly'}
                  onChange={(on) => void update({ improvementResearch: on ? 'weekly' : 'off' })} />
        </div>
      </Card>

      {status && (
        <Card>
          <p className="text-[13px] font-medium text-ink">How much it may spend on this</p>
          <p className="mt-2 text-[13px] text-ink-muted" data-testid="improvement-budgets">
            {status.dailyBudgetRemaining} review
            {status.dailyBudgetRemaining === 1 ? '' : 's'} left today ·{' '}
            {status.weeklyBudgetRemaining} outside look
            {status.weeklyBudgetRemaining === 1 ? '' : 's'} left this week
          </p>
          <p className="mt-2 text-[12px] text-ink-faint">
            Deliberately small, so reflecting on its own work never crowds out what you
            actually asked for.
          </p>
        </Card>
      )}
    </div>
  );
}

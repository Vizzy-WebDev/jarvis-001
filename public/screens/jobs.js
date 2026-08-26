// Background Jobs screen — see root CLAUDE.md's Jobs section. A Job is
// long-running work Jarvis backgrounded (its own judgment, or the user's
// explicit "keep working on that in the background") — distinct from a
// Scheduled Task (screens/tasks.js), which runs on a schedule Jarvis has no
// judgment about at all. Kept updated live via the 'job_progress' SSE event
// (app.js's connectEvents() -> refreshIfActive('jobs')).
//
// Master/detail in one container, modeled on tasks.js's own render() shape,
// kept in a single file since a job's detail view is materially simpler
// than a task's edit form (nothing here is ever hand-edited — a job's own
// goal/plan were set once, by whichever admission call created it).

import { fieldTextarea, sectionCard, armedButton, postJson, flashSaveError } from './_helpers.js';
import { openModal } from './_modal.js';

const STATUS_LABELS = {
  queued: 'Queued',
  planning: 'Planning',
  running: 'Running',
  awaiting_decision: 'Waiting on you',
  done: 'Done',
  failed: 'Failed',
  cancelled: 'Cancelled',
  orphaned: 'Interrupted',
};

const RECOVERY_LABELS = {
  resumable: 'Safe to resume right where it left off',
  restartable: "Can't fully trust its progress, but safe to start over",
  needs_input: 'Was already waiting on a decision when this happened',
  unrecoverable: "Already did something that can't be safely repeated",
};

function badgeClassFor(status) {
  if (status === 'done') return 'badge good';
  if (status === 'failed' || status === 'cancelled') return 'badge bad';
  if (status === 'awaiting_decision' || status === 'orphaned') return 'badge warn';
  return 'badge'; // queued/planning/running
}

function formatWhen(iso) {
  if (!iso) return '';
  return new Date(iso).toLocaleString('en-US', { weekday: 'short', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
}

async function fetchJobs() {
  const res = await fetch('/api/jobs');
  const data = await res.json();
  return data.jobs || [];
}

async function fetchJobDetail(id) {
  const res = await fetch(`/api/jobs/${encodeURIComponent(id)}`);
  return res.json();
}

// ---------- list ----------

function buildJobRow(job, onOpen) {
  const row = document.createElement('div');
  row.className = 'list-row list-row-clickable';
  row.tabIndex = 0;
  row.setAttribute('role', 'button');

  const main = document.createElement('div');
  main.className = 'list-row-main';
  main.appendChild(Object.assign(document.createElement('span'), { className: 'list-row-title', textContent: job.title }));

  const subBits = [formatWhen(job.createdAt), job.kind];
  // Set only when this job is one piece of an approved split
  // (request_job_split.js) — a flat list otherwise gives no hint that
  // several rows are really one larger piece of work.
  if (job.parentId) subBits.push('part of a larger job');
  if (job.status === 'awaiting_decision' && job.waitingOnYou) subBits.push(job.waitingOnYou);
  else if (job.status === 'done' && job.result) subBits.push(job.result);
  else if (job.status === 'failed' && job.error) subBits.push(job.error);
  main.appendChild(Object.assign(document.createElement('span'), { className: 'list-row-sub', textContent: subBits.filter(Boolean).join(' · ') }));

  const badge = document.createElement('span');
  badge.className = badgeClassFor(job.status);
  badge.textContent = STATUS_LABELS[job.status] || job.status;
  main.appendChild(badge);

  row.appendChild(main);
  row.addEventListener('click', () => onOpen(job.id));
  row.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      onOpen(job.id);
    }
  });
  return row;
}

function buildJobsCard(jobs, onOpen) {
  const card = sectionCard('Background jobs');
  if (!jobs.length) {
    card.appendChild(
      Object.assign(document.createElement('p'), {
        className: 'hint',
        textContent: 'Nothing running in the background right now — Jarvis will start one on its own for a long task, or you can ask it to.',
      })
    );
    return card;
  }
  for (const j of jobs) card.appendChild(buildJobRow(j, onOpen));
  return card;
}

// ---------- create modal ----------

function buildNewJobModal(onChange) {
  let goalField, titleField;
  return openModal({
    title: 'Start a background job',
    submitLabel: 'Start',
    busyLabel: 'Starting…',
    build(body) {
      titleField = document.createElement('div');
      titleField.className = 'field';
      titleField.appendChild(Object.assign(document.createElement('label'), { textContent: 'Name (optional)' }));
      const titleInput = document.createElement('input');
      titleInput.type = 'text';
      titleInput.placeholder = 'Leave blank to use the goal itself';
      titleField.appendChild(titleInput);
      titleField.input = titleInput;
      body.appendChild(titleField);

      goalField = fieldTextarea('What should Jarvis do?', 'A clear, complete description of the goal — it will not have this conversation to refer back to.');
      body.appendChild(goalField.wrapper);
    },
    async onSubmit(api) {
      const goal = goalField.textarea.value.trim();
      if (!goal) {
        api.setError('Please describe what the job should do.');
        return null;
      }
      const data = await postJson('/api/jobs', { title: titleField.input.value.trim() || undefined, goal });
      if (!data.ok) {
        api.setError(data.error || 'Could not start that job.');
        return null;
      }
      if (data.atCapacity) {
        api.setError(`Already at capacity — ${data.running.length} job(s) running. Stop one first.`);
        return null;
      }
      return data;
    },
  }).then((result) => {
    if (result) onChange();
    return result;
  });
}

// ---------- detail ----------

function traceRow(t) {
  const row = document.createElement('div');
  row.className = 'list-row';
  const main = document.createElement('div');
  main.className = 'list-row-main';
  main.appendChild(Object.assign(document.createElement('span'), { className: 'list-row-title', textContent: t.summary }));
  main.appendChild(Object.assign(document.createElement('span'), { className: 'list-row-sub', textContent: `${formatWhen(t.createdAt)} · ${t.kind}` }));
  row.appendChild(main);
  return row;
}

function buildTraceCard(trace) {
  const card = sectionCard('Activity');
  if (!trace.length) {
    card.appendChild(Object.assign(document.createElement('p'), { className: 'hint', textContent: 'Nothing recorded yet.' }));
    return card;
  }
  // Newest first, and capped — a long-running job's full trace can be sizable;
  // this is a glance at what happened, not a debug log viewer.
  for (const t of trace.slice(-30).reverse()) card.appendChild(traceRow(t));
  return card;
}

function buildPlanCard(plan) {
  if (!plan) return null;
  const card = sectionCard('Plan');
  if (plan.summary) card.appendChild(Object.assign(document.createElement('p'), { textContent: plan.summary }));
  if (Array.isArray(plan.steps) && plan.steps.length) {
    const list = document.createElement('ul');
    for (const step of plan.steps) list.appendChild(Object.assign(document.createElement('li'), { textContent: step }));
    card.appendChild(list);
  }
  return card;
}

async function renderDetail(container, jobId, onBack, onChange, onOpenJob) {
  container.innerHTML = '';

  const backBtn = document.createElement('button');
  backBtn.type = 'button';
  backBtn.className = 'btn';
  backBtn.textContent = '← Background Jobs';
  backBtn.addEventListener('click', onBack);
  container.appendChild(backBtn);

  const { job, trace, outbox } = await fetchJobDetail(jobId);
  if (!job) {
    container.appendChild(Object.assign(document.createElement('p'), { className: 'hint', textContent: 'This job no longer exists.' }));
    return;
  }

  const header = sectionCard(job.title);
  const badge = document.createElement('span');
  badge.className = badgeClassFor(job.status);
  badge.textContent = STATUS_LABELS[job.status] || job.status;
  header.appendChild(badge);
  header.appendChild(Object.assign(document.createElement('p'), { className: 'hint', textContent: `${job.kind} · started ${formatWhen(job.startedAt) || 'not yet'}${job.finishedAt ? ` · finished ${formatWhen(job.finishedAt)}` : ''}` }));
  header.appendChild(Object.assign(document.createElement('p'), { textContent: job.goal }));
  if (job.result) header.appendChild(Object.assign(document.createElement('p'), { className: 'hint', textContent: `Result: ${job.result}` }));
  if (job.error) header.appendChild(Object.assign(document.createElement('p'), { className: 'hint', textContent: `Error: ${job.error}` }));
  if (job.parentId) {
    const parentLink = document.createElement('button');
    parentLink.type = 'button';
    parentLink.className = 'btn';
    parentLink.textContent = '↑ Part of a larger job — view it';
    parentLink.addEventListener('click', () => onOpenJob(job.parentId));
    header.appendChild(parentLink);
  }
  container.appendChild(header);

  const planCard = buildPlanCard(job.plan);
  if (planCard) container.appendChild(planCard);

  // ---- pending decision / recovery actions ----
  const pending = outbox.filter((o) => !o.deliveredAt);
  if (pending.length) {
    const card = sectionCard('Waiting on you');
    for (const p of pending) {
      card.appendChild(Object.assign(document.createElement('p'), { textContent: p.summary }));
    }
    if (job.status === 'orphaned') {
      card.appendChild(Object.assign(document.createElement('p'), { className: 'hint', textContent: RECOVERY_LABELS[job.recovery] || '' }));
      const actions = document.createElement('div');
      actions.className = 'list-row-actions';
      if (job.recovery === 'resumable') {
        const resumeBtn = document.createElement('button');
        resumeBtn.type = 'button';
        resumeBtn.className = 'btn btn-primary';
        resumeBtn.textContent = 'Resume';
        resumeBtn.addEventListener('click', async () => {
          resumeBtn.disabled = true;
          await postJson(`/api/jobs/${encodeURIComponent(job.id)}/resume`, {});
          onChange();
        });
        actions.appendChild(resumeBtn);
      }
      if (job.recovery !== 'unrecoverable') {
        const restartBtn = document.createElement('button');
        restartBtn.type = 'button';
        restartBtn.className = 'btn';
        restartBtn.textContent = 'Restart';
        restartBtn.addEventListener('click', async () => {
          restartBtn.disabled = true;
          await postJson(`/api/jobs/${encodeURIComponent(job.id)}/restart`, {});
          onChange();
        });
        actions.appendChild(restartBtn);
      }
      actions.appendChild(
        armedButton('Discard', 'Really discard?', async () => {
          await postJson(`/api/jobs/${encodeURIComponent(job.id)}/discard`, {});
          onChange();
        })
      );
      card.appendChild(actions);
    } else if (job.status === 'awaiting_decision') {
      const guidanceField = fieldTextarea('Guidance (optional)', 'What should it try instead?');
      card.appendChild(guidanceField.wrapper);
      const actions = document.createElement('div');
      actions.className = 'list-row-actions';
      const keepGoingBtn = document.createElement('button');
      keepGoingBtn.type = 'button';
      keepGoingBtn.className = 'btn btn-primary';
      keepGoingBtn.textContent = 'Keep going';
      keepGoingBtn.addEventListener('click', async () => {
        keepGoingBtn.disabled = true;
        const data = await postJson(`/api/jobs/${encodeURIComponent(job.id)}/resume-stuck`, { guidance: guidanceField.textarea.value.trim() || undefined });
        if (!data.ok) {
          keepGoingBtn.disabled = false;
          flashSaveError(card, data.error || 'Could not resume that job.');
          return;
        }
        onChange();
      });
      actions.appendChild(keepGoingBtn);
      actions.appendChild(
        armedButton('Stop', 'Really stop this job?', async () => {
          await postJson(`/api/jobs/${encodeURIComponent(job.id)}/discard`, {});
          onChange();
        })
      );
      card.appendChild(actions);
    }
    container.appendChild(card);
  } else if (job.status === 'queued' || job.status === 'running' || job.status === 'planning') {
    container.appendChild(
      armedButton('Stop', 'Really stop this job?', async () => {
        await postJson(`/api/jobs/${encodeURIComponent(job.id)}/discard`, {});
        onChange();
      })
    );
  }

  container.appendChild(buildTraceCard(trace));
}

// ---------- screen ----------

export async function render(container) {
  async function showList() {
    container.innerHTML = '';
    const jobs = await fetchJobs();
    // Newest first, and each row's "waiting on you" text needs its own
    // pending-outbox lookup — cheap enough to do per row for a screen this
    // small (jobs are not a high-volume list the way chat messages are).
    const withPending = await Promise.all(
      jobs
        .slice()
        .reverse()
        .map(async (j) => {
          if (j.status !== 'awaiting_decision') return j;
          const detail = await fetchJobDetail(j.id).catch(() => null);
          const pending = detail?.outbox?.find((o) => !o.deliveredAt);
          return { ...j, waitingOnYou: pending?.summary || null };
        })
    );

    container.appendChild(buildJobsCard(withPending, showDetail));

    const newBtn = document.createElement('button');
    newBtn.type = 'button';
    newBtn.className = 'btn btn-primary';
    newBtn.textContent = '+ Start a background job';
    newBtn.addEventListener('click', () => buildNewJobModal(showList));
    container.appendChild(newBtn);
  }

  async function showDetail(jobId) {
    await renderDetail(container, jobId, showList, showList, showDetail);
  }

  await showList();
}

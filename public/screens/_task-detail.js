// Scheduled Tasks detail page — opened from a row in tasks.js. Same pattern
// as _skill-detail.js/_connector-detail.js/_notification-detail.js: not a
// hash route (public/router.js's currentSectionId() only matches exact
// SECTIONS ids), tasks.js swaps its own container's content between its
// list view and this view internally.
//
// There is no GET /api/tasks/:id route — task-store.js's getTask(id) has
// never been exposed over HTTP. Rather than add one, this follows the same
// pattern _notification-detail.js already uses for task runs and
// connectors: fetch the (already small) full list and find the one it
// needs.

import { sectionCard, armedButton, postJson } from './_helpers.js';
import { toggleSwitch } from './_ui.js';
import { formatWhen, describeRecurrence, buildTaskModal } from './tasks.js';
import { render as renderRunDetail } from './_task-run-detail.js';

const NOTIFY_LABELS = {
  on_error: 'Only if something goes wrong',
  always: 'Every time it runs',
  never: 'Never',
};

/** What a task's action actually does, in plain English, plus which model handles it — the exact information the create/edit form collects but the list row never showed. */
function describeAction(action, models) {
  const modelLabel = action.modelId
    ? models.find((m) => m.id === action.modelId)?.label || 'a model that no longer exists'
    : 'Auto (best fit)';
  if (action.type === 'message') {
    return { kind: 'Sends a reminder', detail: action.text || '(no message set)' };
  }
  if (action.type === 'briefing') {
    return { kind: 'Runs your morning briefing', detail: `Using: ${modelLabel}` };
  }
  return { kind: 'Follows these instructions', detail: action.text || '(no instructions set)', model: `Using: ${modelLabel}` };
}

export async function render(container, { taskId, onBack }) {
  container.innerHTML = '';

  const header = document.createElement('div');
  header.className = 'detail-header';
  const backBtn = document.createElement('button');
  backBtn.type = 'button';
  backBtn.className = 'btn';
  backBtn.textContent = '← Scheduled Tasks';
  backBtn.addEventListener('click', onBack);
  header.appendChild(backBtn);
  container.appendChild(header);

  const [tasksRes, runsRes, modelsRes] = await Promise.all([
    fetch('/api/tasks'),
    fetch(`/api/task-runs?taskId=${encodeURIComponent(taskId)}`),
    fetch('/api/models'),
  ]);
  const { tasks } = await tasksRes.json();
  const { runs } = await runsRes.json();
  const { models } = await modelsRes.json();
  const task = tasks.find((t) => t.id === taskId);

  if (!task) {
    container.appendChild(Object.assign(document.createElement('p'), { className: 'error', textContent: 'This task no longer exists.' }));
    return;
  }

  const refresh = () => render(container, { taskId, onBack });

  const titleRow = document.createElement('div');
  titleRow.className = 'detail-title-row';
  titleRow.appendChild(Object.assign(document.createElement('h2'), { textContent: task.title }));

  const toggle = toggleSwitch({
    value: task.enabled,
    onChange: async (next) => {
      try {
        const data = await postJson(`/api/tasks/${encodeURIComponent(task.id)}`, { enabled: next }, 'PATCH');
        if (!data.ok) throw new Error(data.error || 'Save failed');
        await refresh();
      } catch {
        // The PATCH didn't actually take — leave the switch showing the
        // real, unsaved state rather than a value that silently didn't
        // persist.
        toggle.setValue(!next);
      }
    },
  });
  toggle.wrapper.classList.add('detail-action-btn');
  titleRow.appendChild(toggle.wrapper);
  container.appendChild(titleRow);

  container.appendChild(
    Object.assign(document.createElement('p'), {
      className: 'hint',
      textContent: task.enabled ? describeRecurrence(task.recurrence) : `${describeRecurrence(task.recurrence)} — currently off`,
    })
  );

  const doesCard = sectionCard('What it does');
  const action = describeAction(task.action, models);
  doesCard.appendChild(Object.assign(document.createElement('p'), { className: 'list-row-title', textContent: action.kind }));
  doesCard.appendChild(Object.assign(document.createElement('p'), { textContent: action.detail }));
  if (action.model) doesCard.appendChild(Object.assign(document.createElement('p'), { className: 'hint', textContent: action.model }));
  container.appendChild(doesCard);

  const infoCard = sectionCard('Details');
  infoCard.appendChild(Object.assign(document.createElement('p'), { className: 'hint', textContent: `Notify: ${NOTIFY_LABELS[task.notify] || task.notify}` }));
  if (task.nextRunAt) {
    infoCard.appendChild(Object.assign(document.createElement('p'), { className: 'hint', textContent: `Next run: ${formatWhen(task.nextRunAt)}` }));
  }
  if (task.lastRunAt) {
    infoCard.appendChild(Object.assign(document.createElement('p'), { className: 'hint', textContent: `Last ran: ${formatWhen(task.lastRunAt)}` }));
  }
  container.appendChild(infoCard);

  const actionsRow = document.createElement('div');
  actionsRow.className = 'button-row';

  const editBtn = document.createElement('button');
  editBtn.type = 'button';
  editBtn.className = 'btn';
  editBtn.textContent = 'Edit';
  editBtn.addEventListener('click', () => buildTaskModal(models, refresh, task));
  actionsRow.appendChild(editBtn);

  const runBtn = document.createElement('button');
  runBtn.type = 'button';
  runBtn.className = 'btn';
  runBtn.textContent = 'Run now';
  runBtn.addEventListener('click', async () => {
    runBtn.disabled = true;
    runBtn.textContent = 'Running…';
    try {
      await fetch(`/api/tasks/${encodeURIComponent(task.id)}/run`, { method: 'POST' });
      await refresh();
    } finally {
      runBtn.disabled = false;
      runBtn.textContent = 'Run now';
    }
  });
  actionsRow.appendChild(runBtn);

  const deleteBtn = armedButton('Delete', 'Really delete?', async () => {
    await fetch(`/api/tasks/${encodeURIComponent(task.id)}`, { method: 'DELETE' });
    onBack();
  });
  actionsRow.appendChild(deleteBtn);

  container.appendChild(actionsRow);

  const runsCard = sectionCard('Recent runs');
  if (!runs.length) {
    runsCard.appendChild(Object.assign(document.createElement('p'), { className: 'hint', textContent: 'This task has not run yet.' }));
  } else {
    for (const r of runs.slice(0, 10)) {
      const row = document.createElement('div');
      row.className = 'list-row list-row-clickable';
      row.tabIndex = 0;
      row.setAttribute('role', 'button');
      const main = document.createElement('div');
      main.className = 'list-row-main';
      main.appendChild(Object.assign(document.createElement('span'), { className: 'list-row-sub', textContent: formatWhen(r.ranAt) }));
      const badge = document.createElement('span');
      badge.className = `badge ${r.ok ? 'good' : 'bad'}`;
      badge.textContent = r.ok ? 'ok' : 'error';
      main.appendChild(badge);
      row.appendChild(main);
      const open = () => renderRunDetail(container, { runId: r.id, taskId: task.id, onBack: refresh, backLabel: `← ${task.title}` });
      row.addEventListener('click', open);
      row.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          open();
        }
      });
      runsCard.appendChild(row);
    }
  }
  container.appendChild(runsCard);
}

// One task run's full detail — opened from a row in tasks.js's "Recent
// activity" list, or from a task's own "Recent runs" list in
// _task-detail.js. Same fetch-the-list-and-find-by-id pattern
// _notification-detail.js already uses for the same data — there is no
// GET /api/task-runs/:id route, only task-store.js's listRuns(taskId).
//
// This exists because buildRunRow() (tasks.js) fetched `run.summary` — the
// actual text output of the run — and simply never rendered it. Only
// notifications' own detail view showed it before this.

import { sectionCard } from './_helpers.js';
import { formatWhen, runSummaryBits } from './tasks.js';

export async function render(container, { runId, taskId, onBack, backLabel }) {
  container.innerHTML = '';

  const header = document.createElement('div');
  header.className = 'detail-header';
  const backBtn = document.createElement('button');
  backBtn.type = 'button';
  backBtn.className = 'btn';
  backBtn.textContent = backLabel || '← Back';
  backBtn.addEventListener('click', onBack);
  header.appendChild(backBtn);
  container.appendChild(header);

  const res = await fetch(taskId ? `/api/task-runs?taskId=${encodeURIComponent(taskId)}` : '/api/task-runs');
  const { runs } = await res.json();
  const run = (runs || []).find((r) => r.id === runId);

  if (!run) {
    container.appendChild(
      Object.assign(document.createElement('p'), { className: 'error', textContent: 'This run is no longer in the task history.' })
    );
    return;
  }

  const titleRow = document.createElement('div');
  titleRow.className = 'detail-title-row';
  titleRow.appendChild(Object.assign(document.createElement('h2'), { textContent: run.title }));
  const badge = document.createElement('span');
  badge.className = `badge ${run.ok ? 'good' : 'bad'}`;
  badge.textContent = run.ok ? 'ok' : 'error';
  titleRow.appendChild(badge);
  container.appendChild(titleRow);

  for (const bit of runSummaryBits(run)) {
    container.appendChild(Object.assign(document.createElement('p'), { className: 'hint', textContent: bit }));
  }

  const card = sectionCard('Result');
  card.appendChild(Object.assign(document.createElement('p'), { textContent: run.summary || '(nothing was recorded)' }));
  container.appendChild(card);
}

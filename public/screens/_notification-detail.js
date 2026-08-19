// The per-notification detail page — opened from a row in notifications.js.
// Same pattern as _connector-detail.js/_skill-detail.js: not a hash route
// (public/router.js's currentSectionId() only matches exact SECTIONS ids,
// so a sub-route like #/notifications/n123 isn't possible without a router
// change), notifications.js swaps its own container's content between its
// list view and this view internally.
//
// What this actually shows beyond the title/body a row already had depends
// on `meta`, which is empty for most notifications (voice/system ones) —
// those just get the same content in a bigger frame. Where a notification's
// server-side call site *did* enrich `meta` (see server/scheduler.js,
// server.js's oauth callback, monitor/engine.js), this fetches the real
// underlying record and shows it.

import { sectionCard, armedButton, postJson } from './_helpers.js';
import { runSummaryBits } from './tasks.js';
import { navigate } from '../router.js';

function fullTime(iso) {
  return new Date(iso).toLocaleString('en-US', {
    weekday: 'short', month: 'short', day: 'numeric', year: 'numeric', hour: 'numeric', minute: '2-digit',
  });
}

/** The extra section a notification's `meta` unlocks, or null if there's nothing more to show than the title/body already visible above it. */
async function buildMetaSection(n) {
  if (n.meta?.runId) {
    try {
      const res = await fetch('/api/task-runs');
      const data = await res.json();
      const run = (data.runs || []).find((r) => r.id === n.meta.runId);
      if (!run) {
        const card = sectionCard('Task run');
        card.appendChild(Object.assign(document.createElement('p'), { className: 'hint', textContent: 'This run is no longer in the task history.' }));
        return card;
      }
      const card = sectionCard('Task run');
      for (const bit of runSummaryBits(run)) {
        card.appendChild(Object.assign(document.createElement('p'), { className: 'hint', textContent: bit }));
      }
      if (run.summary) {
        card.appendChild(Object.assign(document.createElement('p'), { textContent: run.summary }));
      }
      const goBtn = document.createElement('button');
      goBtn.type = 'button';
      goBtn.className = 'btn';
      goBtn.textContent = 'View in Scheduled Tasks';
      goBtn.addEventListener('click', () => navigate('tasks'));
      card.appendChild(goBtn);
      return card;
    } catch {
      return null; // a failed fetch here just means less detail, not a broken page
    }
  }

  if (n.meta?.connectorId) {
    try {
      const res = await fetch('/api/connectors');
      const data = await res.json();
      const connector = (data.connectors || []).find((c) => c.id === n.meta.connectorId);
      const card = sectionCard('Connector');
      if (!connector) {
        card.appendChild(Object.assign(document.createElement('p'), { className: 'hint', textContent: 'This connector no longer exists.' }));
        return card;
      }
      card.appendChild(Object.assign(document.createElement('p'), { textContent: connector.label || connector.id }));
      card.appendChild(Object.assign(document.createElement('p'), { className: 'hint', textContent: `Status: ${connector.status || 'unknown'}` }));
      const goBtn = document.createElement('button');
      goBtn.type = 'button';
      goBtn.className = 'btn';
      goBtn.textContent = 'View in App Control';
      goBtn.addEventListener('click', () => navigate('app-control'));
      card.appendChild(goBtn);
      return card;
    } catch {
      return null;
    }
  }

  // meta.monitorId: monitors are ephemeral (expired/stopped) with no
  // standalone lookup route, and the notification's own body already
  // states what was being watched for — nothing more to add reliably.
  return null;
}

export async function render(container, { notification, onBack }) {
  container.innerHTML = '';
  const n = notification;

  const header = document.createElement('div');
  header.className = 'detail-header';
  const backBtn = document.createElement('button');
  backBtn.type = 'button';
  backBtn.className = 'btn';
  backBtn.textContent = '← Notifications';
  backBtn.addEventListener('click', onBack);
  header.appendChild(backBtn);
  container.appendChild(header);

  const titleRow = document.createElement('div');
  titleRow.className = 'detail-title-row';
  const dot = document.createElement('span');
  dot.className = `notif-dot level-${n.level}`;
  titleRow.appendChild(dot);
  titleRow.appendChild(Object.assign(document.createElement('h2'), { textContent: n.title }));
  container.appendChild(titleRow);

  container.appendChild(Object.assign(document.createElement('p'), { className: 'hint', textContent: fullTime(n.ts) }));

  if (n.body) {
    container.appendChild(Object.assign(document.createElement('p'), { textContent: n.body }));
  }

  const metaSection = await buildMetaSection(n);
  if (metaSection) container.appendChild(metaSection);

  const actionsRow = document.createElement('div');
  actionsRow.className = 'button-row';

  if (n.action?.label) {
    const goBtn = document.createElement('button');
    goBtn.type = 'button';
    goBtn.className = 'btn btn-primary';
    goBtn.textContent = n.action.label;
    goBtn.addEventListener('click', () => navigate(n.action.section));
    actionsRow.appendChild(goBtn);
  }

  const deleteBtn = armedButton('Delete', 'Really delete?', async () => {
    await fetch(`/api/notifications/${encodeURIComponent(n.id)}`, { method: 'DELETE' });
    onBack();
  });
  actionsRow.appendChild(deleteBtn);

  container.appendChild(actionsRow);
}

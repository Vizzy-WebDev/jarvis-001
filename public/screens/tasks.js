// Scheduled Tasks screen — create/view/run/delete tasks, and see recent run
// history (including anything that ran "late" because Jarvis was closed at
// its scheduled time, and which model actually handled each run — see
// server/scheduler/scheduler.js). Tasks created here use the same store as
// the schedule_task voice skill, so a reminder set by talking to Jarvis
// shows up here too, and vice versa. Creation is a popup (see _modal.js)
// rather than a permanently-inline form.

import { fieldInput, fieldSelect, sectionCard, armedButton, postJson } from './_helpers.js';
import { openModal } from './_modal.js';
import { iconTile, segmented, popover, noticeBox, toggleSwitch } from './_ui.js';

const RECUR_TYPES = [
  ['once', 'Once'],
  ['daily', 'Every day'],
  ['weekdays', 'Weekdays'],
  ['weekly', 'Specific days each week'],
  ['interval', 'Every N minutes'],
];

const DAY_NAMES = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];

// The 3 "when it runs, Jarvis should..." modes offered by the segmented
// control in the Instructions section. A 4th option ('skill' — run one
// specific skill directly) isn't offered here — this version has no
// per-task skill picker at all (see the "coming later" Connectors badge and
// the blanket permission notice further down).
const INSTRUCTION_MODES = [
  ['prompt', 'Instructions'],
  ['message', 'Just remind me'],
  ['briefing', 'Morning briefing'],
];

export function formatWhen(iso) {
  return new Date(iso).toLocaleString('en-US', { weekday: 'short', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
}

/** The "ran using X — auto-switched from Y (reason)" / "ran late" / error bits for one task run — shared by buildRunRow() below and _notification-detail.js's task_run view, so the two never drift apart on what a run actually looked like. */
export function runSummaryBits(r) {
  const bits = [formatWhen(r.ranAt)];
  if (r.late) bits.push('ran late — Jarvis was closed at the scheduled time');
  if (r.modelLabel) {
    let bit = `ran using ${r.modelLabel}`;
    if (r.switchedFromLabel) bit += ` — auto-switched from ${r.switchedFromLabel}${r.switchReason ? ` (${r.switchReason})` : ''}`;
    bits.push(bit);
  }
  if (r.pinnedModelMissing) bits.push('the model picked for this task no longer exists — used the usual auto-pick instead');
  if (!r.ok && r.error) bits.push(r.error);
  return bits;
}

function buildRecurrence(type, { date, time, days, everyMinutes }) {
  if (type === 'once') {
    if (!date || !time) return null;
    const at = new Date(`${date}T${time}:00`);
    if (Number.isNaN(at.getTime())) return null;
    return { type: 'once', at: at.toISOString() };
  }
  if (type === 'daily' || type === 'weekdays') {
    if (!time) return null;
    return { type, time };
  }
  if (type === 'weekly') {
    if (!time || !days.length) return null;
    return { type: 'weekly', time, days };
  }
  if (type === 'interval') {
    if (!everyMinutes || everyMinutes <= 0) return null;
    return { type: 'interval', everyMs: everyMinutes * 60 * 1000 };
  }
  return null;
}

// A small toolbar-button trigger for "which model handles this run" — opens
// a popover listing "Auto (best fit)" plus every enabled model, grouped by
// the connection it belongs to (falls back to a flat, ungrouped list for
// any model whose connection isn't in `connections` — e.g. the extra fetch
// this needed failed — so a model is never silently missing from the
// picker). Returns `{el, getValue()}`; '' means Auto.
function buildModelPicker(models, connections) {
  let selected = '';

  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'counter-btn';
  btn.textContent = 'Auto';

  let pop = null;
  btn.addEventListener('click', () => {
    pop = popover({
      anchor: btn,
      build(body) {
        const list = document.createElement('div');
        list.className = 'popover-list';

        const autoBtn = document.createElement('button');
        autoBtn.type = 'button';
        autoBtn.className = 'popover-option';
        autoBtn.textContent = 'Auto (best fit)';
        autoBtn.addEventListener('click', () => {
          selected = '';
          btn.textContent = 'Auto';
          pop.close();
        });
        list.appendChild(autoBtn);

        const byConnection = new Map();
        for (const m of models) {
          const key = m.connectionId || '';
          if (!byConnection.has(key)) byConnection.set(key, []);
          byConnection.get(key).push(m);
        }

        const groupedConnectionIds = new Set();
        for (const conn of connections) {
          const group = byConnection.get(conn.id);
          if (!group || !group.length) continue;
          groupedConnectionIds.add(conn.id);
          list.appendChild(
            Object.assign(document.createElement('div'), { className: 'popover-group-label', textContent: conn.label })
          );
          for (const m of group) {
            const optBtn = document.createElement('button');
            optBtn.type = 'button';
            optBtn.className = 'popover-option';
            // `m.ready` only means "has a key/connection configured" — every
            // model here passes that. The checkmark is meant to mean "known
            // to be working right now", which is `availability.state`
            // (server/models/health.js's tracked, measured signal), not
            // readiness.
            optBtn.textContent = m.label + (m.availability?.state === 'working' ? ' ✓' : '');
            optBtn.addEventListener('click', () => {
              selected = m.id;
              btn.textContent = m.label;
              pop.close();
            });
            list.appendChild(optBtn);
          }
        }

        // Any model whose connection wasn't found above (grouping fetch
        // failed, or an orphaned model) — list it flat rather than hiding it.
        for (const m of models) {
          if (groupedConnectionIds.has(m.connectionId || '')) continue;
          const optBtn = document.createElement('button');
          optBtn.type = 'button';
          optBtn.className = 'popover-option';
          optBtn.textContent = m.label + (m.availability?.state === 'working' ? ' ✓' : '');
          optBtn.addEventListener('click', () => {
            selected = m.id;
            btn.textContent = m.label;
            pop.close();
          });
          list.appendChild(optBtn);
        }

        body.appendChild(list);
      },
    });
  });

  return { el: btn, getValue: () => selected };
}

// ---------- task list ----------

function buildTaskRow(task, onChange) {
  const row = document.createElement('div');
  row.className = 'list-row';

  const main = document.createElement('div');
  main.className = 'list-row-main';
  main.appendChild(Object.assign(document.createElement('span'), { className: 'list-row-title', textContent: task.title }));
  const sub = document.createElement('span');
  sub.className = 'list-row-sub';
  sub.textContent = task.enabled && task.nextRunAt ? `Next: ${formatWhen(task.nextRunAt)}` : 'Off';
  main.appendChild(sub);

  const actions = document.createElement('div');
  actions.className = 'list-row-actions';

  const toggle = toggleSwitch({
    value: task.enabled,
    onChange: async (next) => {
      await postJson(`/api/tasks/${encodeURIComponent(task.id)}`, { enabled: next }, 'PATCH');
      await onChange();
    },
  });

  const runBtn = document.createElement('button');
  runBtn.type = 'button';
  runBtn.className = 'btn';
  runBtn.textContent = 'Run now';
  runBtn.addEventListener('click', async () => {
    runBtn.disabled = true;
    runBtn.textContent = 'Running…';
    try {
      await fetch(`/api/tasks/${encodeURIComponent(task.id)}/run`, { method: 'POST' });
      await onChange();
    } finally {
      runBtn.disabled = false;
      runBtn.textContent = 'Run now';
    }
  });

  const removeBtn = armedButton('Delete', 'Really delete?', async () => {
    await fetch(`/api/tasks/${encodeURIComponent(task.id)}`, { method: 'DELETE' });
    await onChange();
  });

  actions.append(toggle.wrapper, runBtn, removeBtn);
  row.append(main, actions);
  return row;
}

function buildTasksCard(tasks, onChange) {
  const card = sectionCard('Scheduled tasks');
  if (!tasks.length) {
    card.appendChild(
      Object.assign(document.createElement('p'), {
        className: 'hint',
        textContent: 'Nothing scheduled yet — create one below, or just tell Jarvis "remind me to...".',
      })
    );
    return card;
  }
  for (const t of tasks) card.appendChild(buildTaskRow(t, onChange));
  return card;
}

// ---------- recent activity ----------

function buildRunRow(r) {
  const row = document.createElement('div');
  row.className = 'list-row';
  const main = document.createElement('div');
  main.className = 'list-row-main';
  main.appendChild(Object.assign(document.createElement('span'), { className: 'list-row-title', textContent: r.title }));

  main.appendChild(Object.assign(document.createElement('span'), { className: 'list-row-sub', textContent: runSummaryBits(r).join(' · ') }));

  const badge = document.createElement('span');
  badge.className = `badge ${r.ok ? 'good' : 'bad'}`;
  badge.textContent = r.ok ? 'ok' : 'error';
  main.appendChild(badge);

  row.appendChild(main);
  return row;
}

function buildRunsCard(runs) {
  const card = sectionCard('Recent activity');
  if (!runs.length) {
    card.appendChild(Object.assign(document.createElement('p'), { className: 'hint', textContent: 'Nothing has run yet.' }));
    return card;
  }
  for (const r of runs.slice(0, 10)) card.appendChild(buildRunRow(r));
  return card;
}

// ---------- create-task modal ----------

async function buildCreateTaskModal(models, onChange) {
  // The model picker needs each model's connection label to group by, which
  // `models` (as passed in from render(), already fetched once) doesn't
  // carry — a second, small fetch of the same endpoint here, rather than
  // changing what render() fetches/passes, keeps this rewrite scoped to
  // just the create-task modal.
  let connections = [];
  try {
    const res = await fetch('/api/models');
    const data = await res.json();
    connections = data.connections || [];
  } catch {
    connections = [];
  }

  let titleField, typeField, dateField, timeField, minutesField, daysWrapper, dayChecks;
  let modeSeg, promptTextarea, messageField, toolbarLeft, instructionsBox, notice, notifyField;

  const modelPicker = buildModelPicker(models, connections);

  return openModal({
    title: 'Create a task',
    size: 'wide',
    submitLabel: 'Create task',
    busyLabel: 'Creating…',
    build(body, api) {
      // ---- name row ----
      const nameRow = document.createElement('div');
      nameRow.className = 'task-name-row';
      titleField = fieldInput('Name', 'text', 'e.g. "Morning stretch"');
      nameRow.append(iconTile('⚡'), titleField.wrapper);
      body.appendChild(nameRow);

      // ---- schedule section ----
      const scheduleHeader = document.createElement('div');
      scheduleHeader.className = 'section-label-row';
      scheduleHeader.appendChild(
        Object.assign(document.createElement('span'), { className: 'section-label', textContent: 'Schedule' })
      );
      body.appendChild(scheduleHeader);

      typeField = fieldSelect('Repeats', RECUR_TYPES);
      body.appendChild(typeField.wrapper);

      dateField = fieldInput('Date', 'date');
      timeField = fieldInput('Time', 'time');
      minutesField = fieldInput('Every how many minutes', 'number', 'e.g. 60');
      body.append(dateField.wrapper, timeField.wrapper, minutesField.wrapper);

      daysWrapper = document.createElement('div');
      daysWrapper.className = 'field';
      daysWrapper.appendChild(Object.assign(document.createElement('label'), { textContent: 'Days' }));
      const daysRow = document.createElement('div');
      daysRow.className = 'days-row';
      dayChecks = DAY_NAMES.map((name, idx) => {
        const lbl = document.createElement('label');
        lbl.className = 'day-check';
        const cb = document.createElement('input');
        cb.type = 'checkbox';
        cb.value = String(idx);
        lbl.append(cb, document.createTextNode(name));
        daysRow.appendChild(lbl);
        return cb;
      });
      daysWrapper.appendChild(daysRow);
      body.appendChild(daysWrapper);

      // ---- instructions section ----
      const instructionsHeader = document.createElement('div');
      instructionsHeader.className = 'section-label-row';
      instructionsHeader.appendChild(
        Object.assign(document.createElement('span'), { className: 'section-label', textContent: 'Instructions' })
      );
      modeSeg = segmented(INSTRUCTION_MODES, { value: 'prompt', onChange: () => refreshVisibility() });
      instructionsHeader.appendChild(modeSeg.wrapper);
      body.appendChild(instructionsHeader);

      instructionsBox = document.createElement('div');
      instructionsBox.className = 'instructions-box';

      promptTextarea = document.createElement('textarea');
      promptTextarea.placeholder = 'Describe what Jarvis should do each time…';
      instructionsBox.appendChild(promptTextarea);

      const toolbar = document.createElement('div');
      toolbar.className = 'instructions-toolbar';
      toolbarLeft = document.createElement('div');
      toolbarLeft.className = 'instructions-toolbar-left';
      // Connectors means real external app/service integrations (MCP or
      // similar) — none exist yet, so this is an inert label, not a picker,
      // marked the same way Calendar/Email are marked on the Morning
      // Briefing page. Jarvis's own built-in abilities (opening an app,
      // browsing, searching, checking the weather, ...) aren't "connectors"
      // and don't get a per-task toggle here — see the blanket notice below.
      toolbarLeft.appendChild(
        Object.assign(document.createElement('span'), { className: 'badge', textContent: 'Connectors — coming later' })
      );
      toolbar.append(toolbarLeft, modelPicker.el);
      instructionsBox.appendChild(toolbar);
      body.appendChild(instructionsBox);

      messageField = fieldInput('Message', 'text', 'e.g. "Time to stretch!"');
      body.appendChild(messageField.wrapper);

      // ---- consent notice (prompt mode only — see refreshNotice) ----
      notice = noticeBox();
      notice.checkbox.addEventListener('change', () => api.setSubmitEnabled(notice.checkbox.checked));
      body.appendChild(notice.wrapper);

      // ---- notification ----
      notifyField = fieldSelect('Notification', [
        ['on_error', 'Only if something goes wrong'],
        ['always', 'Every time it runs'],
        ['never', 'Never'],
      ]);
      notifyField.select.value = 'on_error';
      body.appendChild(notifyField.wrapper);

      function refreshRecurrenceVisibility() {
        const type = typeField.select.value;
        dateField.wrapper.classList.toggle('hidden', type !== 'once');
        timeField.wrapper.classList.toggle('hidden', !['once', 'daily', 'weekdays', 'weekly'].includes(type));
        minutesField.wrapper.classList.toggle('hidden', type !== 'interval');
        daysWrapper.classList.toggle('hidden', type !== 'weekly');
      }

      // A single fixed heads-up rather than one built from checked
      // connectors — there's nothing left to enumerate (Connectors is
      // "coming later", Skills isn't in this version), but a prompt-mode
      // task can still lead Jarvis to open apps/websites/searches on its
      // own on a schedule, which is worth a plain warning before that's
      // allowed to run unattended.
      function refreshNotice(api) {
        const show = modeSeg.getValue() === 'prompt';
        notice.wrapper.classList.toggle('hidden', !show);
        if (!show) {
          notice.checkbox.checked = false;
          api.setSubmitEnabled(true);
          return;
        }
        notice.textEl.textContent =
          'This task\'s instructions may lead Jarvis to open apps, browse websites, search the web, or use ' +
          'its other built-in abilities on its own, without asking first each time it runs.';
        api.setSubmitEnabled(notice.checkbox.checked);
      }

      function refreshVisibility() {
        const mode = modeSeg.getValue();
        instructionsBox.classList.toggle('hidden', mode === 'message');
        promptTextarea.classList.toggle('hidden', mode !== 'prompt');
        toolbarLeft.classList.toggle('hidden', mode !== 'prompt');
        messageField.wrapper.classList.toggle('hidden', mode !== 'message');
        refreshNotice(api);
      }

      typeField.select.addEventListener('change', refreshRecurrenceVisibility);
      refreshRecurrenceVisibility();
      refreshVisibility();
    },
    async onSubmit(api) {
      if (!titleField.input.value.trim()) {
        api.setError('Please give this task a name.');
        return null;
      }

      const recurrence = buildRecurrence(typeField.select.value, {
        date: dateField.input.value,
        time: timeField.input.value,
        days: dayChecks.filter((cb) => cb.checked).map((cb) => Number(cb.value)),
        everyMinutes: Number(minutesField.input.value),
      });
      if (!recurrence) {
        api.setError('Please fill in the schedule details.');
        return null;
      }

      const mode = modeSeg.getValue();
      const modelId = modelPicker.getValue() || undefined;
      let action;
      if (mode === 'prompt') {
        const text = promptTextarea.value.trim();
        if (!text) {
          api.setError('Please describe what Jarvis should do.');
          return null;
        }
        // No `connectors` key at all — absent/undefined means "unrestricted"
        // to runner.js, which is the correct way to say "every built-in
        // ability is available" now that there's no per-item picker to
        // build a narrower list from (Connectors is "coming later", Skills
        // isn't in this version). The server-side allowlist plumbing this
        // used to feed is left in place for when a real picker comes back.
        action = { type: 'prompt', text, modelId };
      } else if (mode === 'briefing') {
        action = { type: 'briefing', modelId };
      } else {
        action = { type: 'message', text: messageField.input.value.trim() || titleField.input.value.trim() };
      }

      if (mode === 'prompt' && !notice.checkbox.checked) {
        api.setError('Please confirm you understand this runs automatically.');
        return null;
      }

      const data = await postJson('/api/tasks', {
        title: titleField.input.value.trim(),
        recurrence,
        action,
        notify: notifyField.select.value,
      });
      if (!data.ok) {
        api.setError(data.error || 'Could not create that task.');
        return null;
      }
      return data;
    },
  }).then((result) => {
    if (result) onChange();
  });
}

export async function render(container) {
  container.innerHTML = '';
  const [tasksRes, runsRes, modelsRes] = await Promise.all([fetch('/api/tasks'), fetch('/api/task-runs'), fetch('/api/models')]);
  const { tasks } = await tasksRes.json();
  const { runs } = await runsRes.json();
  const { models } = await modelsRes.json();
  const onChange = () => render(container);

  container.appendChild(buildTasksCard(tasks, onChange));

  const newBtn = document.createElement('button');
  newBtn.type = 'button';
  newBtn.className = 'btn btn-primary';
  newBtn.textContent = '+ New task';
  newBtn.addEventListener('click', () => buildCreateTaskModal(models, onChange));
  container.appendChild(newBtn);

  container.appendChild(buildRunsCard(runs));
}

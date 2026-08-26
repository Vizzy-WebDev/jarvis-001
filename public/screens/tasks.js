// Scheduled Tasks screen — create/view/edit/run/delete tasks, see recent run
// history (including anything that ran "late" because Jarvis was closed at
// its scheduled time, and which model actually handled each run — see
// server/scheduler/scheduler.js), and see any active "watch for X" monitors.
// Tasks created here use the same store as the schedule_task voice skill, so
// a reminder set by talking to Jarvis shows up here too, and vice versa.
// Creation AND editing are both the same popup (see _modal.js) rather than a
// permanently-inline form — editing reuses PATCH /api/tasks/:id, which has
// always existed server-side but was never called with more than {enabled}
// until this screen grew a real task detail page (_task-detail.js).

import { fieldInput, fieldSelect, sectionCard, armedButton, postJson } from './_helpers.js';
import { openModal } from './_modal.js';
import { iconTile, segmented, popover, noticeBox, toggleSwitch } from './_ui.js';
import { render as renderTaskDetail } from './_task-detail.js';
import { render as renderRunDetail } from './_task-run-detail.js';

const RECUR_TYPES = [
  ['once', 'Once'],
  ['daily', 'Every day'],
  ['weekdays', 'Weekdays'],
  ['weekly', 'Specific days each week'],
  ['interval', 'Every N minutes'],
];

const DAY_NAMES = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
const DAY_NAMES_FULL = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'];

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

function formatTimeOfDay(time) {
  const [h, m] = String(time || '00:00').split(':').map((n) => parseInt(n, 10));
  const d = new Date();
  d.setHours(Number.isFinite(h) ? h : 0, Number.isFinite(m) ? m : 0, 0, 0);
  return d.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' });
}

/**
 * Plain-English description of a recurrence spec — the row subtitle used to
 * just say "Next: …" or the bare word "Off", with no way to see the actual
 * schedule without opening the create form. A small client-side port of
 * server/scheduler/recurrence.js's describe(): public/ has no access to
 * server/ modules, so this is duplicated rather than imported — keep the two
 * in sync if the recurrence shape ever changes.
 */
export function describeRecurrence(spec) {
  if (!spec || !spec.type) return 'never';
  switch (spec.type) {
    case 'once': {
      const at = new Date(spec.at);
      return Number.isNaN(at.getTime())
        ? 'once'
        : `once, on ${at.toLocaleDateString('en-US', { weekday: 'long', month: 'long', day: 'numeric' })} at ${at.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' })}`;
    }
    case 'daily':
      return `every day at ${formatTimeOfDay(spec.time)}`;
    case 'weekdays':
      return `weekdays at ${formatTimeOfDay(spec.time)}`;
    case 'weekly': {
      const days = Array.isArray(spec.days) && spec.days.length ? spec.days : [];
      const names = days.map((d) => DAY_NAMES_FULL[d]).filter(Boolean);
      return `every ${names.join(', ') || 'week'} at ${formatTimeOfDay(spec.time)}`;
    }
    case 'interval': {
      const mins = Math.round((Number(spec.everyMs) || 0) / 60000);
      if (mins > 0 && mins % 1440 === 0) return `every ${mins / 1440} day${mins / 1440 === 1 ? '' : 's'}`;
      if (mins > 0 && mins % 60 === 0) return `every ${mins / 60} hour${mins / 60 === 1 ? '' : 's'}`;
      return `every ${mins} minute${mins === 1 ? '' : 's'}`;
    }
    default:
      return 'never';
  }
}

/** The "ran using X — auto-switched from Y (reason)" / "ran late" / error bits for one task run — shared by buildRunRow() below and _notification-detail.js's / _task-run-detail.js's task_run view, so they never drift apart on what a run actually looked like. */
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

/** iso -> the local {date, time} strings <input type=date>/<input type=time> expect — the reverse of buildRecurrence()'s `new Date(`${date}T${time}:00`)`, so editing a 'once' task round-trips through local time correctly rather than drifting via UTC. */
function localDateTimeParts(iso) {
  const d = new Date(iso);
  const pad = (n) => String(n).padStart(2, '0');
  return {
    date: `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`,
    time: `${pad(d.getHours())}:${pad(d.getMinutes())}`,
  };
}

// A small toolbar-button trigger for "which model handles this run" — opens
// a popover listing "Auto (best fit)" plus every enabled model, grouped by
// the connection it belongs to (falls back to a flat, ungrouped list for
// any model whose connection isn't in `connections` — e.g. the extra fetch
// this needed failed — so a model is never silently missing from the
// picker). Returns `{el, getValue(), setValue(id)}` — setValue is what lets
// the edit modal preselect the model a task is already pinned to.
function buildModelPicker(models, connections, initial = '') {
  let selected = initial;

  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'counter-btn';

  function labelFor(id) {
    if (!id) return 'Auto';
    return models.find((m) => m.id === id)?.label || 'Auto';
  }
  btn.textContent = labelFor(selected);

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

  return {
    el: btn,
    getValue: () => selected,
    setValue(id) {
      selected = id || '';
      btn.textContent = labelFor(selected);
    },
  };
}

// ---------- task list ----------

function buildTaskRow(task, onOpen, onChange) {
  const row = document.createElement('div');
  row.className = 'list-row list-row-clickable';
  row.tabIndex = 0;
  row.setAttribute('role', 'button');

  const main = document.createElement('div');
  main.className = 'list-row-main';
  main.appendChild(Object.assign(document.createElement('span'), { className: 'list-row-title', textContent: task.title }));
  const sub = document.createElement('span');
  sub.className = 'list-row-sub';
  const schedule = describeRecurrence(task.recurrence);
  sub.textContent = task.enabled
    ? task.nextRunAt
      ? `${schedule} · Next: ${formatWhen(task.nextRunAt)}`
      : schedule
    : `${schedule} · Off`;
  main.appendChild(sub);

  const actions = document.createElement('div');
  actions.className = 'list-row-actions';
  // Every action here lives inside a row that's now clickable end-to-end —
  // without this, clicking the toggle or Run now would also fire the row's
  // own click handler and open the detail page underneath it.
  actions.addEventListener('click', (e) => e.stopPropagation());

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
  row.addEventListener('click', () => onOpen(task.id));
  row.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      onOpen(task.id);
    }
  });
  return row;
}

function buildTasksCard(tasks, onOpen, onChange) {
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
  for (const t of tasks) card.appendChild(buildTaskRow(t, onOpen, onChange));
  return card;
}

// ---------- watching for ("watch for X" monitors) ----------

function buildMonitorRow(m, onChange) {
  const row = document.createElement('div');
  row.className = 'list-row';
  const main = document.createElement('div');
  main.className = 'list-row-main';
  main.appendChild(Object.assign(document.createElement('span'), { className: 'list-row-title', textContent: m.description || 'Something' }));
  main.appendChild(
    Object.assign(document.createElement('span'), {
      className: 'list-row-sub',
      textContent: m.expiresAt ? `Watching until ${formatWhen(m.expiresAt)}` : 'Watching',
    })
  );
  row.appendChild(main);

  const actions = document.createElement('div');
  actions.className = 'list-row-actions';
  const stopBtn = document.createElement('button');
  stopBtn.type = 'button';
  stopBtn.className = 'btn';
  stopBtn.textContent = 'Stop';
  stopBtn.addEventListener('click', async () => {
    stopBtn.disabled = true;
    try {
      await fetch(`/api/monitors/${encodeURIComponent(m.id)}/stop`, { method: 'POST' });
    } finally {
      await onChange();
    }
  });
  actions.appendChild(stopBtn);
  row.appendChild(actions);
  return row;
}

/** Only shown when at least one monitor is actively watching — returns null otherwise, so the screen doesn't grow an empty "Watching for" card most of the time. */
function buildMonitorsCard(monitors, onChange) {
  const active = monitors.filter((m) => m.status === 'watching');
  if (!active.length) return null;
  const card = sectionCard('Watching for');
  for (const m of active) card.appendChild(buildMonitorRow(m, onChange));
  return card;
}

// ---------- recent activity ----------

function buildRunRow(r, onOpen) {
  const row = document.createElement('div');
  row.className = 'list-row list-row-clickable';
  row.tabIndex = 0;
  row.setAttribute('role', 'button');
  const main = document.createElement('div');
  main.className = 'list-row-main';
  main.appendChild(Object.assign(document.createElement('span'), { className: 'list-row-title', textContent: r.title }));

  main.appendChild(Object.assign(document.createElement('span'), { className: 'list-row-sub', textContent: runSummaryBits(r).join(' · ') }));

  const badge = document.createElement('span');
  badge.className = `badge ${r.ok ? 'good' : 'bad'}`;
  badge.textContent = r.ok ? 'ok' : 'error';
  main.appendChild(badge);

  row.appendChild(main);
  row.addEventListener('click', () => onOpen(r));
  row.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      onOpen(r);
    }
  });
  return row;
}

function buildRunsCard(runs, onOpen, showAll, onToggleShowAll) {
  const card = sectionCard('Recent activity');
  if (!runs.length) {
    card.appendChild(Object.assign(document.createElement('p'), { className: 'hint', textContent: 'Nothing has run yet.' }));
    return card;
  }
  const visible = showAll ? runs : runs.slice(0, 10);
  for (const r of visible) card.appendChild(buildRunRow(r, onOpen));
  // The server keeps up to 200 runs (task-store.js's MAX_RUNS_KEPT) — this
  // used to hard-cap the list at 10 with no way to see the rest at all.
  if (runs.length > 10) {
    const toggleBtn = document.createElement('button');
    toggleBtn.type = 'button';
    toggleBtn.className = 'btn';
    toggleBtn.textContent = showAll ? 'Show recent only' : `Show all (${runs.length})`;
    toggleBtn.addEventListener('click', onToggleShowAll);
    card.appendChild(toggleBtn);
  }
  return card;
}

// ---------- create/edit-task modal ----------

/**
 * Builds and opens the task modal — creation when `existingTask` is omitted,
 * editing when it's given. This used to be create-only (`buildCreateTaskModal`,
 * POST-only); the fields, visibility rules, and validation are unchanged —
 * only the prefill-from-an-existing-task step and the PATCH-instead-of-POST
 * branch on submit are new. Exported so _task-detail.js's Edit button can
 * open the exact same form the list's "+ New task" button does.
 */
export async function buildTaskModal(models, onChange, existingTask = null) {
  // The model picker needs each model's connection label to group by, which
  // `models` (as passed in from render(), already fetched once) doesn't
  // carry — a second, small fetch of the same endpoint here, rather than
  // changing what render() fetches/passes, keeps this scoped to just the
  // modal.
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

  const modelPicker = buildModelPicker(models, connections, existingTask?.action?.modelId || '');

  return openModal({
    title: existingTask ? 'Edit task' : 'Create a task',
    size: 'wide',
    submitLabel: existingTask ? 'Save changes' : 'Create task',
    busyLabel: existingTask ? 'Saving…' : 'Creating…',
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

      // ---- prefill from an existing task, for editing ----
      if (existingTask) {
        titleField.input.value = existingTask.title || '';
        notifyField.select.value = existingTask.notify || 'on_error';

        const rec = existingTask.recurrence || {};
        typeField.select.value = rec.type || 'once';
        if (rec.type === 'once' && rec.at) {
          const { date, time } = localDateTimeParts(rec.at);
          dateField.input.value = date;
          timeField.input.value = time;
        } else if (rec.type === 'daily' || rec.type === 'weekdays') {
          timeField.input.value = rec.time || '';
        } else if (rec.type === 'weekly') {
          timeField.input.value = rec.time || '';
          const days = rec.days || [];
          dayChecks.forEach((cb) => {
            cb.checked = days.includes(Number(cb.value));
          });
        } else if (rec.type === 'interval') {
          minutesField.input.value = String(Math.round((Number(rec.everyMs) || 0) / 60000));
        }
        refreshRecurrenceVisibility();

        const action = existingTask.action || {};
        modeSeg.setValue(action.type || 'prompt');
        if (action.type === 'prompt') {
          promptTextarea.value = action.text || '';
        } else if (action.type === 'message') {
          messageField.input.value = action.text || '';
        }
        refreshVisibility();
        // Editing an already-running task means the user already consented
        // to it once — don't make them re-tick the box just to change the
        // time.
        if (action.type === 'prompt') {
          notice.checkbox.checked = true;
          api.setSubmitEnabled(true);
        }
      }
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

      const payload = {
        title: titleField.input.value.trim(),
        recurrence,
        action,
        notify: notifyField.select.value,
      };
      const data = existingTask
        ? await postJson(`/api/tasks/${encodeURIComponent(existingTask.id)}`, payload, 'PATCH')
        : await postJson('/api/tasks', payload);
      if (!data.ok) {
        api.setError(data.error || (existingTask ? 'Could not save that task.' : 'Could not create that task.'));
        return null;
      }
      return data;
    },
  }).then((result) => {
    if (result) onChange();
    return result;
  });
}

// ---------- screen ----------

export async function render(container) {
  let showAllRuns = false;

  async function showList() {
    container.innerHTML = '';
    const [tasksRes, runsRes, modelsRes, monitorsRes] = await Promise.all([
      fetch('/api/tasks'),
      fetch('/api/task-runs'),
      fetch('/api/models'),
      fetch('/api/monitors'),
    ]);
    const { tasks } = await tasksRes.json();
    const { runs } = await runsRes.json();
    const { models } = await modelsRes.json();
    const monitorsData = await monitorsRes.json().catch(() => ({ monitors: [] }));
    const onChange = showList;

    container.appendChild(buildTasksCard(tasks, showDetail, onChange));

    const monitorsCard = buildMonitorsCard(monitorsData.monitors || [], onChange);
    if (monitorsCard) container.appendChild(monitorsCard);

    const newBtn = document.createElement('button');
    newBtn.type = 'button';
    newBtn.className = 'btn btn-primary';
    newBtn.textContent = '+ New task';
    newBtn.addEventListener('click', () => buildTaskModal(models, onChange));
    container.appendChild(newBtn);

    let runsCardEl = buildRunsCard(runs, openRun, showAllRuns, toggleShowAll);
    container.appendChild(runsCardEl);

    function toggleShowAll() {
      showAllRuns = !showAllRuns;
      const next = buildRunsCard(runs, openRun, showAllRuns, toggleShowAll);
      runsCardEl.replaceWith(next);
      runsCardEl = next;
    }

    function openRun(run) {
      showRunDetail(run.id);
    }
  }

  async function showDetail(taskId) {
    container.innerHTML = '';
    await renderTaskDetail(container, { taskId, onBack: showList });
  }

  async function showRunDetail(runId) {
    container.innerHTML = '';
    await renderRunDetail(container, { runId, onBack: showList, backLabel: '← Scheduled Tasks' });
  }

  await showList();
}

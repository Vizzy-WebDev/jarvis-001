// Skills screen — rebuilt from scratch against a real screenshot of
// Claude's own Skills interface (see CLAUDE.md's permanent Skills rule and
// its Skills research notes for the full "how it works, how it looks"
// writeup this was built from). Shows ONLY the user's own Skills — no
// marketplace/Browse view, since Jarvis has no third-party skills ecosystem
// to browse. `GET /api/skills/installed` (server-side: listUserSkills(),
// server/skills/store/skill-files.js) is the sole data source this screen
// reads from — it has no code path that can ever return one of Jarvis's own
// built-in abilities, which is what makes the native-ability leak this
// screen used to have structurally impossible now, not just remembered.
//
// Jarvis's built-in abilities (weather, opening an app, desktop control,
// ...) are never shown here, never listed, never offered as something to
// "add" — see CLAUDE.md's permanent rule. A Skill is a recipe Jarvis
// follows using its own abilities, never a rename of one.

import { fieldInput, fieldTextarea, postJson } from './_helpers.js';
import { openModal } from './_modal.js';
import { popover, segmented } from './_ui.js';
import { render as renderSkillDetail } from './_skill-detail.js';

function formatDate(iso) {
  if (!iso) return '—';
  return new Date(iso).toLocaleDateString('en-US', { month: 'numeric', day: 'numeric', year: '2-digit' });
}

// ---------- list row ----------

function buildSkillRow(skill, onOpen) {
  const row = document.createElement('div');
  row.className = 'list-row list-row-clickable';
  row.tabIndex = 0;
  row.setAttribute('role', 'button');

  const main = document.createElement('div');
  main.className = 'list-row-main';
  main.appendChild(Object.assign(document.createElement('span'), { className: 'list-row-title', textContent: skill.name }));
  if (!skill.enabled) {
    main.appendChild(Object.assign(document.createElement('span'), { className: 'badge', textContent: 'Off' }));
  }
  row.appendChild(main);

  const meta = document.createElement('div');
  meta.className = 'skill-row-meta';
  meta.appendChild(Object.assign(document.createElement('span'), { textContent: formatDate(skill.updatedAt || skill.installedAt) }));
  meta.appendChild(Object.assign(document.createElement('span'), { textContent: 'You' }));
  row.appendChild(meta);

  row.addEventListener('click', onOpen);
  row.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      onOpen();
    }
  });
  return row;
}

// ---------- + Add menu ----------

function buildWriteModal(onChange) {
  let nameField, descField, instructionsField;
  return openModal({
    title: 'Write skill instructions',
    size: 'wide',
    submitLabel: 'Create',
    busyLabel: 'Creating…',
    build(body) {
      nameField = fieldInput('Skill name', 'text', 'weekly-status-report');
      body.appendChild(nameField.wrapper);
      descField = fieldTextarea('Description', 'Generate weekly status reports from recent work. Use when asked for updates or progress summaries.');
      body.appendChild(descField.wrapper);
      instructionsField = fieldTextarea(
        'Instructions',
        'Summarize my recent work in three sections: wins, blockers, and next steps. Keep the tone professional but not stiff…'
      );
      body.appendChild(instructionsField.wrapper);
    },
    async onSubmit(api) {
      if (!nameField.input.value.trim()) {
        api.setError('Please give this skill a name.');
        return null;
      }
      if (!descField.textarea.value.trim()) {
        api.setError('Describe what this skill does and when to use it.');
        return null;
      }
      if (!instructionsField.textarea.value.trim()) {
        api.setError('Please describe what Jarvis should do.');
        return null;
      }
      const data = await postJson('/api/skills', {
        name: nameField.input.value.trim(),
        description: descField.textarea.value.trim(),
        instructions: instructionsField.textarea.value.trim(),
      });
      if (!data.ok) {
        api.setError(data.error || 'Could not create that skill.');
        return null;
      }
      return data;
    },
  }).then((result) => {
    if (result) onChange();
  });
}

// Matches Claude's real "Upload skill" dialog (verified against a real
// screenshot — see CLAUDE.md's Skills section): a drag-and-drop zone plus a
// click-to-browse fallback, accepting the same three real formats Claude
// does (.zip, .skill — confirmed to just be a renamed .zip — or a bare .md
// for a skill with no extra files), and the same file-requirements text.
// Which of the three a dropped/chosen file actually is gets decided
// server-side by content (see skill-zip.js) — the `accept` attribute here is
// only a picker-filtering hint, not what enforces anything.
function buildUploadDropzone() {
  const zone = document.createElement('div');
  zone.className = 'upload-dropzone';
  zone.tabIndex = 0;
  zone.setAttribute('role', 'button');

  const icon = document.createElement('div');
  icon.className = 'upload-dropzone-icon';
  icon.textContent = '+';
  const label = document.createElement('p');
  label.textContent = 'Drag and drop or click to upload';
  zone.append(icon, label);

  const fileInput = document.createElement('input');
  fileInput.type = 'file';
  fileInput.accept = '.zip,.skill,.md';
  fileInput.className = 'hidden';

  // Tracked directly rather than read back from `fileInput.files` at submit
  // time — a browser only ever populates a file input's own `.files` list
  // from ITS OWN picker dialog. A drop event on `zone` (a different element)
  // never touches it, which was a real bug: the dropzone's label updated to
  // show the dropped file's name, but clicking Upload still saw no file at
  // all and said so. Tracking the File object here makes both paths behave
  // identically, which is what was actually missing.
  let selectedFile = null;

  function choose(file) {
    if (!file) return;
    selectedFile = file;
    label.textContent = file.name;
  }

  zone.addEventListener('click', () => fileInput.click());
  zone.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      fileInput.click();
    }
  });
  fileInput.addEventListener('change', () => choose(fileInput.files?.[0]));

  zone.addEventListener('dragover', (e) => {
    e.preventDefault();
    zone.classList.add('dragover');
  });
  zone.addEventListener('dragleave', () => zone.classList.remove('dragover'));
  zone.addEventListener('drop', (e) => {
    e.preventDefault();
    zone.classList.remove('dragover');
    choose(e.dataTransfer?.files?.[0]);
  });

  return { wrapper: zone, fileInput, getFile: () => selectedFile };
}

function buildUploadModal(onChange) {
  let dropzone;
  return openModal({
    title: 'Upload a skill',
    submitLabel: 'Upload',
    busyLabel: 'Uploading…',
    build(body) {
      dropzone = buildUploadDropzone();
      body.append(dropzone.wrapper, dropzone.fileInput);

      const requirements = document.createElement('ul');
      requirements.className = 'upload-requirements';
      requirements.appendChild(
        Object.assign(document.createElement('li'), { textContent: '.md file must contain a skill name and description formatted in YAML' })
      );
      requirements.appendChild(
        Object.assign(document.createElement('li'), { textContent: '.zip or .skill file must include a SKILL.md file' })
      );
      body.appendChild(requirements);
    },
    async onSubmit(api) {
      const file = dropzone.getFile();
      if (!file) {
        api.setError('Choose a file first.');
        return null;
      }
      const res = await fetch('/api/skills/upload', { method: 'POST', body: file });
      const data = await res.json();
      if (!data.ok) {
        api.setError(data.error || 'Could not install that skill.');
        return null;
      }
      return data;
    },
  }).then((result) => {
    if (result) onChange();
  });
}

// Mirrors Claude's own "Create with Claude": not a separate screen, just a
// shortcut into ordinary conversation. Closes this screen, focuses the
// composer, and pre-fills a starter line — server/skills/create_skill.js is
// what actually saves the skill once the conversation settles on one, the
// same tool "make me a skill that does X" reaches from plain conversation.
function startCreateWithJarvis() {
  window.dispatchEvent(
    new CustomEvent('jarvis:start-skill-chat', { detail: { text: "Let's create a skill together. First ask me what it should do." } })
  );
}

function openAddMenu(anchor, onChange) {
  const pop = popover({
    anchor,
    build(body) {
      const list = document.createElement('div');
      list.className = 'popover-list';
      const options = [
        ['Create with Jarvis', startCreateWithJarvis],
        ['Write skill instructions', () => buildWriteModal(onChange)],
        ['Upload a skill', () => buildUploadModal(onChange)],
      ];
      for (const [label, action] of options) {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'popover-option';
        btn.textContent = label;
        btn.addEventListener('click', () => {
          pop.close();
          action();
        });
        list.appendChild(btn);
      }
      body.appendChild(list);
    },
  });
}

// ---------- screen ----------

export async function render(container) {
  let filter = 'all'; // 'all' | 'on' | 'off'
  let sort = 'updated'; // 'updated' | 'name'
  let search = '';

  async function showList() {
    container.innerHTML = '';
    const res = await fetch('/api/skills/installed');
    const { skills } = await res.json();

    // No page-title heading here — the generic-screen chrome (router.js's
    // renderInto()) already shows "Skills" as the page header; a second one
    // here would just duplicate it (and, as a flex sibling of a plain
    // button in the same row, stretched the button to an odd tall shape).
    const header = document.createElement('div');
    header.className = 'button-row';

    const addBtn = document.createElement('button');
    addBtn.type = 'button';
    addBtn.className = 'btn btn-primary';
    addBtn.textContent = '+ Add';
    addBtn.addEventListener('click', () => openAddMenu(addBtn, showList));
    header.appendChild(addBtn);
    container.appendChild(header);

    const toolbar = document.createElement('div');
    toolbar.className = 'button-row';

    const searchField = fieldInput('', 'search', 'Search skills…');
    searchField.input.value = search;
    searchField.input.addEventListener('input', () => {
      search = searchField.input.value;
      renderRows();
    });
    toolbar.appendChild(searchField.wrapper);

    // "Show:" makes it unambiguous this narrows which already-installed
    // skills the list displays — it never changes any skill's own state.
    // Without a label, the All/On/Off pills read as though clicking "Off"
    // turns every skill off, which it never does (a real report from live
    // use — see CLAUDE.md's Skills section).
    toolbar.appendChild(Object.assign(document.createElement('span'), { className: 'toolbar-label', textContent: 'Show:' }));
    const filterPicker = segmented(
      [
        ['all', 'All'],
        ['on', 'On'],
        ['off', 'Off'],
      ],
      { value: filter, onChange: (v) => { filter = v; renderRows(); } }
    );
    toolbar.appendChild(filterPicker.wrapper);

    const sortPicker = segmented(
      [
        ['updated', 'Last updated'],
        ['name', 'Name A–Z'],
      ],
      { value: sort, onChange: (v) => { sort = v; renderRows(); } }
    );
    toolbar.appendChild(sortPicker.wrapper);

    container.appendChild(toolbar);

    const listWrap = document.createElement('div');
    listWrap.className = 'section-card';
    container.appendChild(listWrap);

    function renderRows() {
      listWrap.innerHTML = '';
      let rows = skills.filter((s) => (filter === 'all' ? true : filter === 'on' ? s.enabled : !s.enabled));
      if (search.trim()) {
        const q = search.trim().toLowerCase();
        rows = rows.filter((s) => s.name.toLowerCase().includes(q) || s.description.toLowerCase().includes(q));
      }
      rows = [...rows].sort((a, b) =>
        sort === 'name' ? a.name.localeCompare(b.name) : new Date(b.updatedAt || b.installedAt || 0) - new Date(a.updatedAt || a.installedAt || 0)
      );

      if (!skills.length) {
        listWrap.appendChild(
          Object.assign(document.createElement('p'), {
            className: 'hint',
            textContent: "You haven't made any skills yet — use + Add above to get started.",
          })
        );
        return;
      }
      if (!rows.length) {
        listWrap.appendChild(Object.assign(document.createElement('p'), { className: 'hint', textContent: 'Nothing matches.' }));
        return;
      }
      for (const skill of rows) listWrap.appendChild(buildSkillRow(skill, () => showDetail(skill.name)));
    }

    renderRows();
  }

  async function showDetail(name) {
    container.innerHTML = '';
    await renderSkillDetail(container, { name, onBack: showList });
  }

  await showList();
}

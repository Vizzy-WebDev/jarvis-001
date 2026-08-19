// The per-skill detail page — opened from a row in skills.js's list. Same
// pattern as _connector-detail.js: not a hash route, skills.js swaps its own
// container's content between its list view and this view internally (the
// "wipe and rebuild" pattern every screen uses, just at a finer grain).
// Visual layout matched directly against a real screenshot of Claude's own
// skill detail page (see CLAUDE.md's Skills section for the research this
// was built from): back link, name + owner + info popover, an enable toggle
// and a ⋮ action menu on the header row, a truncated description with "See
// more", then a preview/raw-source toggle over the instructions body.

import { fieldTextarea, postJson } from './_helpers.js';
import { openModal } from './_modal.js';
import { popover, segmented, toggleSwitch } from './_ui.js';
import { markdownBlock, copyableBlock } from './_markdown.js';

function formatDate(iso) {
  if (!iso) return 'Never';
  return new Date(iso).toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
}

/** The (i) popover next to the name — Last updated / Trigger / Description. "Trigger" is always "Automatic": Claude shows "Slash command + auto" here, but Jarvis's composer has no slash commands, so a Skill only ever triggers by the model matching its description. */
function openInfoPopover(anchor, skill) {
  popover({
    anchor,
    build(body) {
      const wrap = document.createElement('div');
      wrap.className = 'skill-info-popover';

      const row = document.createElement('div');
      row.className = 'skill-info-row';
      const col1 = document.createElement('div');
      col1.appendChild(Object.assign(document.createElement('p'), { className: 'hint', textContent: 'Last updated' }));
      col1.appendChild(Object.assign(document.createElement('p'), { textContent: formatDate(skill.updatedAt) }));
      const col2 = document.createElement('div');
      col2.appendChild(Object.assign(document.createElement('p'), { className: 'hint', textContent: 'Trigger' }));
      col2.appendChild(Object.assign(document.createElement('p'), { textContent: 'Automatic' }));
      row.append(col1, col2);
      wrap.appendChild(row);

      wrap.appendChild(Object.assign(document.createElement('p'), { className: 'hint', textContent: 'Description' }));
      wrap.appendChild(Object.assign(document.createElement('p'), { textContent: skill.description }));

      body.appendChild(wrap);
    },
  });
}

function buildDescriptionBlock(description) {
  const wrap = document.createElement('p');
  wrap.className = 'skill-detail-description';
  const short = description.length > 180 ? `${description.slice(0, 180)}…` : description;

  if (description.length <= 180) {
    wrap.textContent = description;
    return wrap;
  }

  const textSpan = document.createElement('span');
  textSpan.textContent = short;
  const more = document.createElement('button');
  more.type = 'button';
  more.className = 'link-btn';
  more.textContent = 'See more';
  let expanded = false;
  more.addEventListener('click', () => {
    expanded = !expanded;
    textSpan.textContent = expanded ? description : short;
    more.textContent = expanded ? 'See less' : 'See more';
  });
  wrap.append(textSpan, ' ', more);
  return wrap;
}

async function buildEditModal(skill, onChange) {
  const res = await fetch(`/api/skills/${encodeURIComponent(skill.name)}`);
  const data = await res.json();
  if (!data.ok) return;

  let descField, instructionsField;
  return openModal({
    title: `Edit "${skill.name}"`,
    size: 'wide',
    submitLabel: 'Save',
    busyLabel: 'Saving…',
    build(body) {
      descField = fieldTextarea('Description', '');
      descField.textarea.value = data.frontmatter?.description || skill.description;
      body.appendChild(descField.wrapper);
      instructionsField = fieldTextarea('Instructions', '');
      instructionsField.textarea.value = data.instructions;
      body.appendChild(instructionsField.wrapper);
    },
    async onSubmit(api) {
      if (!descField.textarea.value.trim()) {
        api.setError('Describe what this skill does and when to use it.');
        return null;
      }
      if (!instructionsField.textarea.value.trim()) {
        api.setError('Please describe what Jarvis should do.');
        return null;
      }
      const result = await postJson(
        `/api/skills/${encodeURIComponent(skill.name)}`,
        { description: descField.textarea.value.trim(), instructions: instructionsField.textarea.value.trim() },
        'PATCH'
      );
      if (!result.ok) {
        api.setError(result.error || 'Could not save those changes.');
        return null;
      }
      return result;
    },
  }).then((result) => {
    if (result) onChange();
  });
}

function startSkillChat(text) {
  window.dispatchEvent(new CustomEvent('jarvis:start-skill-chat', { detail: { text } }));
}

async function buildReplaceModal(skill, onChange) {
  let fileInput;
  return openModal({
    title: `Replace "${skill.name}"`,
    submitLabel: 'Replace',
    busyLabel: 'Replacing…',
    build(body) {
      body.appendChild(
        Object.assign(document.createElement('p'), {
          className: 'hint',
          textContent: "Choose a new .zip, .skill, or .md to replace this skill's entire content. Its name and on/off state stay the same.",
        })
      );
      const fileWrap = document.createElement('div');
      fileWrap.className = 'field';
      fileWrap.appendChild(Object.assign(document.createElement('label'), { textContent: 'A .zip, .skill, or .md file' }));
      fileInput = document.createElement('input');
      fileInput.type = 'file';
      fileInput.accept = '.zip,.skill,.md';
      fileWrap.appendChild(fileInput);
      body.appendChild(fileWrap);
    },
    async onSubmit(api) {
      const file = fileInput.files?.[0];
      if (!file) {
        api.setError('Choose a file first.');
        return null;
      }
      const res = await fetch(`/api/skills/${encodeURIComponent(skill.name)}/replace`, { method: 'POST', body: file });
      const data = await res.json();
      if (!data.ok) {
        api.setError(data.error || 'Could not replace that skill.');
        return null;
      }
      return data;
    },
  }).then((result) => {
    if (result) onChange();
  });
}

function downloadSkill(name) {
  const a = document.createElement('a');
  a.href = `/api/skills/${encodeURIComponent(name)}/download`;
  a.download = `${name}.zip`;
  document.body.appendChild(a);
  a.click();
  a.remove();
}

async function confirmUninstall(skill, onBack) {
  const result = await openModal({
    title: `Uninstall "${skill.name}"?`,
    submitLabel: 'Uninstall',
    onSubmit: async () => {
      await fetch(`/api/skills/${encodeURIComponent(skill.name)}`, { method: 'DELETE' });
      return true;
    },
  });
  if (result) onBack();
}

function openActionsMenu(anchor, skill, { onChange, onBack }) {
  const pop = popover({
    anchor,
    build(body) {
      const list = document.createElement('div');
      list.className = 'popover-list';
      const actions = [
        ['Try in chat', () => startSkillChat(`Let's try out my "${skill.name}" skill.`)],
        ['Edit', () => buildEditModal(skill, onChange)],
        ['Edit with Jarvis', () => startSkillChat(`Let's edit my "${skill.name}" skill together. Here's what it currently does: ${skill.description}`)],
        ['Replace', () => buildReplaceModal(skill, onChange)],
        ['Download', () => downloadSkill(skill.name)],
        ['Uninstall', () => confirmUninstall(skill, onBack)],
      ];
      for (const [label, action] of actions) {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = label === 'Uninstall' ? 'popover-option popover-option-danger' : 'popover-option';
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

export async function render(container, { name, onBack }) {
  container.innerHTML = '';
  const res = await fetch(`/api/skills/${encodeURIComponent(name)}`);
  const data = await res.json();
  if (!data.ok) {
    container.appendChild(Object.assign(document.createElement('p'), { className: 'error', textContent: data.error || 'Could not load that skill.' }));
    return;
  }
  const skill = data.skill;
  const refresh = () => render(container, { name, onBack });

  const header = document.createElement('div');
  header.className = 'detail-header';
  const backBtn = document.createElement('button');
  backBtn.type = 'button';
  backBtn.className = 'btn';
  backBtn.textContent = '← Skills';
  backBtn.addEventListener('click', onBack);
  header.appendChild(backBtn);
  container.appendChild(header);

  const titleRow = document.createElement('div');
  titleRow.className = 'detail-title-row';
  titleRow.appendChild(Object.assign(document.createElement('h2'), { textContent: skill.name }));
  const infoBtn = document.createElement('button');
  infoBtn.type = 'button';
  infoBtn.className = 'icon-btn';
  infoBtn.textContent = 'ⓘ';
  infoBtn.addEventListener('click', () => openInfoPopover(infoBtn, skill));
  titleRow.appendChild(infoBtn);

  const toggle = toggleSwitch({
    value: skill.enabled,
    onChange: async (next) => {
      await postJson(`/api/skills/${encodeURIComponent(skill.name)}`, { enabled: next }, 'PATCH');
    },
  });
  toggle.wrapper.classList.add('detail-action-btn');
  titleRow.appendChild(toggle.wrapper);

  const menuBtn = document.createElement('button');
  menuBtn.type = 'button';
  menuBtn.className = 'btn';
  menuBtn.textContent = '⋮';
  menuBtn.addEventListener('click', () => openActionsMenu(menuBtn, skill, { onChange: refresh, onBack }));
  titleRow.appendChild(menuBtn);
  container.appendChild(titleRow);

  container.appendChild(Object.assign(document.createElement('p'), { className: 'hint', textContent: 'by You' }));

  container.appendChild(buildDescriptionBlock(skill.description || ''));

  // Preview / raw-source toggle over the instructions body — mirrors the
  // eye / </> icons in Claude's own detail page.
  const bodyWrap = document.createElement('div');
  bodyWrap.className = 'section-card';

  const toggleRow = document.createElement('div');
  toggleRow.className = 'skill-detail-view-toggle';
  const viewToggle = segmented(
    [
      ['preview', '👁'],
      ['code', '</>'],
    ],
    {
      value: 'preview',
      onChange: (v) => {
        contentWrap.innerHTML = '';
        contentWrap.appendChild(v === 'preview' ? markdownBlock(data.instructions) : copyableBlock(data.raw || data.instructions));
      },
    }
  );
  toggleRow.appendChild(viewToggle.wrapper);
  bodyWrap.appendChild(toggleRow);

  const contentWrap = document.createElement('div');
  contentWrap.appendChild(markdownBlock(data.instructions));
  bodyWrap.appendChild(contentWrap);

  container.appendChild(bodyWrap);
}

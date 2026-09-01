// Morning Briefing screen — which sections are included, a custom note, a
// real connector picker, and a live preview button.
//
// Weather and headlines have NO controls here at all, on purpose — they're
// fixed, always-available native abilities (server/tools/get_weather.js,
// get_headlines.js), not something the user "adds." This screen used to
// have a "Live info sources" card — an "Add a source" picker that listed
// weather/headlines as if they were Skills to attach — which was a real
// instance of the native-ability-as-Skill bug (see CLAUDE.md's permanent
// Skills rule). Nothing was removed from what Jarvis can DO: the briefing
// still includes weather whenever a city has been set, and headlines
// whenever they've been turned on — both set conversationally via the
// configure_briefing skill ("set my weather to Lagos", "turn on headlines"),
// matching CLAUDE.md's existing "Desktop control / Browser / Files have no
// settings screen" precedent for a built-in ability with no UI.
//
// Connectors ARE a real, user-driven picker here (replacing a permanently-
// disabled Calendar/Email placeholder that predated any real connector
// system existing) — see server/scheduler/briefing-config.js's `connectors`
// field. Populated from the same connectors the App Control screen shows,
// filtered to ones actually usable right now (usableConnectors() in
// _helpers.js — the same definition server/prompt.js's connectorsSection()
// uses, so this picker never offers something the model itself wouldn't
// recognize as connected). Nothing is pre-selected — the briefing includes
// a connector only once explicitly ticked here.

import { sectionCard, fieldTextarea, postJson, usableConnectors } from './_helpers.js';
import { connectorPickerButton } from './_connector-picker.js';
import { markdownBlock } from './_markdown.js';
import { navigate } from '../router.js';

const SECTION_LABELS = [
  ['greeting', 'Greeting'],
  ['dateTime', "Today's date and time"],
  ['tasks', 'Upcoming scheduled items'],
  ['goals', 'Your goals and notes'],
  ['focus', 'Suggested focus for the day'],
  ['custom', 'Custom note'],
];

function buildSectionsCard(config, onSave) {
  const card = sectionCard('What to include');
  for (const [key, label] of SECTION_LABELS) {
    const row = document.createElement('label');
    row.className = 'settings-row';
    row.appendChild(Object.assign(document.createElement('span'), { textContent: label }));
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.checked = Boolean(config.sections[key]);
    cb.addEventListener('change', () => onSave({ sections: { [key]: cb.checked } }));
    row.appendChild(cb);
    card.appendChild(row);

    // "Your goals and notes" pulls straight from Profile & Goals — link
    // through so it's obvious what this checkbox will actually include,
    // rather than leaving it a mystery until the next preview.
    if (key === 'goals') {
      const link = document.createElement('button');
      link.type = 'button';
      link.className = 'btn';
      link.textContent = 'View your notes →';
      link.addEventListener('click', () => navigate('profile'));
      card.appendChild(link);
    }
  }
  return card;
}

function buildCustomCard(config, onSave) {
  const card = sectionCard('Custom note');
  const textField = fieldTextarea('Included when "Custom note" is on', 'Anything you always want mentioned…');
  textField.textarea.value = config.customText || '';
  card.appendChild(textField.wrapper);

  const saveBtn = document.createElement('button');
  saveBtn.type = 'button';
  saveBtn.className = 'btn';
  saveBtn.textContent = 'Save';
  saveBtn.addEventListener('click', () => onSave({ customText: textField.textarea.value }));
  card.appendChild(saveBtn);
  return card;
}

/**
 * Real connector picker — nothing pre-selected, only what's ticked here
 * gets used (config.connectors, an array of connector ids). `usable` is
 * already filtered to connectors that are genuinely connected and have at
 * least one real tool available (see usableConnectors() in _helpers.js).
 * Uses the SAME popover-button picker as the Schedule/Task screen
 * (_connector-picker.js) — this used to be its own separately-built
 * always-visible checklist card; the user asked for the two to share one
 * structure instead of looking and behaving differently.
 *
 * No "Connect another app in App Control" shortcut here on purpose (removed
 * per explicit request) — App Control is still reachable from the main nav
 * drawer as always, this screen just doesn't duplicate a link to it any more.
 */
function buildConnectorsCard(config, usable, onSave) {
  const card = sectionCard('Connected apps for this briefing');
  card.appendChild(
    Object.assign(document.createElement('p'), {
      className: 'hint',
      textContent: usable.length
        ? 'Nothing is included automatically — pick exactly which of your connected apps this briefing may use.'
        : "You don't have any connected apps usable here yet.",
    })
  );
  const selected = new Set(config.connectors || []);
  const picker = connectorPickerButton({
    usable,
    selected,
    label: 'Connectors',
    onChange: () => onSave({ connectors: [...selected] }),
  });
  card.appendChild(picker);
  return card;
}

function buildPreviewCard() {
  const card = sectionCard('Preview');
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'btn btn-primary';
  btn.textContent = 'Preview my briefing now';
  const resultWrap = document.createElement('div');

  btn.addEventListener('click', async () => {
    btn.disabled = true;
    btn.textContent = 'Putting it together…';
    resultWrap.innerHTML = '';
    try {
      const res = await fetch('/api/briefing/preview', { method: 'POST' });
      const data = await res.json();
      if (data.text) {
        resultWrap.appendChild(markdownBlock(data.text));
      } else {
        resultWrap.appendChild(
          Object.assign(document.createElement('p'), { className: 'error', textContent: 'Could not generate a preview.' })
        );
      }
    } catch {
      resultWrap.appendChild(
        Object.assign(document.createElement('p'), { className: 'error', textContent: 'Could not reach the Jarvis server.' })
      );
    } finally {
      btn.disabled = false;
      btn.textContent = 'Preview my briefing now';
    }
  });

  card.append(btn, resultWrap);
  return card;
}

export async function render(container) {
  container.innerHTML = '';
  const [configRes, connectorsRes] = await Promise.all([fetch('/api/briefing'), fetch('/api/connectors')]);
  const config = await configRes.json();
  const { connectors } = await connectorsRes.json();

  const onSave = (patch) => postJson('/api/briefing', patch);

  container.appendChild(buildSectionsCard(config, onSave));
  container.appendChild(buildCustomCard(config, onSave));
  container.appendChild(buildConnectorsCard(config, usableConnectors(connectors), onSave));
  container.appendChild(buildPreviewCard());
}

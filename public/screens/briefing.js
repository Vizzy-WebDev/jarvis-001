// Morning Briefing screen — which sections are included, a custom note, and
// a live preview button. Calendar/email are shown as reserved, disabled
// slots — they need their own Google sign-in, a separate, bigger step that
// hasn't been built. See server/scheduler/briefing-config.js's DEFAULT_CONFIG
// comment for why those slots exist in the saved config already.
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

import { sectionCard, fieldTextarea, postJson } from './_helpers.js';
import { toggleSwitch } from './_ui.js';
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
 * Was a plain static badge with no state at all — now a real (disabled)
 * toggleSwitch reflecting the config slot that's already saved
 * (briefing-config.js's `calendar.enabled`/`email.enabled`), so the only
 * change needed once the connector is actually built is dropping `disabled`.
 * Not faking the feature — clicking has no effect and says so.
 */
function buildComingSoonCard(config, onSave) {
  const card = sectionCard('Coming later');
  card.appendChild(
    Object.assign(document.createElement('p'), {
      className: 'hint',
      textContent:
        'Calendar and email sections are reserved for a future update — they need their own Google ' +
        'sign-in, which is a separate, bigger step than this upgrade.',
    })
  );
  for (const [key, label] of [
    ['calendar', 'Calendar events'],
    ['email', 'Unread email summary'],
  ]) {
    const row = document.createElement('div');
    row.className = 'settings-row';
    row.appendChild(Object.assign(document.createElement('span'), { textContent: label }));
    const toggle = toggleSwitch({ value: Boolean(config[key]?.enabled), disabled: true });
    row.appendChild(toggle.wrapper);
    card.appendChild(row);
  }
  const setupBtn = document.createElement('button');
  setupBtn.type = 'button';
  setupBtn.className = 'btn';
  setupBtn.textContent = 'Set this up in App Control';
  setupBtn.addEventListener('click', () => navigate('app-control'));
  card.appendChild(setupBtn);
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
  const configRes = await fetch('/api/briefing');
  const config = await configRes.json();

  const onSave = (patch) => postJson('/api/briefing', patch);

  container.appendChild(buildSectionsCard(config, onSave));
  container.appendChild(buildCustomCard(config, onSave));
  container.appendChild(buildComingSoonCard(config, onSave));
  container.appendChild(buildPreviewCard());
}

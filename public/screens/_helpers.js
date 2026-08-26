// Small DOM-building helpers shared by every screen under public/screens/.
// Plain createElement/textContent throughout — never innerHTML with
// server- or user-supplied text — since screen content (model labels, task
// titles, error messages, ...) isn't trusted.

export function fieldInput(labelText, type, placeholder) {
  const wrapper = document.createElement('div');
  wrapper.className = 'field';
  const label = document.createElement('label');
  label.textContent = labelText;
  const input = document.createElement('input');
  input.type = type;
  if (placeholder) input.placeholder = placeholder;
  // Chrome ignores autocomplete="off" specifically on type="password"
  // fields (and often the plain-text field right before one, treating the
  // pair as a login form) — it'll offer/autofill a saved credential
  // regardless, confirmed live on the OAuth guided-setup Client ID/Secret
  // fields (a saved login's username appeared in Client ID unprompted).
  // "new-password" is the one value Chrome actually respects here, since
  // it signals "entering a new secret," not "logging into a saved
  // account." Every one of this app's password-type fields (OAuth Client
  // Secret, a custom API connector's key, a model connection's key) goes
  // through this one shared function, so the fix belongs here, once — not
  // three separate patches, and it covers any future password field too.
  input.autocomplete = type === 'password' ? 'new-password' : 'off';
  input.spellcheck = false;
  wrapper.append(label, input);
  return { wrapper, input };
}

export function fieldTextarea(labelText, placeholder) {
  const wrapper = document.createElement('div');
  wrapper.className = 'field';
  const label = document.createElement('label');
  label.textContent = labelText;
  const textarea = document.createElement('textarea');
  if (placeholder) textarea.placeholder = placeholder;
  wrapper.append(label, textarea);
  return { wrapper, textarea };
}

export function fieldSelect(labelText, options) {
  const wrapper = document.createElement('div');
  wrapper.className = 'field';
  const label = document.createElement('label');
  label.textContent = labelText;
  const select = document.createElement('select');
  for (const [value, text] of options) {
    select.appendChild(Object.assign(document.createElement('option'), { value, textContent: text }));
  }
  wrapper.append(label, select);
  return { wrapper, select };
}

export function sectionCard(titleText) {
  const card = document.createElement('div');
  card.className = 'section-card';
  card.appendChild(Object.assign(document.createElement('h2'), { textContent: titleText }));
  return card;
}

/** A destructive-action button that requires a second click within 3s to actually fire — no blocking confirm() dialog. */
export function armedButton(label, armedLabel, onConfirm, className = 'btn btn-danger') {
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = className;
  btn.textContent = label;
  let armed = false;
  let timer = null;
  btn.addEventListener('click', (e) => {
    // Found live: this button is sometimes used inside a larger clickable
    // row (e.g. notifications.js's rows) — without this, both the arming
    // click AND the confirming click also fire whatever click handler the
    // row itself has, which for a row that opens a detail view on click
    // means arming this button destroys the button (mid-arm, via a
    // container re-render) before the second click can ever land.
    e.stopPropagation();
    if (!armed) {
      armed = true;
      btn.textContent = armedLabel;
      timer = setTimeout(() => {
        armed = false;
        btn.textContent = label;
      }, 3000);
      return;
    }
    clearTimeout(timer);
    armed = false;
    btn.textContent = label;
    onConfirm();
  });
  return btn;
}

export async function postJson(url, body, method = 'POST') {
  const res = await fetch(url, { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  return res.json();
}

/** Same shape as postJson above, but rejects on a non-2xx response instead of silently resolving with whatever error JSON the server sent — for any control (a toggle, a permission switch) that needs to know a save actually failed so it can revert its own already-applied UI change, not just log it. Promoted out of _connector-detail.js, which was the first caller. */
export async function patchJsonStrict(url, body) {
  const res = await fetch(url, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  if (!res.ok) throw new Error(`Save failed (${res.status})`);
  return res.json();
}

/** A brief red line appended under `card`, auto-removed after a few seconds — used when a save fails so the user isn't left staring at a control that silently didn't take. */
export function flashSaveError(card, message) {
  const el = document.createElement('p');
  el.className = 'hint save-error';
  el.textContent = message;
  card.appendChild(el);
  setTimeout(() => el.remove(), 4000);
}

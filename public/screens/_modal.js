// The app's one modal dialog component — used by every screen that needs a
// popup instead of a permanently-inline form (add a model, create a task,
// add a briefing source, ...).
//
// Appended to <body>, NEVER to a screen's own container: every screen's
// render() starts with `container.innerHTML = ''` on refresh (see
// public/router.js's rebuild-from-scratch pattern) — a modal living inside
// one would be silently destroyed mid-use by any re-render the modal itself
// didn't cause. Callers must only refresh the screen AFTER this promise
// resolves, never from inside onSubmit while the dialog is still mounted.

let current = null; // the currently-open modal's internal handle, if any

/**
 * Opens the dialog. Returns a Promise that resolves with whatever
 * `onSubmit` returned, or `undefined` if the user cancelled (Cancel / × /
 * Esc / clicking the scrim).
 *
 * `build(body, api)` fills the dialog body — called once, synchronously,
 * before the dialog is shown. `onSubmit(api)` runs on the primary button;
 * returning a falsy value (or throwing) keeps the dialog open with
 * `setBusy(false)` restored — use `api.setError(...)` to explain why.
 * Returning anything else closes the dialog and resolves with that value.
 *
 * `api` (handed to both `build` and `onSubmit`):
 *   { body, setSubmitEnabled(bool), setSubmitLabel(str), setError(msgOrNull), setBusy(bool), close(result) }
 *
 * `size: 'default' | 'wide'` (default `'default'`) — pass `'wide'` for
 * forms that need more horizontal room (e.g. a toolbar of shared
 * primitives from _ui.js). Adds the `modal-wide` class alongside the
 * existing `modal-dialog` class; everything else about the dialog is
 * unchanged.
 */
export function openModal({ title, build, submitLabel = 'Save', cancelLabel = 'Cancel', onSubmit, busyLabel = 'Working…', size = 'default' }) {
  return new Promise((resolve) => {
    // Only one modal at a time — opening a second closes the first as a cancel.
    if (current) current.cancel();

    let settled = false;
    const previouslyFocused = document.activeElement;

    const scrim = document.createElement('div');
    scrim.className = 'modal-scrim';

    const dialog = document.createElement('div');
    dialog.className = size === 'wide' ? 'modal-dialog modal-wide' : 'modal-dialog';
    dialog.setAttribute('role', 'dialog');
    dialog.setAttribute('aria-modal', 'true');

    const header = document.createElement('div');
    header.className = 'modal-header';
    header.appendChild(Object.assign(document.createElement('h2'), { className: 'modal-title', textContent: title }));
    const closeBtn = document.createElement('button');
    closeBtn.type = 'button';
    closeBtn.className = 'modal-close';
    closeBtn.setAttribute('aria-label', 'Close');
    closeBtn.textContent = '×';
    header.appendChild(closeBtn);

    const body = document.createElement('div');
    body.className = 'modal-body';

    const errorEl = document.createElement('p');
    errorEl.className = 'error hidden';

    const footer = document.createElement('div');
    footer.className = 'modal-footer';
    const cancelBtn = document.createElement('button');
    cancelBtn.type = 'button';
    cancelBtn.className = 'btn';
    cancelBtn.textContent = cancelLabel;
    let currentLabel = submitLabel;
    const submitBtn = document.createElement('button');
    submitBtn.type = 'button';
    submitBtn.className = 'btn btn-primary';
    submitBtn.textContent = currentLabel;
    footer.append(cancelBtn, submitBtn);

    dialog.append(header, body, errorEl, footer);
    scrim.appendChild(dialog);
    document.body.appendChild(scrim);
    document.body.classList.add('modal-open');

    function finish(result) {
      if (settled) return;
      settled = true;
      document.removeEventListener('keydown', onKeydown, true);
      scrim.remove();
      document.body.classList.remove('modal-open');
      if (current === handle) current = null;
      if (previouslyFocused instanceof HTMLElement) previouslyFocused.focus();
      resolve(result);
    }

    function cancel() {
      finish(undefined);
    }

    function onKeydown(e) {
      if (e.key === 'Escape') {
        e.stopPropagation(); // don't also let app.js's drawer-close handler react
        cancel();
      }
    }
    document.addEventListener('keydown', onKeydown, true);

    scrim.addEventListener('click', (e) => {
      if (e.target === scrim) cancel();
    });
    closeBtn.addEventListener('click', cancel);
    cancelBtn.addEventListener('click', cancel);

    const api = {
      body,
      setSubmitEnabled(enabled) {
        submitBtn.disabled = !enabled;
      },
      setSubmitLabel(text) {
        // currentLabel, not just the button text — setBusy(false) below
        // restores to THIS, not the dialog's original submitLabel, or a
        // relabel (e.g. "Test and add" -> "Add selected" after discovery
        // reveals a checklist) would be silently undone the moment the
        // busy spinner clears.
        currentLabel = text;
        submitBtn.textContent = text;
      },
      setError(message) {
        if (message) {
          errorEl.textContent = message;
          errorEl.classList.remove('hidden');
        } else {
          errorEl.classList.add('hidden');
        }
      },
      setBusy(busy) {
        submitBtn.disabled = busy;
        cancelBtn.disabled = busy;
        submitBtn.textContent = busy ? busyLabel : currentLabel;
      },
      close(result) {
        finish(result);
      },
    };

    submitBtn.addEventListener('click', async () => {
      api.setError(null);
      api.setBusy(true);
      try {
        const result = onSubmit ? await onSubmit(api) : true;
        if (result) {
          finish(result);
        } else {
          api.setBusy(false);
        }
      } catch (err) {
        api.setBusy(false);
        api.setError(err?.message || 'Something went wrong.');
      }
    });

    build?.(body, api);

    const focusable = dialog.querySelector('input, select, textarea, button');
    focusable?.focus();

    const handle = { cancel };
    current = handle;
  });
}

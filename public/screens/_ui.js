// Small library of shared interactive UI components — toolbar buttons,
// segmented pickers, popovers, icon tiles — for screens that need more than
// _helpers.js's plain form fields (e.g. the Model Settings / Scheduled
// Tasks toolbars). Same conventions as the rest of public/screens/: plain
// createElement/textContent, never innerHTML with dynamic text, every
// export returns plain DOM elements or a small `{wrapper, ...}` handle
// mirroring fieldInput/fieldSelect in _helpers.js.

/**
 * A small square-ish tile showing either a plain glyph (emoji/unicode
 * symbol, e.g. `iconTile('⚡')` — tasks.js's own icon, unchanged) or a real
 * icon built by `_connector-icons.js`'s `iconFor()`, which returns
 * `{content, bg}`: `content` is an SVG element or a fallback glyph string,
 * `bg` (optional) tints the tile itself to that service's real brand color
 * — the same "colored badge + white glyph" look real connector lists use,
 * rather than every tile sharing one neutral panel color.
 */
export function iconTile(glyphOrIcon) {
  const el = document.createElement('div');
  el.className = 'icon-tile';
  const isIconObject = glyphOrIcon && typeof glyphOrIcon === 'object' && 'content' in glyphOrIcon;
  const content = isIconObject ? glyphOrIcon.content : glyphOrIcon;
  if (isIconObject && glyphOrIcon.bg) el.style.background = glyphOrIcon.bg;
  if (content instanceof Node) el.appendChild(content);
  else el.textContent = content;
  return el;
}

/**
 * A row of button-like segments, one active at a time — for small
 * enum-style pickers inside a modal (e.g. a view-mode switch).
 *
 * `options` is `[[value, label], ...]`, same shape fieldSelect takes.
 * Returns `{wrapper, setValue(v), getValue()}`.
 */
export function segmented(options, { value, onChange } = {}) {
  const wrapper = document.createElement('div');
  wrapper.className = 'segmented';

  let current = value;
  const buttons = new Map();

  for (const [optValue, label] of options) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'segmented-item';
    btn.textContent = label;
    btn.dataset.value = optValue;
    btn.addEventListener('click', () => {
      if (current === optValue) return;
      current = optValue;
      applyActive();
      onChange?.(current);
    });
    buttons.set(optValue, btn);
    wrapper.appendChild(btn);
  }

  function applyActive() {
    for (const [optValue, btn] of buttons) {
      btn.classList.toggle('active', optValue === current);
    }
  }
  applyActive();

  return {
    wrapper,
    setValue(v) {
      current = v;
      applyActive();
    },
    getValue() {
      return current;
    },
  };
}

/**
 * A plain two-state on/off switch — track + sliding knob, no text label of
 * its own (the caller supplies one alongside, same as any other row).
 * Deliberately separate from `permissionControl`-shaped things: this is for
 * a genuinely binary state (a skill/task/briefing-source/model is on or
 * off), not a three-way standing-permission choice, so it stays a real
 * switch rather than the three-icon Always-allow/Ask/Deny control built for
 * connector tool permissions in _connector-detail.js. Introduced to replace
 * the "Turn on/Turn off" text buttons those screens used before, at the
 * user's explicit request to make on/off controls consistent app-wide.
 * Returns `{wrapper, setValue(bool)}`.
 */
export function toggleSwitch({ value = false, onChange, disabled = false } = {}) {
  const wrapper = document.createElement('button');
  wrapper.type = 'button';
  wrapper.className = 'toggle-switch';
  wrapper.setAttribute('role', 'switch');
  wrapper.disabled = disabled;

  const knob = document.createElement('span');
  knob.className = 'toggle-switch-knob';
  wrapper.appendChild(knob);

  let current = Boolean(value);
  function apply() {
    wrapper.classList.toggle('on', current);
    wrapper.setAttribute('aria-checked', String(current));
  }
  apply();

  wrapper.addEventListener('click', () => {
    if (wrapper.disabled) return;
    current = !current;
    apply();
    onChange?.(current);
  });

  return {
    wrapper,
    setValue(v) {
      current = Boolean(v);
      apply();
    },
  };
}

/**
 * A compact toolbar button with a trailing count badge — the "Connectors 3"
 * / "Skills 4" style button. Returns `{el, setCount(n)}`.
 */
export function counterButton(label, count) {
  const el = document.createElement('button');
  el.type = 'button';
  el.className = 'counter-btn';
  const labelEl = document.createTextNode(label + ' ');
  const badge = document.createElement('span');
  badge.className = 'badge';
  badge.textContent = String(count);
  el.append(labelEl, badge);
  return {
    el,
    setCount(n) {
      badge.textContent = String(n);
    },
  };
}

// Only one popover open at a time, same "opening a new one closes the
// previous" pattern _modal.js uses for its own `current` handle.
let openPopover = null;

/**
 * A small floating panel anchored below (or, if it would overflow the
 * right edge, right-aligned to) `anchor`. Lives at document.body level —
 * NOT inside a modal's .modal-body/.modal-dialog, both of which clip
 * overflow, so a popover nested there would be clipped or scroll away
 * from its anchor.
 *
 * `build(body, close)` is called once synchronously to fill the popover —
 * same calling convention as _modal.js's `build(body, api)`, with `close`
 * standing in for the api: a row whose click action navigates away (rather
 * than just changing something in place) should call it explicitly, since
 * navigating doesn't itself count as an "outside click" of a popover whose
 * own content triggered it. Most callers only take `(body)` and never
 * reference the second argument, which is fine — it's additive.
 *
 * Closes on outside click, Escape, or a caller-invoked `close()`. Calls
 * `onClose?.()` on any close path. Returns `{close}`.
 */
export function popover({ anchor, build, onClose }) {
  if (openPopover) openPopover.close();

  const el = document.createElement('div');
  el.className = 'popover';

  const rect = anchor.getBoundingClientRect();
  el.style.top = `${rect.bottom + 6}px`;
  const overflowsRight = rect.left + 320 > window.innerWidth;
  if (overflowsRight) {
    el.style.right = `${window.innerWidth - rect.right}px`;
  } else {
    el.style.left = `${rect.left}px`;
  }

  document.body.appendChild(el);

  let closed = false;
  function close() {
    if (closed) return;
    closed = true;
    document.removeEventListener('mousedown', onMousedown);
    document.removeEventListener('keydown', onKeydown, true);
    el.remove();
    if (openPopover === handle) openPopover = null;
    onClose?.();
  }

  function onMousedown(e) {
    if (!el.contains(e.target) && e.target !== anchor) close();
  }

  function onKeydown(e) {
    if (e.key === 'Escape') {
      e.stopPropagation(); // close only this popover, not a parent modal
      close();
    }
  }

  document.addEventListener('mousedown', onMousedown);
  document.addEventListener('keydown', onKeydown, true);

  build?.(el, close);

  const handle = { close };
  openPopover = handle;
  return handle;
}

/**
 * A dropdown whose own button reflects the current selection — "✓ Always
 * allow ⌄" — instead of a static label, opening a panel that marks the
 * active choice with a checkmark. Built for the connector permission
 * group control (_connector-detail.js) against a user-supplied Claude
 * Desktop reference screenshot, but generic: `options` is
 * `[{value, label, glyph?}, ...]`. `value`/`setValue()` intentionally
 * don't have to be one of `options`' own values — the group control's
 * "current value" is really a computed aggregate ('custom' when the
 * group's tools don't all agree), not a plain tracked field, so the
 * caller may pass something not in the list; `activeOption()` falls back
 * to the last option (this control's own callers always list "Custom"
 * last) rather than silently rendering blank.
 *
 * NOTE: doesn't rely on popover()'s own `build(body, close)` closing
 * capability (that second parameter isn't actually passed at the call
 * site yet as of this writing — `build?.(el)` — so a future caller
 * shouldn't assume it works without checking); this captures its own
 * handle from popover()'s return value instead, which the popover's own
 * doc comment already describes as the current safe way to close it.
 *
 * Returns `{wrapper, setValue(v)}`.
 */
export function dropdownControl(options, { value, onChange } = {}) {
  const wrapper = document.createElement('button');
  wrapper.type = 'button';
  wrapper.className = 'counter-btn dropdown-control';

  let current = value;
  function activeOption() {
    return options.find((o) => o.value === current) || options[options.length - 1];
  }
  function render() {
    const opt = activeOption();
    wrapper.textContent = '';
    if (opt.glyph) wrapper.appendChild(Object.assign(document.createElement('span'), { className: 'dropdown-control-glyph', textContent: opt.glyph }));
    wrapper.appendChild(document.createTextNode(`${opt.label} ⌄`));
  }
  render();

  wrapper.addEventListener('click', () => {
    const popCtl = popover({
      anchor: wrapper,
      build(body) {
        const list = document.createElement('div');
        list.className = 'popover-list';
        for (const opt of options) {
          const row = document.createElement('button');
          row.type = 'button';
          row.className = 'popover-option dropdown-option';
          const left = document.createElement('span');
          left.className = 'dropdown-option-label';
          if (opt.glyph) left.appendChild(Object.assign(document.createElement('span'), { textContent: opt.glyph }));
          left.appendChild(document.createTextNode(opt.label));
          row.appendChild(left);
          if (opt.value === current) {
            row.appendChild(Object.assign(document.createElement('span'), { className: 'dropdown-option-check', textContent: '✓' }));
          }
          row.addEventListener('click', () => {
            popCtl.close();
            if (opt.value === current) return; // re-picking the already-active option is a no-op
            current = opt.value;
            render();
            onChange?.(opt.value);
          });
          list.appendChild(row);
        }
        body.appendChild(list);
      },
    });
  });

  return {
    wrapper,
    setValue(v) {
      current = v;
      render();
    },
  };
}

/**
 * The unattended-run consent notice box, factored out of tasks.js so its
 * upcoming rewrite (and any other caller) doesn't have to hand-roll the
 * same DOM structure again. CSS (`.notice`/`.notice-check`) already exists
 * in style.css — this only builds the DOM.
 *
 * Returns `{wrapper, textEl, checkbox}`.
 */
export function noticeBox() {
  const wrapper = document.createElement('div');
  wrapper.className = 'notice';

  const textEl = document.createElement('span');
  textEl.className = 'notice-text';
  wrapper.appendChild(textEl);

  const label = document.createElement('label');
  label.className = 'notice-check';
  const checkbox = document.createElement('input');
  checkbox.type = 'checkbox';
  label.append(checkbox, document.createTextNode('I understand'));
  wrapper.appendChild(label);

  return { wrapper, textEl, checkbox };
}

/**
 * "Which model should handle this?" — a small toolbar button that opens a
 * popover listing "Auto (best fit)" plus every enabled model, grouped by the
 * connection it came from. `''` means Auto.
 *
 * `require` (optional) narrows the list to models with a given capability,
 * e.g. `{video: true}` for a job that genuinely needs one — a model that
 * can't do it is shown greyed and unselectable rather than hidden, so it's
 * obvious *why* a model isn't available rather than it silently vanishing.
 *
 * The checkmark means "known to be working right now"
 * (`availability.state === 'working'`), NOT `ready` — `ready` only means a
 * key is configured, which every listed model passes anyway. Getting that
 * wrong once put a tick beside every model regardless of real state.
 *
 * Returns `{el, getValue(), setValue(id)}`.
 *
 * (Scheduled Tasks has its own near-identical copy predating this one; it
 * still works and wasn't touched here. Worth collapsing into this when
 * tasks.js is next edited.)
 */
export function modelPicker(models, connections, { require: required = null, initial = '' } = {}) {
  let selected = initial;

  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'counter-btn';

  function labelFor(id) {
    if (!id) return 'Auto';
    return models.find((m) => m.id === id)?.label || 'Auto';
  }
  btn.textContent = labelFor(selected);

  function canDo(model) {
    if (!required) return true;
    return Object.entries(required).every(([cap, wanted]) => !wanted || model.caps?.[cap]);
  }

  btn.addEventListener('click', () => {
    const pop = popover({
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

        const addOption = (model) => {
          const opt = document.createElement('button');
          opt.type = 'button';
          opt.className = 'popover-option';
          const working = model.availability?.state === 'working';
          const usable = canDo(model);
          opt.textContent = model.label + (working ? ' ✓' : '') + (usable ? '' : " — can't do this");
          opt.disabled = !usable;
          opt.addEventListener('click', () => {
            selected = model.id;
            btn.textContent = model.label;
            pop.close();
          });
          list.appendChild(opt);
        };

        const byConnection = new Map();
        for (const m of models) {
          const key = m.connectionId || '';
          if (!byConnection.has(key)) byConnection.set(key, []);
          byConnection.get(key).push(m);
        }

        const grouped = new Set();
        for (const conn of connections || []) {
          const group = byConnection.get(conn.id);
          if (!group?.length) continue;
          grouped.add(conn.id);
          list.appendChild(
            Object.assign(document.createElement('div'), { className: 'popover-group-label', textContent: conn.label })
          );
          group.forEach(addOption);
        }
        // Anything whose connection wasn't found (an orphaned model, or a
        // failed connections fetch) is listed flat rather than hidden.
        for (const m of models) {
          if (!grouped.has(m.connectionId || '')) addOption(m);
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

// Shared connector picker — one popover-button component reused everywhere
// a screen lets the user tick which of their already-connected apps a
// feature may use (tasks.js's per-task connector picker, briefing.js's
// per-briefing one). Built once for the Schedule/Task screen; Morning
// Briefing reuses this SAME structure rather than the separate always-
// visible checklist card it had grown on its own.
//
// Always shows each connector's real logo (iconForConnector() — the same
// resolution _connector-detail.js and app-control.js already use) next to
// its name, with a real toggleSwitch() per row rather than a plain
// checkbox. The popover itself only ever shows the first INLINE_CAP
// connectors — found live that an unbounded list overflows past the
// bottom of the screen once there are more than a handful of usable
// connectors — with a "View all" row that opens openConnectorOverlay()
// for the complete list.

import { iconTile, popover, toggleSwitch } from './_ui.js';
import { iconForConnector } from './_connector-icons.js';

const INLINE_CAP = 5;

/** One connector row — logo + name + a real on/off switch. Shared by both the capped popover list and the "View all" overlay so there's exactly one row implementation. */
function buildConnectorRow(connector, selected, onChange) {
  const row = document.createElement('label');
  row.className = 'settings-row';
  const left = document.createElement('span');
  left.className = 'popover-option-content';
  left.appendChild(iconTile(iconForConnector({ iconDataUri: connector.iconDataUri })));
  left.appendChild(Object.assign(document.createElement('span'), { textContent: connector.label }));
  row.appendChild(left);

  const toggle = toggleSwitch({
    value: selected.has(connector.id),
    onChange: (v) => {
      if (v) selected.add(connector.id);
      else selected.delete(connector.id);
      onChange?.();
    },
  });
  row.appendChild(toggle.wrapper);
  return row;
}

/**
 * A small, self-contained overlay listing EVERY usable connector — deliberately
 * NOT built on _modal.js's openModal(). openModal() allows only one modal at a
 * time and cancels whichever is already open the moment a second one opens;
 * this picker is itself used INSIDE the Task screen's "Create a task" modal, so
 * going through openModal() here would silently cancel that in-progress form
 * the moment "View all" was clicked. Reuses the same .modal-scrim/.modal-dialog/
 * .modal-header/.modal-body/.modal-close classes for a consistent look, at a
 * higher z-index than a real modal (see .connector-overlay-scrim in style.css) —
 * the same trick .popover already relies on to render above an open modal.
 * Every toggle here applies immediately via the shared `selected` Set, exactly
 * like the popover — there's nothing to "submit", just a single close action.
 */
function openConnectorOverlay({ usable, selected, onChange, title = 'All connectors' }) {
  const scrim = document.createElement('div');
  scrim.className = 'modal-scrim connector-overlay-scrim';

  const dialog = document.createElement('div');
  dialog.className = 'modal-dialog';
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
  body.className = 'modal-body connector-overlay-body';
  for (const connector of usable) {
    body.appendChild(buildConnectorRow(connector, selected, onChange));
  }

  dialog.append(header, body);
  scrim.appendChild(dialog);
  document.body.appendChild(scrim);

  function close() {
    document.removeEventListener('keydown', onKeydown, true);
    scrim.remove();
  }
  function onKeydown(e) {
    if (e.key === 'Escape') {
      e.stopPropagation();
      close();
    }
  }
  document.addEventListener('keydown', onKeydown, true);
  scrim.addEventListener('click', (e) => {
    if (e.target === scrim) close();
  });
  closeBtn.addEventListener('click', close);
}

/**
 * `usable` — usableConnectors() output. `selected` — a live Set of
 * connector ids this picker mutates directly; the caller owns persistence
 * (onChange fires after every toggle, same as before). `label` is the
 * button's base text (before the "(N)" suffix). `emptyHint` covers the
 * "nothing usable yet" case. Returns the button element — append it
 * anywhere a toolbar or card wants it.
 */
export function connectorPickerButton({
  usable,
  selected,
  onChange,
  label = 'Connectors',
  emptyHint = 'No connected apps are usable yet — connect one in App Control first.',
} = {}) {
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'btn';

  function refreshLabel() {
    btn.textContent = selected.size ? `${label} (${selected.size})` : label;
  }
  refreshLabel();

  btn.addEventListener('click', () => {
    const pop = popover({
      anchor: btn,
      build(el) {
        if (!usable.length) {
          el.appendChild(
            Object.assign(document.createElement('p'), { className: 'hint', textContent: emptyHint })
          );
          return;
        }

        const list = document.createElement('div');
        list.className = 'popover-list';
        for (const connector of usable.slice(0, INLINE_CAP)) {
          list.appendChild(
            buildConnectorRow(connector, selected, () => {
              refreshLabel();
              onChange?.();
            })
          );
        }
        el.appendChild(list);

        if (usable.length > INLINE_CAP) {
          const viewAllBtn = document.createElement('button');
          viewAllBtn.type = 'button';
          viewAllBtn.className = 'btn popover-view-all';
          viewAllBtn.textContent = `View all (${usable.length})`;
          viewAllBtn.addEventListener('click', () => {
            pop.close();
            openConnectorOverlay({
              usable,
              selected,
              title: `All ${label.toLowerCase()}`,
              onChange: () => {
                refreshLabel();
                onChange?.();
              },
            });
          });
          el.appendChild(viewAllBtn);
        }
      },
    });
  });

  return btn;
}

// Minimal hash router (#/models) built on top of app.js's existing
// showScreen() primitive. One place voice navigation (the open_section
// skill's ui_action) and the drawer menu both go through, and the browser
// back button works for free because real URLs are involved.

import { SECTIONS, findSection } from './nav.js';

const screenModules = new Map(); // sectionId -> loaded module, cached after first import
let showScreenFn = null;
let afterNavigateFn = null;
let refreshDebounce = null;

export function initRouter({ showScreen, afterNavigate }) {
  showScreenFn = showScreen;
  afterNavigateFn = afterNavigate;
  window.addEventListener('hashchange', () => navigate(currentSectionId(), { pushHash: false }));
}

export function currentSectionId() {
  const hash = location.hash.replace(/^#\/?/, '');
  return SECTIONS.some((s) => s.id === hash) ? hash : 'home';
}

export async function navigate(sectionId, { pushHash = true } = {}) {
  const section = findSection(sectionId);

  // Setting location.hash fires a native 'hashchange' event, which our own
  // listener above turns into a second navigate(..., {pushHash:false})
  // call. Rendering here AND letting that second call render too used to
  // double-render every section (each card appearing twice). So: when the
  // hash is actually changing, only update it and return — the hashchange
  // handler does the one real render. Render directly only when the hash
  // is already correct (same-section re-click, or an internal
  // pushHash:false call from hashchange/initial boot).
  if (pushHash) {
    const target = section.id === 'home' ? '#/' : `#/${section.id}`;
    const current = location.hash || '#/';
    if (current !== target) {
      location.hash = target;
      return;
    }
  }

  if (section.screen) {
    // The built-in assistant screen is already in the DOM — just show it.
    showScreenFn(section.screen);
  } else {
    showScreenFn('generic-screen');
    await renderInto(section);
  }

  afterNavigateFn?.(section.id);
}

/**
 * Re-invokes a screen's own render() if it's the one currently showing — for
 * app.js's SSE dispatcher to reflect a server-side state change (e.g. a
 * model health transition, see server/models/health.js) live, without the
 * user reloading. Debounced so a burst of transitions (e.g. "Check all
 * models" flipping several models in a row) collapses into one re-render
 * instead of one per event. Does nothing if that screen isn't the active one
 * right now, or was never loaded in the first place — same "wipe and
 * rebuild from scratch" contract every screen's render() already follows,
 * just re-triggered from outside a navigation event.
 */
export function refreshIfActive(sectionId) {
  if (currentSectionId() !== sectionId) return;
  const mod = screenModules.get(sectionId);
  if (!mod) return;
  clearTimeout(refreshDebounce);
  refreshDebounce = setTimeout(() => {
    const container = document.getElementById('generic-screen-content');
    if (!container) return;
    Promise.resolve(mod.render(container)).catch((err) =>
      console.error(`[router] refreshIfActive("${sectionId}") failed:`, err)
    );
  }, 400);
}

async function renderInto(section) {
  document.getElementById('generic-title').textContent = section.label;
  const container = document.getElementById('generic-screen-content');
  container.innerHTML = '<p class="hint">Loading…</p>';
  try {
    let mod = screenModules.get(section.id);
    if (!mod) {
      mod = await import(section.module);
      screenModules.set(section.id, mod);
    }
    await mod.render(container);
  } catch (err) {
    console.error(`[router] failed to render "${section.id}":`, err);
    container.innerHTML = '<p class="error">Could not load this section. Try again, or check the console for details.</p>';
  }
}

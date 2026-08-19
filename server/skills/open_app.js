// Skill: launches a program installed on the user's Windows PC.
//
// Safety: the model only ever supplies a friendly NAME, never a command. Two
// paths from name to launch, both safe for the same reason — what actually
// runs is something we resolved ourselves from a fixed source (our own map,
// or a real shortcut file already sitting on the user's PC), never text the
// model constructed:
//   1. The fixed ALLOWLIST below — a short list of common apps, checked first.
//   2. A resolve-then-launch fallback — searches the user's real Start Menu
//      shortcuts for a name match and launches THAT shortcut file directly
//      (Windows' own `start` command runs a .lnk exactly like an .exe, no
//      extra resolution needed). This is what makes "open any app I've
//      actually installed" work in general, not just the ~20 in the map.

import fs from 'node:fs';
import path from 'node:path';
import { spawn } from 'node:child_process';

// Values are either a plain command (resolved via Windows' `start`, which
// checks the PATH and the "App Paths" registry — covers built-in apps and
// most installed desktop software) or a protocol URI (for Settings/Spotify).
const ALLOWLIST = {
  notepad: 'notepad',
  'text editor': 'notepad',
  calculator: 'calc',
  calc: 'calc',
  paint: 'mspaint',
  'ms paint': 'mspaint',
  'file explorer': 'explorer',
  explorer: 'explorer',
  files: 'explorer',
  settings: 'ms-settings:',
  'windows settings': 'ms-settings:',
  'task manager': 'taskmgr',
  'command prompt': 'cmd',
  cmd: 'cmd',
  terminal: 'cmd',
  'control panel': 'control',
  chrome: 'chrome',
  'google chrome': 'chrome',
  edge: 'msedge',
  'microsoft edge': 'msedge',
  word: 'winword',
  'microsoft word': 'winword',
  excel: 'excel',
  'microsoft excel': 'excel',
  powerpoint: 'powerpnt',
  'microsoft powerpoint': 'powerpnt',
  spotify: 'spotify:',
};

// Every real installed app normally has a shortcut in one of these two
// folders (all-users vs. this-user) — the exact same place the Windows Start
// Menu itself reads from, so "can Jarvis open it" ends up matching "is it in
// your Start Menu" almost exactly.
const START_MENU_DIRS = [
  process.env.ProgramData && path.join(process.env.ProgramData, 'Microsoft', 'Windows', 'Start Menu', 'Programs'),
  process.env.APPDATA && path.join(process.env.APPDATA, 'Microsoft', 'Windows', 'Start Menu', 'Programs'),
].filter(Boolean);

const MAX_SHORTCUTS_SCANNED = 2000; // generous cap so a pathological Start Menu can't hang this

// Exported (alongside the default skill export) purely so the matching logic
// is directly testable without actually launching anything — per CLAUDE.md's
// pure-logic-module pattern.
export function listStartMenuShortcuts() {
  const shortcuts = [];
  for (const dir of START_MENU_DIRS) {
    if (!fs.existsSync(dir)) continue;
    let relativePaths;
    try {
      relativePaths = fs.readdirSync(dir, { recursive: true });
    } catch {
      continue; // a locked-down folder or a transient read error — not fatal, just skip it
    }
    for (const rel of relativePaths) {
      if (shortcuts.length >= MAX_SHORTCUTS_SCANNED) break;
      if (!rel.toLowerCase().endsWith('.lnk')) continue;
      shortcuts.push({ name: path.basename(rel, '.lnk'), fullPath: path.join(dir, rel) });
    }
  }
  return shortcuts;
}

/** Exact name match first; otherwise the shortest shortcut name that contains (or is contained by) the query — favors a direct match ("code" -> "Visual Studio Code") over an unrelated longer name that happens to share a substring. */
export function findBestShortcutMatch(query, shortcuts) {
  const q = query.toLowerCase();
  const exact = shortcuts.find((s) => s.name.toLowerCase() === q);
  if (exact) return exact;

  const partial = shortcuts
    .filter((s) => {
      const name = s.name.toLowerCase();
      return name.includes(q) || q.includes(name);
    })
    .sort((a, b) => a.name.length - b.name.length);
  return partial[0] || null;
}

export default {
  name: 'open_app',
  description:
    'Launch a program installed on the user\'s Windows computer — anything in their Start Menu, not just a fixed ' +
    'list. Only use this for actual desktop apps, not websites.',
  parameters: {
    type: 'object',
    properties: {
      app: {
        type: 'string',
        description: 'The name of the app to open, e.g. "notepad", "calculator", or "Visual Studio Code".',
      },
    },
    required: ['app'],
  },
  async run({ app }) {
    const rawName = String(app || '').trim();
    const key = rawName.toLowerCase();
    const allowlisted = ALLOWLIST[key];

    if (allowlisted) {
      spawn('cmd.exe', ['/c', 'start', '', allowlisted], { detached: true, stdio: 'ignore' }).unref();
      return { ok: true, opened: app };
    }

    const shortcuts = listStartMenuShortcuts();
    const match = findBestShortcutMatch(rawName, shortcuts);
    if (!match) {
      return {
        ok: false,
        error:
          `I couldn't find "${app}" — it's not one of the apps I know by name (${[...new Set(Object.values(ALLOWLIST))].join(', ')}), ` +
          `and no matching shortcut turned up in your Start Menu.`,
      };
    }

    spawn('cmd.exe', ['/c', 'start', '', match.fullPath], { detached: true, stdio: 'ignore' }).unref();
    return { ok: true, opened: match.name };
  },
};

// Skill: grants Jarvis permission to read/write/move files inside one
// specific folder. This is the ONLY way the files connector's allowlist
// ever grows — there is no settings screen for it (see CLAUDE.md's "App
// Control connectors" section). A file tool hitting a folder outside the
// allowlist fails with a plain error; the model is expected to ask the user
// in conversation and only then call this. `confirm: 'always'` reuses the
// existing read-back-and-confirm gate (skills/index.js) rather than trusting
// the model's own judgment about what counts as a real "yes."
//
// Meta skill (excluded from the scheduled-task/briefing picker and from any
// unattended/background turn) — granting new folder access only ever makes
// sense as part of a live, two-way conversation, never something a scheduled
// prompt-mode task should be able to trigger on its own.

import path from 'node:path';
import { getOrCreateSingleton, updateConnector } from '../connectors/store.js';

// A small, non-editable floor under the folder allowlist — the same
// "sensible hardcoded defaults" philosophy as control/safety.js's window/
// process blocklist, applied to the filesystem: these can never be granted,
// no matter what's asked for, since they're core to Windows itself.
const DENYLISTED_ROOTS = ['C:\\Windows', 'C:\\Program Files', 'C:\\Program Files (x86)'];

function isDenylisted(resolved) {
  const lower = resolved.toLowerCase();
  return DENYLISTED_ROOTS.some((root) => {
    const rootLower = root.toLowerCase();
    return lower === rootLower || lower.startsWith(rootLower + path.sep);
  });
}

export default {
  name: 'allow_folder',
  meta: true,
  description:
    'Grants Jarvis permission to read, write, and move files inside one specific folder. Only call this ' +
    'after a file operation failed because the folder was not allowed yet, and the user has agreed to let ' +
    'Jarvis use that folder.',
  confirm: 'always',
  parameters: {
    type: 'object',
    properties: {
      folder: { type: 'string', description: 'The absolute folder path to allow, e.g. C:\\Users\\you\\Documents.' },
    },
    required: ['folder'],
  },
  summarize(args) {
    return `Allow Jarvis to read, write, and move files inside "${args.folder}"?`;
  },
  async run(args) {
    const resolved = path.resolve(args.folder);
    if (isDenylisted(resolved)) {
      return { ok: false, error: `"${resolved}" is a core Windows folder — Jarvis can't be granted access to it.` };
    }
    const connector = getOrCreateSingleton('files', { label: 'Files' });
    const allowedFolders = connector.config?.allowedFolders || [];
    if (allowedFolders.includes(resolved)) {
      return { ok: true, alreadyAllowed: resolved };
    }
    updateConnector(connector.id, { config: { allowedFolders: [...allowedFolders, resolved] } });
    return { ok: true, granted: resolved };
  },
};

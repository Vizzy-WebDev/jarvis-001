// The files connector: read/write/list/move, restricted to a folder
// allowlist. This is Jarvis's own built-in ability, not something the user
// configures on a settings screen (there isn't one) — the allowlist starts
// empty and grows only through conversation: a file tool hitting a path
// outside it fails with a plain error, the model asks the user for
// permission, and only after an explicit yes does it call the `allow_folder`
// skill (server/skills/allow_folder.js) to remember it. This is the
// "driving File Explorer to move a file is absurd when the operation is one
// function call" connector — no clicking involved at all.
//
// Every path is resolved to an absolute path and checked against the
// allowlist BEFORE any fs call — never trust a relative path or a `..`
// segment to stay where it looks like it's going.

import fs from 'node:fs';
import path from 'node:path';

const MAX_LIST_ENTRIES = 200;
const MAX_READ_BYTES = 512 * 1024; // 512KB — plenty for text files, not an accidental huge binary read

/** True if `target` (already resolved to an absolute path) is inside one of `allowedFolders`. */
function isAllowed(target, allowedFolders) {
  const resolved = path.resolve(target);
  return (allowedFolders || []).some((folder) => {
    const base = path.resolve(folder);
    return resolved === base || resolved.startsWith(base + path.sep);
  });
}

function requireAllowed(target, allowedFolders) {
  if (!allowedFolders?.length) {
    throw new Error('No folders are allowed yet — ask the user for permission to a specific folder first, then use allow_folder.');
  }
  const resolved = path.resolve(target);
  if (!isAllowed(resolved, allowedFolders)) {
    throw new Error(`"${target}" is outside every allowed folder — nothing was touched.`);
  }
  return resolved;
}

export function listFiles(dirPath, allowedFolders) {
  const resolved = requireAllowed(dirPath, allowedFolders);
  const entries = fs.readdirSync(resolved, { withFileTypes: true });
  return entries.slice(0, MAX_LIST_ENTRIES).map((e) => ({
    name: e.name,
    kind: e.isDirectory() ? 'folder' : 'file',
  }));
}

export function readFile(filePath, allowedFolders) {
  const resolved = requireAllowed(filePath, allowedFolders);
  const stat = fs.statSync(resolved);
  if (stat.isDirectory()) throw new Error(`"${filePath}" is a folder, not a file.`);
  if (stat.size > MAX_READ_BYTES) {
    throw new Error(`"${filePath}" is too large to read this way (${Math.round(stat.size / 1024)}KB).`);
  }
  return fs.readFileSync(resolved, 'utf8');
}

export function writeFile(filePath, content, allowedFolders) {
  const resolved = requireAllowed(filePath, allowedFolders);
  fs.mkdirSync(path.dirname(resolved), { recursive: true });
  fs.writeFileSync(resolved, String(content ?? ''), 'utf8');
  return { path: resolved };
}

export function moveFile(fromPath, toPath, allowedFolders) {
  const from = requireAllowed(fromPath, allowedFolders);
  const to = requireAllowed(toPath, allowedFolders);
  fs.mkdirSync(path.dirname(to), { recursive: true });
  fs.renameSync(from, to);
  return { from, to };
}

// ---------- tool declarations + dispatch (consumed by connectors/index.js) ----------

export function toolDeclarations() {
  return [
    {
      name: 'list_files',
      description: "List the files and folders inside one of the user's allowed folders.",
      parameters: { type: 'object', properties: { path: { type: 'string' } }, required: ['path'] },
    },
    {
      name: 'read_file',
      description: 'Read the text contents of a file inside one of the allowed folders.',
      parameters: { type: 'object', properties: { path: { type: 'string' } }, required: ['path'] },
    },
    {
      name: 'write_file',
      description: 'Write (creating or overwriting) a text file inside one of the allowed folders.',
      parameters: {
        type: 'object',
        properties: { path: { type: 'string' }, content: { type: 'string' } },
        required: ['path', 'content'],
      },
    },
    {
      name: 'move_file',
      description: 'Move or rename a file, as long as both the source and destination are inside allowed folders.',
      parameters: {
        type: 'object',
        properties: { from: { type: 'string' }, to: { type: 'string' } },
        required: ['from', 'to'],
      },
    },
  ];
}

export function dispatch(name, args, config) {
  const allowedFolders = config?.allowedFolders || [];
  if (name === 'list_files') return { ok: true, entries: listFiles(args.path, allowedFolders) };
  if (name === 'read_file') return { ok: true, content: readFile(args.path, allowedFolders) };
  if (name === 'write_file') return { ok: true, ...writeFile(args.path, args.content, allowedFolders) };
  if (name === 'move_file') return { ok: true, ...moveFile(args.from, args.to, allowedFolders) };
  throw new Error(`Unknown files tool: ${name}`);
}

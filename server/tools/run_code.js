// Skill: runs a short piece of code in an isolated sandbox and returns its
// output — for real computation (parsing something numeric, transforming
// data, checking an algorithm) that's more reliable done in code than
// worked out in the model's head. Not a general shell: no network and no
// file access beyond what's explicitly asked for and already allowed.
//
// Host folder access reuses the EXACT SAME allowlist allow_folder.js grants
// into (connectors/store.js's 'files' singleton) rather than trusting
// whatever folder the model claims it needs — if a folder isn't already
// allowed, this fails and tells the model to ask the user via allow_folder
// first, the same discipline the files connector itself already uses.
//
// confirm: 'always' — running code has real side effects even sandboxed (it
// can write into a granted folder, or reach the network if asked), so it
// goes through the same read-back-and-confirm gate as control_computer.js
// and allow_folder.js. Not meta: a scheduled task or the briefing may
// legitimately want to run a calculation too — ctx.autoConfirm already
// covers unattended runs the same way it does for those.

import path from 'node:path';
import { runCode as runInSandbox } from '../sandbox/runner.js';
import { getOrCreateSingleton } from '../connectors/store.js';
import { saveArtifact, deleteArtifact, artifactFilePath, recordVerification } from '../artifacts/artifact-store.js';
import { verifyFileOpens } from '../ops/verify.js';

const MIME_BY_EXT = {
  '.json': 'application/json',
  '.csv': 'text/csv',
  '.txt': 'text/plain',
  '.md': 'text/markdown',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.pdf': 'application/pdf',
  '.html': 'text/html',
};

const MAX_CODE_CHARS = 20_000;
const DEFAULT_TIMEOUT_MS = 10_000;

function truncateForSummary(code) {
  const oneLine = code.replace(/\s+/g, ' ').trim();
  return oneLine.length > 160 ? `${oneLine.slice(0, 160)}…` : oneLine;
}

export default {
  name: 'run_code',
  description:
    'Run a short piece of JavaScript or Python code in an isolated sandbox and get back its output — use this ' +
    'for real computation (math, parsing, data transforms, checking an algorithm) rather than working it out ' +
    'yourself. Not for opening apps or controlling the desktop (use control_computer for that) and not a general ' +
    'shell — no network and no file access beyond what the user has already allowed.',
  confirm: 'always',
  parameters: {
    type: 'object',
    properties: {
      language: { type: 'string', enum: ['javascript', 'python'], description: 'Which language the code is written in.' },
      code: { type: 'string', description: 'The code to run.' },
      needsNetwork: { type: 'boolean', description: 'True only if this code genuinely needs to reach the internet (e.g. an HTTP request). Defaults to false — no network.' },
      folder: { type: 'string', description: 'An absolute folder path the code needs to read or write, if any. Must already be allowed (see allow_folder) — leave out if the code only needs its own temporary workspace.' },
    },
    required: ['language', 'code'],
  },
  summarize(args) {
    return `Run this ${args.language} code${args.needsNetwork ? ' (it will reach the internet)' : ''}${args.folder ? ` with access to "${args.folder}"` : ''}: ${truncateForSummary(args.code)}`;
  },
  async run(args, ctx = {}) {
    const code = String(args.code || '');
    if (!code.trim()) return { ok: false, error: 'No code was given to run.' };
    if (code.length > MAX_CODE_CHARS) {
      return { ok: false, error: `That's too long to run at once (${code.length} characters, limit ${MAX_CODE_CHARS}).` };
    }

    let allowPaths = [];
    if (args.folder) {
      const connector = getOrCreateSingleton('files', { label: 'Files' });
      const allowedFolders = connector.config?.allowedFolders || [];
      // allow_folder.js stores folders through path.resolve() (forward
      // slashes become backslashes on Windows) — resolve here too, or a
      // folder the user already granted (e.g. via "C:/Users/.../Documents")
      // would never match its own backslash-normalized entry in the
      // allowlist. Confirmed live: without this, an already-granted folder
      // was wrongly reported as not allowed.
      const resolved = path.resolve(args.folder);
      const isAllowed = allowedFolders.some(
        (f) => resolved === f || resolved.toLowerCase().startsWith(`${f.toLowerCase()}\\`)
      );
      if (!isAllowed) {
        return {
          ok: false,
          error: `"${args.folder}" hasn't been allowed yet. Ask the user if it's okay, and if they agree, call allow_folder for that exact folder first, then try this again.`,
        };
      }
      allowPaths = [resolved];
    }

    const result = await runInSandbox({
      language: args.language,
      code,
      timeoutMs: DEFAULT_TIMEOUT_MS,
      allowNetwork: Boolean(args.needsNetwork),
      allowPaths,
    });

    // Output/Artifact generation (root CLAUDE.md's Operational Awareness
    // item 3) — any real file the code itself wrote (restricted-backend.js's
    // outputFiles, collected before its own throwaway temp dir was deleted)
    // becomes a real, downloadable artifact rather than being lost with the
    // temp folder. Each is mechanically verified the same way
    // create_artifact.js verifies its own output — a broken file is never
    // kept pretending to be a real deliverable.
    const artifacts = [];
    for (const f of result.outputFiles || []) {
      let artifact;
      try {
        const mimeType = MIME_BY_EXT[path.extname(f.name).toLowerCase()] || 'application/octet-stream';
        artifact = saveArtifact({ name: f.name, mimeType, content: f.content, sessionId: ctx.sessionId || null });
      } catch (err) {
        console.error('[run_code] saving a generated output file as an artifact failed:', err);
        continue;
      }
      const check = verifyFileOpens(artifactFilePath(artifact.id));
      if (!check.ok) {
        deleteArtifact(artifact.id);
        continue;
      }
      recordVerification(artifact.id, { verified: true, detail: null });
      artifacts.push({ id: artifact.id, name: artifact.name, mimeType: artifact.mimeType, size: artifact.size, url: `/api/artifacts/${artifact.id}` });
    }

    return {
      ok: result.ok,
      stdout: result.stdout,
      stderr: result.stderr || undefined,
      exitCode: result.exitCode,
      error: result.error,
      timedOut: result.timedOut || undefined,
      isolation: result.isolation,
      warnings: result.warnings,
      artifacts: artifacts.length ? artifacts : undefined,
      // Only rendered as a real file card when there's exactly one real
      // output — a rare multi-file run still lists every artifact above
      // (visible to the model, which can mention each), just without a
      // dedicated card for each; see this file's own header comment for
      // why that's an acceptable, disclosed scope limit rather than a bug.
      ui_action: artifacts.length === 1 ? { type: 'artifact_created', ...artifacts[0] } : undefined,
    };
  },
};

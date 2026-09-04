// Shared ZIP-writing helper for the Office format writers below — .NET's
// own `System.IO.Compression.ZipFile`/`ZipFileExtensions` via PowerShell,
// not a hand-rolled ZIP encoder (Node has no built-in ZIP WRITER, only
// zlib.inflateRawSync for reading — see documents/zip.js's own header
// comment) and not the `Compress-Archive` cmdlet either.
//
// **A real, live-caught bug is why `Compress-Archive` isn't used, despite
// being the pattern skills/store/skill-zip.js already established for
// packing a Skill folder.** Verified during this build's own round-trip
// test (write a .docx, read it back through documents/docx.js's real,
// independently-built reader): both `Compress-Archive -Path dir\*` AND
// even `[ZipFile]::CreateFromDirectory()` store every entry's path with
// Windows BACKSLASHES (`word\document.xml`) when the entry name is derived
// from directory traversal on this platform — silently invalid per the
// Open Packaging Conventions spec real Office requires (forward slashes),
// and confirmed to make this project's own reader fail to find
// `word/document.xml` at all. The fix: build each entry with an EXPLICIT,
// hand-constructed forward-slash name via `ZipFile.Open()` +
// `ZipFileExtensions.CreateEntryFromFile(zip, sourcePath, entryName)` —
// `entryName` is a plain string this code controls directly, never derived
// from a filesystem path, so the OS's own path-separator convention never
// leaks into it. Re-verified after the fix: the exact same round-trip
// (write -> read back through documents/docx.js) succeeds.

import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import { spawn } from 'node:child_process';

function runPowerShell(script) {
  return new Promise((resolve, reject) => {
    const ps = spawn('powershell.exe', ['-NoProfile', '-NonInteractive', '-Command', script], { stdio: ['ignore', 'ignore', 'pipe'] });
    let stderr = '';
    ps.stderr.on('data', (chunk) => { stderr += chunk; });
    ps.on('error', (err) => reject(new Error(err.message)));
    ps.on('exit', (code) => {
      if (code !== 0) reject(new Error(stderr.trim() || 'PowerShell command failed.'));
      else resolve();
    });
  });
}

/** Escapes a string for safe interpolation inside a single-quoted PowerShell literal — doubling an embedded `'` is PowerShell's own escape rule for that quote style. */
function psQuote(str) {
  return `'${String(str).replace(/'/g, "''")}'`;
}

/**
 * Writes `files` (a `{relativePath: contentString}` map, e.g.
 * `{'[Content_Types].xml': '...', 'word/document.xml': '...'}` — every key
 * ALWAYS forward-slash-delimited, becoming the exact ZIP entry name) to a
 * throwaway temp folder, zips them with each entry name passed through
 * verbatim, and returns the zip as a Buffer. The temp folder/zip are always
 * cleaned up, success or failure.
 */
export async function buildOfficeZip(files) {
  const workDir = path.join(os.tmpdir(), `jarvis-artifact-${Date.now().toString(36)}${Math.random().toString(36).slice(2, 8)}`);
  const zipPath = `${workDir}.zip`;
  try {
    fs.mkdirSync(workDir, { recursive: true });
    const entries = [];
    let i = 0;
    for (const [relPath, content] of Object.entries(files)) {
      // Written to disk under a flat, collision-proof name — the REAL
      // relative path lives only in `relPath`, passed to
      // CreateEntryFromFile as the entry name, never derived from where
      // the source file happens to sit on this filesystem.
      const sourceFile = path.join(workDir, `part${i++}`);
      fs.writeFileSync(sourceFile, content, 'utf8');
      entries.push({ sourceFile, entryName: relPath });
    }

    const addLines = entries
      .map((e) => `[System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile($zip, ${psQuote(e.sourceFile)}, ${psQuote(e.entryName)}) | Out-Null`)
      .join('\n');
    const script = [
      'Add-Type -AssemblyName System.IO.Compression.FileSystem',
      `$zip = [System.IO.Compression.ZipFile]::Open(${psQuote(zipPath)}, 'Create')`,
      'try {',
      addLines,
      '} finally { $zip.Dispose() }',
    ].join('\n');

    await runPowerShell(script);
    return fs.readFileSync(zipPath);
  } finally {
    fs.rmSync(workDir, { recursive: true, force: true });
    fs.rmSync(zipPath, { force: true });
  }
}

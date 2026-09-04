// Real screen recording (a proper .mp4), via ffmpeg — a genuinely separate
// process from everything else under server/control/: independent of the
// perceive/act loop in session.js, so a recording keeps running through an
// active control session exactly as intended, and independent of
// observation-bridge.js's badge (recording gets its own status, not folded
// into "Jarvis can see your screen").
//
// ffmpeg is a free, independent, open-source tool (not tied to any one
// company) — not bundled with this project (no new dependency committed to
// package.json), so it's resolved the same way browser.js resolves a real
// Chrome executable: check a couple of common install locations, then fall
// back to whatever the OS's own PATH resolves. If neither works, every
// caller gets a plain-language explanation of what ffmpeg is and how to get
// it, never a raw process-spawn error.

import { spawn } from 'node:child_process';
import fs from 'node:fs';
import { reserveRecordingPath } from './recording-store.js';

const FFMPEG_CANDIDATES = [
  'C:\\ffmpeg\\bin\\ffmpeg.exe',
  'C:\\Program Files\\ffmpeg\\bin\\ffmpeg.exe',
];

const NO_FFMPEG_MESSAGE =
  "Screen recording needs a free, independent program called ffmpeg, which isn't installed. " +
  "To add it: go to ffmpeg.org/download.html, download the Windows build, unzip it anywhere " +
  "(e.g. C:\\ffmpeg), then add its \\bin folder to your PATH (Windows Settings > System > About > " +
  "Advanced system settings > Environment Variables > add the \\bin folder to Path). Once that's " +
  "done, ask me to record again — no restart of Jarvis needed beyond a fresh terminal/PATH refresh.";

let cachedFfmpegPath = null; // resolved once, reused — a real ffmpeg install doesn't move mid-session
let activeRecording = null; // { proc, file, fullPath, startedAt }

function tryExisting(candidates) {
  for (const candidate of candidates) {
    if (fs.existsSync(candidate)) return candidate;
  }
  return null;
}

/** Probes whether a bare "ffmpeg" resolves on PATH — the common case if the user followed the install steps above, or a package manager (winget/choco) put it there. Resolves to true/false, never throws. */
function probePathFfmpeg() {
  return new Promise((resolve) => {
    let settled = false;
    const finish = (ok) => {
      if (settled) return;
      settled = true;
      resolve(ok);
    };
    let proc;
    try {
      proc = spawn('ffmpeg', ['-version'], { stdio: 'ignore', windowsHide: true });
    } catch {
      finish(false);
      return;
    }
    proc.on('error', () => finish(false)); // ENOENT — not on PATH
    proc.on('exit', (code) => finish(code === 0));
    setTimeout(() => finish(false), 4000).unref();
  });
}

/** Resolves a real, usable ffmpeg command/path, or null if none is found anywhere. Cached after the first successful resolution. */
export async function resolveFfmpeg() {
  if (cachedFfmpegPath) return cachedFfmpegPath;
  const fixed = tryExisting(FFMPEG_CANDIDATES);
  if (fixed) {
    cachedFfmpegPath = fixed;
    return fixed;
  }
  if (await probePathFfmpeg()) {
    cachedFfmpegPath = 'ffmpeg';
    return cachedFfmpegPath;
  }
  return null;
}

export function isRecording() {
  return Boolean(activeRecording);
}

/**
 * Starts recording the whole real desktop to a fresh .mp4 via ffmpeg's
 * gdigrab input device. `-vcodec libx264 -pix_fmt yuv420p` produces a file
 * any standard <video> element can actually play (raw gdigrab output alone
 * would be enormous and non-standard); `-movflags +faststart` puts the file
 * index at the front so it can start playing before fully downloading.
 */
export async function startRecording({ label } = {}) {
  if (activeRecording) return { ok: false, error: 'A recording is already running — stop that one first.' };

  const ffmpeg = await resolveFfmpeg();
  if (!ffmpeg) return { ok: false, error: NO_FFMPEG_MESSAGE };

  const { file, fullPath } = reserveRecordingPath({ label });
  const args = [
    '-f', 'gdigrab',
    '-framerate', '12',
    '-i', 'desktop',
    '-vcodec', 'libx264',
    '-pix_fmt', 'yuv420p',
    '-movflags', '+faststart',
    '-y',
    fullPath,
  ];

  let proc;
  try {
    // stdin left as a real pipe — stopRecording() writes 'q' to it, ffmpeg's
    // own clean-stop convention, so the output file is properly finalized
    // (a killed process can leave a corrupt/unplayable mp4). stdout/stderr
    // ignored — ffmpeg's own progress chatter isn't useful here and this
    // avoids an unbounded in-memory buffer for a long recording.
    proc = spawn(ffmpeg, args, { stdio: ['pipe', 'ignore', 'ignore'], windowsHide: true });
  } catch (err) {
    return { ok: false, error: err?.message || 'Could not start ffmpeg.' };
  }

  const startedAt = Date.now();
  activeRecording = { proc, file, fullPath, startedAt };
  proc.on('exit', () => {
    if (activeRecording?.proc === proc) activeRecording = null;
  });
  proc.on('error', (err) => {
    console.error('[screen-recorder] ffmpeg error:', err.message);
    if (activeRecording?.proc === proc) activeRecording = null;
  });

  return { ok: true, file };
}

/** Stops the active recording gracefully (ffmpeg's own 'q' keypress convention) and waits for it to actually finish writing the file before resolving. */
export function stopRecording() {
  return new Promise((resolve) => {
    if (!activeRecording) {
      resolve({ ok: false, error: 'Nothing is being recorded right now.' });
      return;
    }
    const { proc, file, fullPath, startedAt } = activeRecording;
    const durationMs = Date.now() - startedAt;

    let settled = false;
    const finish = (result) => {
      if (settled) return;
      settled = true;
      resolve(result);
    };

    proc.once('exit', () => {
      activeRecording = null;
      if (fs.existsSync(fullPath) && fs.statSync(fullPath).size > 0) {
        finish({ ok: true, file, durationMs });
      } else {
        finish({ ok: false, error: 'The recording did not produce a usable file.' });
      }
    });

    try {
      proc.stdin.write('q');
    } catch {
      // The process may already be gone — the 'exit' handler above still
      // fires and resolves this either way.
    }

    // A stuck ffmpeg process (rare, but real processes can hang) shouldn't
    // leave the caller waiting forever — force-kill and report whatever's on
    // disk after a bounded wait.
    setTimeout(() => {
      if (settled) return;
      try {
        proc.kill();
      } catch {
        // Already gone.
      }
    }, 8000).unref();
  });
}

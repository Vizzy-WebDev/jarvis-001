// The one entry point every caller (a skill, a future capability) uses to
// run untrusted code — picks a backend via detect.js and hands off to it.
// Nothing else in the codebase should import wsl-backend.js/
// restricted-backend.js directly; this is the seam that keeps "which
// backend" a decision made in one place.

import { detectBackend, wslSetupSteps } from './detect.js';
import * as wslBackend from './wsl-backend.js';
import * as restrictedBackend from './restricted-backend.js';

/**
 * @param {{language: string, code: string, files?: Array<{name, content}>, timeoutMs?: number, allowNetwork?: boolean, allowPaths?: string[], args?: string[]}} opts
 * @returns {Promise<{ok, stdout, stderr, exitCode, isolation, backend, ...}>}
 */
export async function runCode({ language, code, files = [], timeoutMs = 10000, allowNetwork = false, allowPaths = [], args = [] }) {
  const detection = await detectBackend();

  // `windows-sandbox` is detected honestly by detect.js but has no working
  // runner yet (see detect.js's comment) — falls through to `restricted`
  // rather than silently pretending a backend exists that was never built
  // or tested.
  const usingWsl = detection.backend === 'wsl';
  const backend = usingWsl ? wslBackend : restrictedBackend;

  const result = await backend.runCode({
    language,
    code,
    files,
    timeoutMs,
    allowNetwork,
    allowPaths,
    args,
    distro: detection.distro,
  });

  return {
    ...result,
    backend: usingWsl ? 'wsl' : 'restricted',
    backendNote: detection.backend === 'windows-sandbox' ? 'Windows Sandbox was detected but has no working runner yet — ran with the restricted backend instead.' : undefined,
  };
}

/** For GET /api/sandbox/status — what's active right now and, if it's the weak fallback, how to get the real thing. */
export async function sandboxStatus() {
  const detection = await detectBackend();
  const usingWsl = detection.backend === 'wsl';
  return {
    backend: usingWsl ? 'wsl' : 'restricted',
    isolation: usingWsl ? 'strong' : 'weak',
    distro: detection.distro || null,
    detectedWindowsSandbox: detection.backend === 'windows-sandbox',
    setupSteps: usingWsl ? [] : wslSetupSteps(),
  };
}

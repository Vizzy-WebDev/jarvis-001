# Sandbox (`server/sandbox/`)

Isolated code execution, used by `skills/run_code.js`, `skills/analyze_spreadsheet.js`,
and `skills/run_skill_script.js` (see the root `CLAUDE.md`'s "Skills" section, and
`server/skills/CLAUDE.md`) — a general facility, not Skills-only. `detect.js` picks a
backend at startup: `wsl` (a real distro installed) → `restricted` (fallback). Windows
Sandbox is detected but has no working runner built for it (no scriptable "run this,
hand back stdout" story exists the way `wsl -d <distro> -- <cmd>` has) so it's never
selected — reported truthfully via a `backendNote` rather than shipping an unverified
backend, same "don't claim something works on faith" rule as the connector catalog's
Figma exclusion (see `server/connectors/CLAUDE.md`).

- `restricted-backend.js` — a plain stripped-env child process in a fresh temp folder,
  `taskkill /t` to kill a runaway process tree on timeout, `isolation: 'weak'` on every
  result plus an explicit warning string. It does NOT actually block network access or
  host file reads when neither was granted — that label is honest, not aspirational.
- `wsl-backend.js` — `wsl.exe -d <distro> --cd <dir> -- <cmd>`, files written via the
  `\\wsl.localhost\<distro>\...` UNC path (plain Node `fs` calls land straight on the
  Linux side, no separate copy step), `unshare -n` attempted for network isolation with
  an honest `networkIsolated: false` fallback if it can't. **Not yet verified against a
  real WSL install** (none on the dev machine as of writing) — don't trust it the way
  `restricted-backend.js` is trusted until a real run against an actual distro confirms
  it. Two `wsl.exe`-specific facts: it writes UTF-16LE to stdout/stderr on Windows
  (decode as `'utf16le'` via a raw Buffer, not default UTF-8), and a Windows env var is
  **not** forwarded into WSL unless listed in `WSLENV` — Jarvis's secrets are already
  invisible there without any stripping on this code's part.
- `runner.js` — the one seam (`runCode()`/`sandboxStatus()`) everything else calls;
  nothing outside this folder imports the backend files directly.
- Host folder access (`run_code.js`) reuses the exact same allowlist `allow_folder.js`
  grants into (`connectors/store.js`'s `files` singleton), comparison always run through
  `path.resolve()` so a forward-slash argument still matches a backslash-normalized
  allowlist entry.

<!-- Ported from the Node build during the S6 cutover. The architecture, the
invariants and the live-caught bugs described here all carried over deliberately and
still hold. File paths have been updated to their real Python counterparts and are
verified to exist. Function names written in camelCase (`getToolDeclarations()`) are
the NODE originals, kept because the surrounding reasoning is about them; the Python
equivalent is the snake_case function doing that job in the same module. Where a Node
module had no Python counterpart, the text says so rather than pointing at a file that
does not exist. -->

# Sandbox (`jarvis/sandbox/`)

Isolated code execution, used by `skills/run_code.py`, `skills/analyze_spreadsheet.py`,
and `tools/skill_tools.py` (see the root `CLAUDE.md`'s "Skills" section, and
`jarvis/skills/CLAUDE.md`) — a general facility, not Skills-only. `sandbox/runner.py` picks a
backend at startup: `wsl` (a real distro installed) → `restricted` (fallback). Windows
Sandbox is detected but has no working runner built for it (no scriptable "run this,
hand back stdout" story exists the way `wsl -d <distro> -- <cmd>` has) so it's never
selected — reported truthfully via a `backendNote` rather than shipping an unverified
backend, same "don't claim something works on faith" rule as the connector catalog's
Figma exclusion (see `jarvis/connectors/CLAUDE.md`).

- `sandbox/runner.py` — a plain stripped-env child process in a fresh temp folder,
  `taskkill /t` to kill a runaway process tree on timeout, `isolation: 'weak'` on every
  result plus an explicit warning string. It does NOT actually block network access or
  host file reads when neither was granted — that label is honest, not aspirational.
- `sandbox/runner.py` — `wsl.exe -d <distro> --cd <dir> -- <cmd>`, files written via the
  `\\wsl.localhost\<distro>\...` UNC path (plain Node `fs` calls land straight on the
  Linux side, no separate copy step), `unshare -n` attempted for network isolation with
  an honest `networkIsolated: false` fallback if it can't. **Not yet verified against a
  real WSL install** (none on the dev machine as of writing) — don't trust it the way
  `sandbox/runner.py` is trusted until a real run against an actual distro confirms
  it. Two `wsl.exe`-specific facts: it writes UTF-16LE to stdout/stderr on Windows
  (decode as `'utf16le'` via a raw Buffer, not default UTF-8), and a Windows env var is
  **not** forwarded into WSL unless listed in `WSLENV` — Jarvis's secrets are already
  invisible there without any stripping on this code's part.
- `runner.py` — the one seam (`runCode()`/`sandboxStatus()`) everything else calls;
  nothing outside this folder imports the backend files directly.
- Host folder access (`run_code.py`) reuses the exact same allowlist `allow_folder.py`
  grants into (`connectors/store.py`'s `files` singleton), comparison always run through
  `path.resolve()` so a forward-slash argument still matches a backslash-normalized
  allowlist entry.

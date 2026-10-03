# Sandbox (`jarvis/sandbox/`)

Isolated Python execution for `tools/run_code.py`, `tools/analyze_spreadsheet.py` and
`tools/skill_tools.py`. It is a general facility, not Skills-only. Everything goes
through `runner.py`; nothing outside this folder reaches around it.

- **Two backends, probed once per process** (`backend()`): `wsl` when `wsl.exe -e true`
  succeeds, otherwise `restricted`. Windows Sandbox is not used: there is no scriptable
  "run this and hand back stdout" path for it.
  - `wsl` — the code runs in a WSL distribution with its own filesystem and processes. This
    is the only backend that deserves the word "sandbox".
  - `restricted` — a plain subprocess in a throwaway temp folder with a hard timeout and
    capped output. It stops accidents, not an attacker: it runs as the user and can read
    other files on the machine.
- **Isolation is reported, never implied.** `describe_isolation()` is generated from what
  the probe found, is put into the tool's own description at load time, and names what is
  NOT protected. `status()` backs `GET /api/sandbox/status` and, on the fallback, returns
  the plain-language `wsl --install` steps. It does not know which distro is installed.
- **Secrets never reach the child.** `child_environment()` is `jarvis/childenv.py`'s shared
  scrub — the parent holds every API key, and a child inheriting `os.environ` inherits them
  all.
- **`run_python(code, timeout_s, keep_files, input_files)`** returns a `SandboxResult`.
  `input_files` are written next to `script.py`, flattened to a basename so a caller cannot
  write outside the workspace, and can never overwrite the script. Files the script
  produced (up to `MAX_OUTPUT_FILES`) are handed back for the caller to turn into
  artifacts. Output is capped at `MAX_OUTPUT_CHARS`.
- The WSL path has not been verified against a real WSL install on the dev machine — do
  not trust it beyond what a real run confirms.

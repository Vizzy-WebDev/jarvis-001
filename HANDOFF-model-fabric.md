# Handoff: Model Fabric completion pass

Written 2026-10-13 for the next Claude session to continue.

- **Branch:** `claude/model-fabric` (made from `claude/ui-redesign` at cb7d681)
- **Worktree:** `C:\Users\HP\Documents\CLAUDE PROJECT\Jarvis-001-fabric`. The main folder `Jarvis-001` is on
  `claude/own-browser` (another session's work); do not commit there.
- **Commits on the branch (3, on top of ui-redesign):** `a8fea5b` core fabric fixes, `58f1008` model screen
  wording + rebuilt `frontend/out`, `0fba2b9` "does not exist or no access" stays a missing model.
- **Memory files to read first** (`~/.claude/projects/C--Users-HP-Documents-CLAUDE-PROJECT-Jarvis-001/memory/`):
  `jarvis-test-env-windows.md`, `feedback-scratch-env-for-every-app-boot.md`,
  `feedback-free-memory-without-asking.md`, `jarvis-quota-constraint.md`, `concurrent-claude-sessions.md`.

## What the user asked for
A final, one-pass "Model Fabric" check of `intelligence/models/`: use the official Hermes Agent repo
(`NousResearch/hermes-agent`) as a starting point (not a blueprint), study how it discovers, classifies and
presents models, and verify the whole fabric (direct providers Gemini/OpenAI/Anthropic, gateways, capabilities,
selection, request execution, reasoning controls, routing, fallback, retries, errors). Rebuild only what is
demonstrably misdesigned, fix demonstrated bugs, no new approval cycle, run regression + e2e tests, document
real results, and do NOT declare completion without evidence. Nothing is pushed unless the user says so
(see "Push state" below).

## Conclusion reached
The architecture is sound (scope/recovery errors, drivers translate only, quirks as data, per-model effort
sets); Hermes corroborates it. So: targeted corrections, no rebuild. Discovery principle adopted:
**capability-evidence filtering, not name curation.** Hermes curates model-name lists; Jarvis must hold no
model names in code, so it keeps the full discovered catalog honestly and only *offers* models that can take a
chat turn, failing open when the listing is silent.

## Work completed (all in the 3 commits)
- `ModelNotAllowed` error (`type model_not_allowed`, scope endpoint, recovery user): a 400/403/404 saying "no
  access" / "wrong endpoint" rests only that endpoint as `unusable`, not the whole connection
  (`errors.py`, `drivers/_wire.py` `_NOT_ALLOWED`/`_MISSING`, `rests.py`, `resolve.py`). Text that also says the
  model does not exist stays `ModelNotFound` (OpenAI's real 404 says both; caught by a Playwright test).
- Honest kinds for id-only OpenAI listings via non-chat `model_profiles` in `defaults.yaml` (embedding, speech,
  transcription, image, video, moderation, realtime, completion). Gemini models without `generateContent` get
  kind `embedding` or `other` (raw method names no longer leak).
- `settings.chat_problem(endpoint)`: non-chat kind or `tools` explicitly false -> `select()` refuses (route returns
  400), picker hides it (`ModelPicker.tsx`), Model Settings shows "not for chatting" (`ConnectionCard.tsx`,
  `chatProblem` in `api-types.ts`).
- OpenRouter per-model `reasoning` object -> `reasoning_levels` (weakest first), `reasoning_default`,
  `reasoning_can_disable` (`none` listed and not mandatory); negative prices = no price; `upstream_from_id`
  quirk for gateways (`drivers/openai_chat.py`).
- `settings.effort()` returns the saved level only if the chosen endpoint still lists it.
- New tests: `tests/test_layer_fabric_audit.py` (25). `tests/test_layer_foundation.py` pins the new error type.
- `intelligence/models/DECISIONS.md`: new section "Model Fabric completion pass" (Hermes comparison, decisions,
  live-check numbers, unresolved list).

## Test evidence (venv Python + sitecustomize fallback, scratch dirs)
- Model-layer + contract + architecture files (`tests/test_layer_*.py`, `test_contract.py`,
  `test_architecture.py`, `test_models*.py`) plus the previously failing Playwright test: **405 passed, 4 skipped**.
- Full backend run (browser suites excluded, Notepad/ffmpeg tests deselected): 13 failed, 2043 passed, 1 error.
  - Windows-only, expected: five `test_control_*`/`test_monitor` tests; `test_durable_crash` (`signal.SIGKILL`).
  - Resource noise, pass alone: skills, content_volume, turn_latency, artifacts_e2e, voice_edit_e2e, the niche-naming error.
  - Still failing, unrelated to models: `test_content_manual_e2e::test_adding_a_video_...` and
    `test_content_niches_e2e::test_niche_folders_...` (the test helper's Chromium WebM recording returns 110
    bytes on this machine). NOT yet confirmed to fail without these changes.
- Playwright `tests/test_shell_e2e.py -k "model or picker or provider or connection"`: 12 passed, 1 failed (the
  404-wording regression), since fixed and re-run green.
- Front end: `npm run typecheck` and `npm run build` clean; `frontend/out` rebuilt and committed.
- Live listing (real keys read from `.env`, listing calls only, scratch data dir): Gemini 62 models/34 chat,
  OpenAI 127/81, Anthropic 14/14 with per-model effort levels read from the API.

## NOT done (do these next, in order)
1. **Run the browser suites** as two separate invocations, from the worktree root:
   `test_shell_e2e.py` (~25 min; only the model-related subset has run) and `test_content_e2e.py`. Triage
   failures by re-running each alone before blaming code.
2. **Confirm the two WebM-recorder failures are pre-existing**: run them in a clean checkout of the base
   (`claude/ui-redesign`, e.g. worktree `Jarvis-001-ui`) with the same environment.
3. **Remaining fabric scrutiny** (only where something is demonstrably wrong): retry/backoff and routing/fallback
   with a gateway-shaped catalog (upstream preference), per-provider error mapping, Gemini budget mode dropping an
   effort level missing from `thinking_budgets`, Gemini extra listing fields, LM Studio native list, vLLM
   `max_model_len`. Add a trimmed real-shaped OpenRouter fixture (the 768 KB real catalog is too big to commit)
   for an end-to-end effort-hint-on-the-wire test and gateway fallback preference.
4. **Unresolved, state plainly if still true**: OpenRouter `:batch` rows (usable for chat? unverified, kept as
   rows); OmniRoute is docs-only (mirrors, `parent` rows, `auto/*` routers unhandled); real no-access error
   bodies from OpenAI/Anthropic/Gemini never provoked live; Gemini music/transcription/research models that list
   `generateContent` still appear as chat until a first call refuses them.
5. Run the `pre-merge-gate` skill before any merge to `main`. Do not merge without being told.

## How to run things on this box
- `.venv` (`Jarvis-001\.venv`) has `mcp` and `jsonschema` but no pytest/playwright. Create a scratch dir with a
  `sitecustomize.py` that appends the global site-packages
  (`C:\Users\HP\AppData\Local\Programs\Python\Python314\Lib\site-packages`) to `sys.path`, then:
  `PYTHONPATH="<scratch>;<worktree>/server"` and run `<Jarvis-001>/.venv/Scripts/python.exe -m pytest -p no:cacheprovider ...`.
  Use `;` and drive-letter paths (not `/c/...`) in `PYTHONPATH`. Global Python lacks `mcp`, so
  `test_mcp_connector_flow` fails collection there.
- `frontend/node_modules` in this worktree is a junction to `Jarvis-001\frontend\node_modules`
  (git-ignored). Recreate it if missing: `New-Item -ItemType Junction`.
- Deselect `tests/test_tools.py::test_open_app_refuses_honestly_off_windows` (launches Notepad) and
  `tests/test_control_recording.py::test_recording_is_refused_where_there_is_no_windows_desktop` (leaves an
  ffmpeg recording ~500 MB alive). Kill stray `ffmpeg` and `Notepad` afterwards.
- A full backend run takes ~80 minutes here; use targeted runs while iterating. Under low memory, close
  programs (user's rule: free memory, never stop to ask) but never restart the user's Jarvis on :3000 for testing.
- Live provider check: scratch `JARVIS_DATA_DIR` containing a `models.yaml` with `gemini`/`openai`/`anthropic`
  connections (`secret_ref: model_gemini`/`model_gpt`/`model_claude`), `JARVIS_ENV_PATH` -> the real
  `Jarvis-001\.env` (read only; never print keys), then `models.refresh_catalog()` and `models.list_endpoints()`.
  Keep to listing calls; free-tier quota is the binding constraint.
- Any script that boots the app or touches `get_db()` needs a scratch `JARVIS_DATA_DIR` exported as a literal path.

## Push state

All commits, including this file, are pushed to `origin/claude/model-fabric` (GitHub `Vizzy-WebDev/jarvis-001`), which tracks the local branch. No other branch was touched and nothing was merged to `main`. A copy of this file also sits (untracked) at the top of the main `Jarvis-001` folder so it is easy to find; the tracked copy is on the branch above. Verify with `git rev-list --left-right --count origin/claude/model-fabric...HEAD` (expect `0 0`).

## Rules that still apply
Commit in small verified chunks with specific `git add` paths; end commit messages with the attribution lines
from the session's system reminder. Another Claude session may edit this repo at the same time; re-read shared
files before editing. Report results honestly; do not call this complete until the "NOT done" list is closed
or explicitly accepted by the user.

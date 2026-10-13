# Handoff — making Jarvis a genuinely autonomous agent (Stages 2–3)

Written 2026-10-13 for the next Claude session. Read this first, then carry on from
**"Exact next steps"**. Nothing below needs redoing unless it says so.

## The goal, in the owner's words

Jarvis must become a *genuinely autonomous agent*: take an objective, understand the outcome,
plan, choose tools/models/specialists, execute, verify, recover, adapt, continue across
interruptions and restarts, manage ongoing objectives, and initiate work only within approved
responsibilities — "through the simplest reliable architecture — not merely to add features".

Seven stages were approved ("Proceed"): **0** proof harness + dependable base · **1** ownership
(objective/brief/ledger) · **2** the executive · **3** the checker · **4** subtasks with join +
need-based routing · **5** triggers/responsibilities/authority limits · **6** learning loop.

A stage is done only when its scenarios pass at **three levels**: unit tests, the simulated
world with a stand-in model, and the **real model**. Hard rules: no outward action outside
authority · nothing outward done twice · **nothing called verified that was not checked**.

## Where the work lives

| What | Where |
|---|---|
| Branch | `claude/autonomy` (pushed to `origin`) |
| Worktree to edit in | `C:\Users\HP\Documents\CLAUDE PROJECT\Jarvis-001-autonomy` |
| Frozen copy for live runs | `..\Jarvis-001-live` (detached; `git checkout --detach <commit>` it to the commit under test, so development can continue while a 10–15 minute live run goes) |
| Stage 0 baseline copy | `..\Jarvis-001-baseline` (detached `b8e41f8`) |
| Main folder `Jarvis-001` | **another session's** (branch `claude/own-browser`). Never switch branches or edit there. Its `.venv` is what runs the tests. |

Other sessions own `claude/ui-redesign`, `claude/model-fabric`, `claude/own-browser`. Migrations
40 and 41 are this branch's; the UI session was told to start at 42, so **Stage 3 added no
migration** (the check lives in the job's ledger JSON).

### How to run things (Windows, Git Bash)

The original scratchpad may be gone in a new session. Recreate two tiny helpers anywhere:

```bash
# t.sh — run the main repo's venv python inside the autonomy worktree, on scratch data
export PYTHONPATH="<scratch>/site"          # holds a sitecustomize.py that APPENDS the global site-packages (pytest, playwright)
export JARVIS_DATA_DIR="<scratch>/data"
export JARVIS_ENV_PATH="<scratch>/data/.env"
cd "/c/Users/HP/Documents/CLAUDE PROJECT/Jarvis-001-autonomy" || exit 1
"/c/Users/HP/Documents/CLAUDE PROJECT/Jarvis-001/.venv/Scripts/python.exe" "$@"
```
(`tl.sh` is the same with `Jarvis-001-live`.) See the memory note *Running tests on this Windows box*.

- Tests: `t.sh -m pytest tests/<files> -q -p no:cacheprovider`
- Whole non-browser suite (~16 min; write to a file, run with `run_in_background`):
  `t.sh -m pytest tests -q -p no:cacheprovider $(ls tests/*e2e*.py tests/test_browser_*.py tests/test_control_recording.py | sed 's/^/--ignore=/') --deselect tests/test_tools.py::test_open_app_refuses_honestly_off_windows > full.txt 2>&1`
  — `test_control_recording.py` starts an ffmpeg screen recording that hangs on this machine; ignore it.
  Known Windows-only failures, not ours: `test_control_screen` ×2, `test_control_session` ×1, `test_monitor` ×1.
- Live proof: `tl.sh tests/proving/live.py --real-root "C:/Users/HP/Documents/CLAUDE PROJECT/Jarvis-001" --only <ids> [--repeat N] --out "<windows path>" --yes`
  in the background (`timeout` up to 2 h), then poll for `<out>/report.txt`. Each run folder holds
  `verdict.json`, `facts.json`, `data/jarvis.db`, `data/world.json`, `data/world-log.jsonl`.
  The runner copies the real `.env` and model settings into a throw-away folder; it never touches real data.
- **Heredocs that contain backslashes or apostrophes get mangled by the Bash tool.** Write edit
  scripts with the Write tool and run them; do not paste `\n`/`\d` through a heredoc.
- **Never `git stash` / `git stash pop` in this repo** — the stash list is shared by every
  worktree; a pop applied another session's stash to this tree (recovered, nothing lost; noted
  in memory).

## What is finished and verified

All of this is committed and pushed on `claude/autonomy`.

**Stage 0, Stage 1** — done earlier (proof harness in `tests/proving/`, objective/brief/ledger,
hand-off to background, model-wait, scheduler retries). See the root `CLAUDE.md` and
`background/jobs/CLAUDE.md` on this branch.

**Stage 2 (the executive) — fully validated.** All four real-model tests passed on one commit
(`efa317b`): three 42 calls · detour 28 · dead_end 22 (honest stop) · marathon 53. Getting
test 4 to pass on final code took five attempts and found real faults, all fixed with tests:

| Commit | What |
|---|---|
| `62a3cc2` | An empty progress note at a round's end is the round running out, not a failure. A model answering with nothing / refusing is model trouble, never an "approach" recovery (`durable.model_gave_nothing`); a reply cut off at the server's length limit gets `CUT_OFF_NOTE`. Work already handed to the background in a reply is never handed over twice. **A server that cannot be reached is asked once per call and rests as a connection** (`_wire.network_error` → connection scope; `execute.py` drops a failed connection's other endpoints) — Auto had walked a dead local gateway's 1,404 models for 10+ minutes. A model being asked/answering is a sign of life (`worker._alive`), so slow replies are not "hung". Live runner keeps Windows awake. |
| `3be3a62` | **Approved item 2:** a specialist's background job runs `PRE_CONSENTED` like any other job. Three tests pin it: MEDIUM runs, HIGH still parks and runs only on the person's yes, access (`allowed_names`) still refuses. A specialist run recovered after a restart still uses `ESCALATE` (`conversation/agents/durable_runs.py`, untouched). |
| `efa317b` | **Approved item 4:** `<think>` leakage fixed at the model layer — `intelligence/models/drivers/_inline_think.py`, used by `openai_chat`. Handles closed tags, the dropped closing tag (ends at the first run of three newlines, so the answer is never swallowed), thinking before a tool call, and a reply that is all unclosed thinking (handed back, not lost). Tested under every chunking and through the real driver over a real stream; `keep_inline_thinking` quirk turns it off. Root cause: the gonkarouter gateway answers its DeepSeek entry with MiniMax-M2.7 about half the time, which writes reasoning into `content` and loses `</think>` when tools are declared. Known limit: a second `<think>` later in the same reply stays as text. |

**Stage 3 (the checker) — implemented, scripted tests thorough, live proof NOT yet clean.**

| Commit | What |
|---|---|
| `e81e4d4` | `background/jobs/checking.py`: `evidence` (record of what ran, built by code from the trace), `findings` (no-model facts: outward action left failed / unsure / done twice), `judge` (ONE model call, each line judged against the record, told to trust it over the worker's report), `check` → `verified` / `partly` / `unmet` / `unchecked`. Unmet lines go back to the worker (only those) while `policy.may_recover(job, "check")` allows, then the result is **delivered labelled, never held as stuck**. Verdict kept on the ledger (`objective.set_check`, replay-safe via `of`), in a `check` trace row, in the notice (`detail.check`, `detail.checked`), in the prompt and in speech (`checking.aloud`). **Redo gate** (`objective.refuse_redo`): an outward action that already succeeded in this attempt is refused once ("already done"); asked for again it runs — so wanted repeats stay possible. Decided by operation id (`TurnRequest.gate` now receives it), so a replayed round gets the same answers and a restart redoes everything. |
| `0a39a4a` | Scorer reads the check; "delivered as verified while the world says otherwise" is a hard-rule violation. New scenarios `checked`, `partial`, `trap_bg`. |
| `b2b99bd` | Line 1 of every check is **the request itself** (a loosely written "a report is filed" line had let an honest "there are no July invoices" job be delivered as "all done"). A line the worker's own report admits was not done (`admitted`) is not sent back. |
| `6c5f006` | Two old tests updated (`test_ops` check seam; `test_routes_jobs` — work now waits in line, only a full line is a 409). |
| `881e3e7` | Leaked tool-call markup (`</｜DSML｜tool_calls>`) is stripped from job notes/results (`objective.without_tool_markup`); a junk note no longer replaces the last real one. |
| `a2ef3a9` | The same LOW-risk lookup returned twice in ONE reply runs once (the provider doubles tool calls about every other reply; the doubles were being read as "going round in circles"). Never applied to actions. |
| *(this commit)* | `checking.figures`: **totals are re-added by code.** A total stated over an itemised list in anything filed/sent → `adds_up` or `sum_differs` (blocks `verified`, shown to the judge, named in the label); a total with nothing itemised is confirmed only if it equals the sum of every amount the work read; the worker's own prose can confirm but never contradict (it raised false alarms on correct work in a third of real reports it was tried on). The judge is told a figure nobody re-checked is `unknown`. |

Test counts at the last run: `test_checking.py` 39 · job/proving/executive/followthrough/ops/architecture set 230 passed.
Full non-browser suite last run at `b2b99bd`: 2183 passed, 6 failed (4 Windows-only + the 2 fixed in `6c5f006`). **It has not been re-run since** `881e3e7`, `a2ef3a9` and the figures commit.

### Real-model evidence so far for Stage 3

- `s3a`/`s3b`/`s3c` (on `0a39a4a`…`b2b99bd`): `checked` passed 2 of 4 (the two "failures" did the small task directly in the chat instead of the background — correct result, but no job to check; see open issue 3) · `partial` passed after `b2b99bd` (March verified from the record; July stopped and said why) · `trap_bg` passed twice (honest stop via `cannot_finish`).
- Regression `s3r` on `6c5f006`: background ✔ · kill ✔ · three ✔ (all three delivered `verified`) · detour ✔ · dead_end ✔ · **marathon ✘**.
- Marathon since then: `s3m` 1 of 2, `s3n` (on `a2ef3a9`) **0 of 2 finished + 1 unknown** — and one of them is the important one:

**A hard-rule violation was caught by the scorer** (`s3n/marathon-1`): the job read all 34 records, listed 33 amounts, filed a total 3,400 out, and the check delivered it as *"all of it is done"*. Cause: the check confirmed a report was filed and never checked the sum. `checking.figures` (this commit) is the fix, and it catches that exact report (`sum_differs`). **It has not been proven live yet.**

Real model calls spent this session ≈ 1,100 (running total ≈ 1,750). Almost all on `gonkarouter/deepseek-ai/DeepSeek-V4-Flash-0731`.

## Decisions in force (do not re-ask)

1. One objective record; migration must preserve existing functionality.
2. Unattended/background work may fall back to another connected model.
3. Responsibilities are allowed only inside what the owner approves.
4. Test with both scripted tests and real models at every stage; as many real calls as reasonably necessary, no waste.
5. Specialist background jobs approved like ordinary jobs, HIGH still protected — **done**.
6. Stage 3 design approved in principle; evidence checks, partial completion, recovery and duplicate prevention must be thoroughly tested, and **legitimate repeated actions must remain possible when explicitly requested** — implemented as "refused once, runs when asked again".
7. `<think>` fix at the model layer — **done**.
8. **Do not change the owner's model settings** before presenting the proposed cleanup (below).
9. Standing: keep the memory guard on; free memory safely without closing any Claude session or unrelated work; after each live test report actual results, model-call count, failures and evidence from persisted records; never call anything proven early; push back with evidence rather than agree automatically; commit and push after each verified chunk; never restart or kill the owner's running Jarvis; never touch real `data/`, `.env` or port in tests.

## Exact next steps

1. **Re-run the full non-browser suite** on the current head (command above). Expect only the 4 Windows-only failures.
2. **Prove `checking.figures` live.** Move `Jarvis-001-live` to the head, then run
   `--only marathon --repeat 3` and `--only checked,three,detour`. Read each run's check from
   `jobs.plan` → `check` (verdict, lines, facts) and compare with `facts.json`. What must hold:
   **no run is delivered `verified` with a wrong total** (zero "delivered as verified…" violations),
   and correct runs are `verified` (via `adds_up`) or honestly `partly` ("could not be confirmed").
   Watch for a *false* `sum_differs` on a correct filed report — the parser was tuned on ~12
   real reports; if a new shape trips it, add that shape to `test_checking.py`'s cases and fix
   `_split`, never loosen it into silence.
3. **Marathon reliability (open issue 1)** — investigate before more live spending. In the two
   unfinished `s3n` runs the job read all 34 records and then re-read them (79 reads, 34 distinct)
   until its allowance ran out, with `found: 0` on the ledger. Hypotheses to check in the saved
   run folders *first*: the worker never writes amounts to the ledger (`update_plan`), so each
   round's only memory is its end note; the round-end note sometimes lacks the running total.
   Likely fixes to evaluate: have code keep the successful reads' key facts for the worker
   (it already lists what ran — `objective._activity` — but not what came back), or make the
   "what you have found" section fill from recorded read results when the worker did not.
   Only then re-run marathon ×3.
4. Update docs for what changed after `b205aa3`: `background/jobs/CLAUDE.md` (`checking.py`
   section: the request as line 1, `admitted`, `figures`/`adds_up`/`sum_differs`, markup
   stripping) and the root `CLAUDE.md` executive bullets (same-reply duplicate lookups).
5. **Present to the owner, then stop for their word:**
   - Stage 3's evidence (pass counts per scenario, model calls, the violation that was found and fixed).
   - The **Auto model-settings cleanup proposal** (approved item 5 — investigate and present, do
     not apply). Findings, from a read-only audit on a copy of `data/models.yaml` (the owner has
     already removed the dead `omniroute` gateway themselves; `aliases: {}` = Auto is on):
     Auto now ranks 178 endpoints — **Gemini's 62 first**, then GPT (127 discovered though 4 are
     configured) and Claude (14) — and all three accounts refuse for billing/quota; only
     `gonkarouter` (3 models) answers. So every cold call burns failed attempts before reaching
     the one provider that works. Proposal: (a) add a `default` route naming the three
     gonkarouter models first (DeepSeek → GLM → MiniMax) with `allow_others: true`, changing
     nothing else; (b) optionally disconnect providers with no credit, the owner's call;
     (c) tell them the gateway facts: its DeepSeek entry is answered by MiniMax about half the
     time, it caps replies at 3,072 tokens (a model that thinks that long returns nothing), it
     returns tool calls twice about every other reply, and it leaks DSML tool markup into text in
     roughly one reply in ten — all now handled in Jarvis, none fixable from Jarvis.
   - Whether an explicit larger reply limit helps on that gateway is **untested** (the probe got
     a 502); a single direct call can settle it when the provider is healthy.
6. Then, with approval: Stage 4 (subtasks with join + need-based routing), 5, 6.

## Open issues, honestly

1. **Marathon (34-step) is not reliable on the real model**: 2 passes in 6 attempts on Stage 3
   code. Failures were honest stops or (once, now addressed) a wrong total — never a silent false
   "done" after the figures fix, but that fix is unproven live.
2. `checking.figures` is a heuristic over text: it can only find a wrong sum where amounts are
   itemised in what was filed or sent. A wrong total with nothing itemised and no match to the
   reads is delivered as "could not be confirmed", not as wrong.
3. Asked to "work on it in the background", the model sometimes does a small task directly in
   the chat (2 of 4 `checked` runs). Result correct; the instruction was not followed; a chat
   reply gets no check. Not fixed — arguably fine for a five-call task; the owner should decide.
4. Chat turns are not gated against duplicate outward actions and get no completion check
   (jobs only). Deliberate for now.
5. A job and the chat (or two jobs) doing the same outward action is not prevented — the redo
   gate is per job. Belongs to Stage 4.
6. Leaked DSML markup is only cleaned from job notes/results, not at the driver; a whole tool
   call leaked as text is lost (the reply ends as words). Emulated tool calling is "not
   implemented" by project rule; raise it with the owner before touching.
7. `tests/test_durable_crash.py` can leak `durable_kill_child` processes when it fails before
   its kill (needs a `finally`).
8. The Jobs screen does not show the brief, plan or check verdict (front end belongs to the UI
   session; coordinate, do not edit). The UI session may ask for a `StepEnded(message_id)` event
   in `pipeline.py` — agreed to add it on this branch when they ask.
9. `--auto` proving runs and 3-of-3 consistency repeats are still unmeasured for most scenarios.

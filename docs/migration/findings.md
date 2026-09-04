# Findings from the Next.js + FastAPI migration

Things noticed while porting that are about the EXISTING Node app, not about the
port. Recorded here rather than fixed: the migration's ground rules keep
`server/` untouched for the duration of the build, and none of these are caused
by the port. Each one does change what the Python version should do, though.

---

## 1. Two control-subsystem stores ignore `JARVIS_DATA_DIR`

**Files:** `server/control/recording-store.js:15`, `server/control/screenshot-store.js:20`

```js
const RECORDINGS_DIR = process.env.JARVIS_RECORDINGS_DIR || path.join(__dirname, '..', '..', 'data', 'recordings');
const SHOTS_DIR      = process.env.JARVIS_SCREENSHOTS_DIR || path.join(__dirname, '..', '..', 'data', 'screenshots');
```

Both build their path from `__dirname` and honour only their own dedicated env
var. Neither honours `JARVIS_DATA_DIR`, and neither goes through `store.js`'s
`dataDir()`.

**Consequence:** a test run isolated the documented way — `JARVIS_DATA_DIR`,
`JARVIS_ENV_PATH` and `PORT` all pointed at scratch — still writes screenshots
and screen recordings into the user's REAL `data/` directory. Observed live
during this migration: starting the scratch server with `JARVIS_DATA_DIR`
pointed at `/tmp` still created `data/recordings/` and `data/screenshots/` in the
project directory.

This is the exact bug class the root `CLAUDE.md` already warns about ("A module
that hardcodes a path relative to its own source file (`__dirname`) bypasses this
entirely — use `store.js`'s `dataDir()` export for any new `data/` subdirectory
instead"), and the same one that previously put a 145MB browser profile into the
real data directory. These two are live instances of it that the warning did not
catch.

It also means the project's "Testing must never touch the user's real
`data/`/`.env`/port" rule has a hole in it today, and the pre-merge validation
gate's teardown step would not notice.

**For the port:** `jarvis/control/recording_store.py` and `screenshot_store.py`
must derive their directories from `store.data_dir()`, keeping the dedicated env
vars only as an explicit override on top. Do not reproduce the `__dirname` form.

**Not fixed here** because it is a behaviour change in `server/`, outside the
stack migration's scope. Worth fixing in the Node app directly if that app is
going to keep running for a while.

---

## 2. Two `:id` routes return 200 for an id that cannot exist

**Routes:** `GET /api/profile/:id/versions`, `GET /api/memories/:id/versions`

Every other `:id` GET returns a clean 404 for a nonexistent id. These two return
200 with an empty version list instead.

Not necessarily wrong — "this thing has no versions" is a defensible answer — but
it is inconsistent with the rest of the surface, so it is recorded here to make
clear the Python port reproduces it **deliberately**. The contract fixtures
capture the 200, so the port will be held to it. If the behaviour should change,
change it in both implementations at once and re-record, rather than letting the
two drift.

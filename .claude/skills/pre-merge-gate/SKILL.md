---
name: pre-merge-gate
description: Pre-merge validation gate for Jarvis. Use before merging any branch into main — runs the syntax sweep, fresh-install boot, migration check, route smoke test and full test suites against a scratch instance.
---

# Pre-merge validation gate

Run this against a scratch instance — never the user's real port or data — before
merging any branch into `main`, and report each result plainly rather than summarising
as "passed":

1. **Syntax sweep** — `python -m compileall backend/jarvis` and `cd frontend && npm run
   typecheck`. Zero failures.
2. **Fresh-install boot** — start the real `jarvis.main` in the background
   (`run_in_background: true`) with `JARVIS_DATA_DIR`/`JARVIS_ENV_PATH` pointed at empty
   scratch paths and an unusual `PORT`. Must come up with no unhandled exception.
3. **Migrations + tool loader** — read the scratch `jarvis.db` read-only and confirm
   `PRAGMA user_version` reached 32: `jarvis/migrations.py`'s `MIGRATION_SQL` (19,
   the original schema, never hand-edited) plus
   `migrations_extra.py`'s `EXTRA_MIGRATION_SQL` (13, this build's own — the latest
   being migration 32, Content Management without its accounts table, with
   per-platform media and reported numbers). Counting only
   the first file gives 19 and a false failure — the gate caught exactly that mistake
   in this document. **The database is created lazily on first use**, so hit a route
   before looking for the file. Confirm `load_tools()` (or a route that touches the
   capability registry) succeeds with no import error — currently 72 tools across 37
   modules; a real launch's own startup log is not a reliable place to see the count,
   since nothing in this project configures root logging by default and `jarvis.*`
   loggers have no handler attached unless something else in the process added one.
4. **Route smoke test** — `curl` a real GET (200), a route taking an id with a
   nonexistent one (a clean 404, not a crash), and `/` (the real `index.html` from the
   static mount).
5. **Any recently-fixed security behaviour** — re-confirm it live rather than by reading
   the code, e.g. `GET /api/artifacts/:id`'s forced-download headers on a plain request.
6. **The full suites** — `pytest tests -q` and the Playwright files, run as the two
   separate commands in CLAUDE.md's Testing section, both green.

Teardown: stop the scratch server by the PID actually bound to the scratch port, delete
the scratch directory, and confirm the user's real instance/port is unaffected.

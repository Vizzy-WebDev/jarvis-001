# Content Management (`jarvis/content_manager/`)

Finished content taken from review to publication — the screen is "Content Management" in
the drawer (`frontend/components/screens/ContentScreen.tsx`, parts in
`frontend/components/content/`). **It manages content and its workflow; it does not make it,
and it models no accounts and no publishing provider.** Research, scripts, generation and
editing belong to the agents and tools that hand content in; which account a post goes out
on belongs to the publishing tool. Not to be confused with `jarvis/content/` (Content
Analysis — older and unrelated; it owns `data/content.json` and the `*_content` tool names).

`kinds.py` (content types, platforms, stages, metric names — the one registry) · `store.py`
(every read, plus "what next / who", "needs you", the stage views and analytics) ·
`lifecycle.py` (every write) · `files.py` (media on disk) · `revise.py` (Jarvis doing a
revision as a Background Job). Routes: `jarvis/routes/content_manager.py`. Tools:
`jarvis/tools/content_manager_tools.py`. Schema: migrations 31 and 32 (`cm_*` tables).

## The model

- **One door in, one pipeline.** Content enters through `lifecycle.submit()` whoever brings
  it: the person on the screen ("New content", with their own uploads — the same multipart
  `POST /api/content-items` agents use), Jarvis (`submit_content_for_review`, which also
  takes a file the person attached in chat, copied in because chat uploads are pruned), or
  an agent over HTTP. Every later action is the same function for everyone. `producer` is a
  label ("you", "Jarvis", "ClipBot") used for display and for where a change request goes;
  nothing else branches on it. "Ready to Post" at creation (`approve_now`) is submit followed
  by the ordinary `approve()` — the screen offers it, and the local API accepts it from any
  caller just as it accepts `/approve`; no Jarvis tool can pass it (tested).
- **An item is the publishable thing** — the video, the carousel, the post's text. Title,
  description, caption, hashtags and tags are its *supporting fields*; thumbnail, cover and
  attachments are its *supporting assets*. Which show is decided by the content type; a
  platform version shows only the fields that platform uses (`platform_fields`). Adding a
  type or a platform is one entry in `kinds.py`. Video is Video whatever its shape — 9:16 or
  16:9 is a property of the file, and nothing here converts media.
- **A placement** is one platform (plus optional "where on it": Shorts, Reels, a board) with
  its own status, schedule, result and — only when wanted — its OWN version: text
  (`overrides`, blank = the item's) and files (`media_json`, **per role**: a platform with a
  thumbnail of its own uses it, and the item's video; one with nothing of its own shares
  everything). `store.merged_version()` / `merged_media()` are what a publisher is handed.
  Files belong to the item (`cm_files.item_id`), so Delete forever still removes them all.
- **Stage** is `review | changes_requested | approved | scheduling | published | archived`.
  "Approved" and "Ready to Post" are one state. **After approval the stage is derived from
  placements** (`lifecycle._derive`): anything pending → Scheduling; else anything published
  → Published; else Ready to Post. That is the item's headline stage; **the LISTS show an
  item under every stage one of its platforms is in** (`store._VIEW_SQL`: draft/failed →
  Ready to Post, scheduled/queued/publishing → Scheduling, published → Published), and
  `summary()["posts"]` counts platform posts per view. So nothing live on TikTok is missing
  from Published because YouTube is still scheduled.
- **Editable until it has gone out** (`store.editable`): text and files can change from
  Review until everything is published — a scheduled post is still a plan, and the change
  goes out with it. Not while a post is queued/being posted (the publisher may hold the old
  version), not in Changes Requested (someone is revising), not archived or in the bin. An
  edit after ANY platform published becomes a new revision, so what went out stays on record.
- **The recycle bin is `deleted_at`, separate from `stage`**, so restore puts an item back
  exactly where it was. It is never emptied automatically. Delete forever removes the rows
  (children cascade) AND the files.
- **Niche** is a free-text label and a filter/search dimension over the one workflow.
- Revisions are snapshots (`cm_revisions`), the history is `cm_events` (a plain timeline).
- **Reported numbers** are dated snapshots per published placement (`cm_metrics`), the
  latest also kept on the placement (`metrics_json`) for cheap lists. Numbers only, names
  from `kinds.METRICS` where known, any other reported number under its own name. Never
  calculated, never zero-filled: a post nobody reported shows none. `store.analytics()`
  totals only what was reported.

## Who may do what

- **The person, on the screen**: add content (Review or Ready to Post), edit text and files,
  approve, request changes, hand in a revision, schedule/reschedule/cancel, post now, "I
  posted it myself", record numbers, archive/unarchive, delete/restore/delete forever.
- **Agents, over the local API**: submit, pick up a change request, submit a revision, claim
  a queued post, report its result, report numbers.
- **Jarvis's tools**: `submit_content_for_review`, `submit_content_revision`,
  `list_content_change_requests`, `content_status` and `record_content_metrics` (LOW), plus
  `edit_content_item` and `schedule_content` (MEDIUM — they change what may go public, so the
  person confirms in the conversation). **No tool can approve, publish, archive or delete**;
  `test_content_manager_tools.py` asserts the list and the risks. The hand-in tools are LOW
  on purpose: they only put a draft in front of the person in Review, which IS the
  confirmation, and they must run inside a Background Job (Jarvis revising).
- **Nothing on a clock.** A scheduled post whose time has come simply appears in the publish
  queue (`store.publish_queue()`); no background thread, no interlock.
- Archiving or deleting cancels scheduled posts first (the screen confirms). Neither is
  allowed while a post is mid-publish.

## The local API (127.0.0.1 only, like every Jarvis route)

Errors are `{"ok": false, "error": "<plain sentence>"}` with 400, or 404 when a thing is gone.

- `GET /api/content-meta` — types, fields, assets, platforms, stages, metric names, niches.
- `POST /api/content-items` — hand in content. JSON, or multipart with an `item` field (JSON)
  plus the files. A media entry names an uploaded file by its field name or filename:
  ```json
  {"name": "3 Squat Mistakes", "contentType": "video", "niche": "Fitness", "producer": "ClipBot",
   "fields": {"title": "...", "caption": "...", "hashtags": ["#squat"]},
   "media": [{"file": "clip.mp4", "role": "primary"}, {"file": "thumb.png", "role": "thumbnail"}],
   "findings": [{"level": "warning", "text": "Hook lands late"}],
   "platforms": [{"platform": "tiktok"}, {"platform": "youtube", "destination": "Shorts"}]}
  ```
  Or upload first with `POST /api/content-files` and use `{"fileId": "cmf_…"}`. A refused
  submission leaves no stray files behind. (`readyToPost: true` adds it straight to Ready
  to Post, the same as a submit followed by `/approve`; open to any API caller, never to a
  Jarvis tool.)
- `GET /api/content-items?stage=…[&limit=&offset=]` — with `limit`, one page plus `total`.
  `q` searches names, niche, producer and field VALUES (never the stored JSON's key names).
- `PATCH /api/content-items/{id}` and `PATCH /api/content-placements/{id}` — JSON or
  multipart (same shape as handing in); `media` is the full new list (for a placement, its
  own files only; `[]` goes back to shared).
- `GET /api/content-change-requests[?assignee=agent]` — open requests, each with the item's
  current fields and media. `POST /api/content-change-requests/{id}/pick-up {"by": …}`.
- `POST /api/content-items/{id}/revisions` — `fields` = only what changed, `media` = the full
  new list if media changed, `note`, `by`. Back to Review.
- `GET /api/content-publish-queue` — every post to make now: platform, destination, the
  merged platform version and merged media URLs. `POST /api/content-placements/{id}/claim
  {"by": …}` (exactly one claimant wins), then `POST …/result {"ok": true, "url": …}` or
  `{"ok": false, "error": …}`. A publishing tool connected later fits here unchanged; the
  account it posts from is its own configuration.
- `POST /api/content-placements/{id}/metrics {"metrics": {"views": 1200}, "capturedAt"?, "by"?}`
  (published posts only); `GET` the same path for its history; `GET /api/content-analytics`.

Media is served by `GET /api/content-media/{fileId}` with the forced-download headers
(`attachment`, `nosniff`, sandbox CSP), unconditionally; the screen plays it through
`<img>`/`<video>`/`<audio>` only — never an iframe, never a navigation.

## Testing it busy

`tests/content_seed.py` builds a realistic, busy Content Management through the real
lifecycle (ten niches, every format, several platforms each, every stage, six timezones,
revisions, failures, numbers). `test_content_volume.py` checks every count against an
independent recount, search, paging, speed and a real restart; `test_content_concurrency.py`
races agents, publishers and the person over real HTTP with an invariant checker;
`tests/content_visual_tour.py` (not a test) screenshots every stage and dialog for a person to
look at. **Every test here must run on a scratch data dir** — a test file without the
`scratch` fixture writes into the real project `data/` (it happened once).

## Gotchas

- **Timezones are converted in the browser**, with `Intl` (`components/content/format.ts`):
  Python on Windows has no timezone database, so the server only stores and compares UTC
  instants plus the zone's name. `normalize_time` refuses a naive time.
- A form post with no files arrives as `application/x-www-form-urlencoded`, not multipart —
  both are read as a form. The front end's `request()` must not label a FormData body JSON.
- The shared SQLite connection is used from many threads; every multi-statement write runs
  in one transaction under this module's lock (`lifecycle._tx`).

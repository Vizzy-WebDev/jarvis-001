# Content Management (`jarvis/content_manager/`)

Finished content taken from review to publication — the screen is "Content Management" in
the drawer (`frontend/components/screens/ContentScreen.tsx`, parts in
`frontend/components/content/`). **It manages content; it does not make it.** Research,
scripts, generation and editing belong to the agents and tools that hand content in. Not to
be confused with `jarvis/content/` (Content Analysis — looking into something the person
shares; older and unrelated, and it owns `data/content.json` and the `*_content` tool names).

`kinds.py` (content types, platforms, stages — the one registry) · `store.py` (every read,
plus "what next / who" and "needs you") · `lifecycle.py` (every write) · `files.py` (media on
disk) · `revise.py` (Jarvis doing a revision as a Background Job). Routes:
`jarvis/routes/content_manager.py`. Tools: `jarvis/tools/content_manager_tools.py`.
Schema: migration 31 (`cm_*` tables).

## The model

- **An item is the publishable thing** — the video, the carousel, the post's text. Title,
  description, caption, hashtags and tags are its *supporting fields*; thumbnail, cover and
  attachments are its *supporting assets*. Which fields/assets show is decided by the content
  type, and a platform version shows only the fields that platform uses (`platform_fields`).
  Adding a content type or a platform is one entry in `kinds.py` — the screen reads the
  registry from `GET /api/content-meta` and hardcodes none of it.
- **A placement** is one platform/account/destination the item goes to, with its own
  platform-specific version (`overrides`, blank = use the base), schedule and result.
- **Stage** is `review | changes_requested | approved | scheduling | published | archived`.
  "Approved" and "Ready to Post" are one state (labelled Ready to Post). **After approval the
  stage is derived from placements** (`lifecycle._derive`): anything scheduled/queued/being
  posted → Scheduling; else anything published → Published; else Ready to Post.
- **The recycle bin is `deleted_at`, separate from `stage`**, so restore puts an item back
  exactly where it was. It is never emptied automatically — the person decided that; produced
  content can be expensive to remake. Delete forever removes the rows (children cascade) AND
  the files.
- **Niche** is a free-text label and a filter/search dimension over the one workflow. There is
  deliberately no niche table and no per-niche copy of any screen.
- Revisions are snapshots (`cm_revisions`), the history is `cm_events`. The current revision's
  snapshot is updated when the person edits in Review, so "revision 1" is what they actually
  reviewed, their edits included.

## Who may do what

- **The person, on the screen**: approve, request changes, schedule/reschedule/cancel, post
  now, "I posted it myself", archive/unarchive, delete/restore/delete forever.
- **Agents, over the local API**: submit, pick up a change request, submit a revision, claim
  a queued post, report its result.
- **Jarvis's tools** (`submit_content_for_review`, `submit_content_revision`,
  `list_content_change_requests`, `content_status`) — **no tool can approve, schedule,
  publish, archive or delete**, and `test_content_manager_tools.py` asserts that list. The two
  hand-in tools are Risk.LOW on purpose: they only put a draft in front of the person in
  Review, which IS the confirmation — and they must run inside a Background Job (Jarvis
  revising), where a MEDIUM tool parks the job waiting for a go-ahead nobody is there to give.
- **Nothing on a clock.** A scheduled post whose time has come simply appears in the publish
  queue (`store.publish_queue()`); there is no background thread, and no interlock to wire.
- Archiving or deleting cancels scheduled posts first (the screen confirms): a schedule that
  came due while an item sat in the bin would otherwise go out the moment it was restored.
  Neither is allowed while a post is mid-publish.

## The local API for agents (127.0.0.1 only, like every Jarvis route)

Errors are `{"ok": false, "error": "<plain sentence>"}` with 400, or 404 when a thing is gone.

- `GET /api/content-meta` — content types, their fields and assets, platforms, stages,
  niches in use, accounts.
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
  submission leaves no stray files behind.
- `GET /api/content-change-requests[?assignee=agent]` — open requests, each with the item's
  current fields and media. `POST /api/content-change-requests/{id}/pick-up {"by": "ClipBot"}`
  shows the person the agent is on it.
- `POST /api/content-items/{id}/revisions` — same shape as submitting (`fields` = only what
  changed, `media` = the full new list if media changed, `note`, `by`). Back to Review.
- `GET /api/content-publish-queue` — every post to make now: platform, account handle,
  destination, the merged platform version, media URLs.
  `POST /api/content-placements/{id}/claim {"by": …}` (exactly one claimant wins), then
  `POST /api/content-placements/{id}/result {"ok": true, "url": …}` or
  `{"ok": false, "error": …}`. A posting service connected later (an MCP connector, Composio)
  fits here unchanged: it is handed the platform, handle and destination; match the handle
  exactly as it appears on the platform.

Media is served by `GET /api/content-media/{fileId}` with the forced-download headers
(`attachment`, `nosniff`, sandbox CSP), unconditionally; the screen plays it through
`<img>`/`<video>`/`<audio>` only — never an iframe, never a navigation.

## Gotchas

- **Timezones are converted in the browser**, with `Intl` (`components/content/format.ts`):
  Python on Windows has no timezone database, so the server only ever stores and compares UTC
  instants plus the zone's name for display. `normalize_time` refuses a naive time rather
  than guessing which zone it meant.
- A form post with no files arrives as `application/x-www-form-urlencoded`, not multipart —
  both are read as a form (found by the route tests).
- The shared SQLite connection is used from many threads; every multi-statement write runs
  in one transaction under this module's lock (`lifecycle._tx`).

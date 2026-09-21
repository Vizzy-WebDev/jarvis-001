# Content Analysis (`jarvis/content/`)

`store.py` (leaf CRUD over `data/content.json`) · `intake.py` (the free glance, and
preparing content for a model) · `investigator.py` (the engine). The model-facing tools are
`tools/content_tools.py` (`share_content`, `examine_content`, `check_claim`) and
`tools/look_it_up.py`.

**Nothing is read or watched the instant it is shared, and there is no fixed output
shape.**

- **`intake.identify(source)` makes NO model call, ever.** Sharing something only works
  out what it IS — a title, a rough kind, a length or size — from cheap metadata (YouTube's
  oEmbed endpoint, a page's `<title>`, a file's size on disk). That lets Jarvis say "that's
  a 40-minute video on X — what do you want from it?" and then wait.
- **`investigator.examine(content_id, request)`** puts the user's own request into the one
  model call that does the looking. For expensive media (video/audio/image/PDF) the same
  call also produces a neutral `observations` note, cached in `material`, so a second
  question doesn't re-send the bytes. A follow-up first tries answering from that cache
  (one cheap text-only call); if the model reports `needsAnotherLook: true` because the
  notes don't cover the new question, it looks again for real. Text-shaped sources
  (articles, pasted text, plain documents) skip the cache and re-read the cached text.
- **`judge_claim()`** always runs research before a verdict, with no path around it — a
  model cannot decide it already knows and skip the lookup. Verdicts are `checks out |
  partly true | misleading | false | can't tell`, and the step-by-step breakdown is
  produced whenever the underlying activity is real, even if the claim about it is
  exaggerated.
- **`look_it_up`** is plain sourced research with no verdict, for "how does this tool
  work" questions that don't fit true/false. `check_claim` is not `meta`, so it is valid in
  a scheduled task or a briefing.
- **Findings and failures both land in the conversation** (`_push_finding()` /
  `_push_failure()`), so asking "so what did that turn up?" after a background job that hit
  a quota limit gets an honest answer instead of silence. On a free-tier account quota
  exhaustion mid-job is the normal case, not an edge case.

## Gotchas

**YouTube captions are not obtainable by scraping.** The watch page is a cookie-consent
wall without a consent cookie (sending `CONSENT=YES+...; SOCS=CAI` gets the real page), and
the caption-track JSON needs bracket-counting to parse (a regex truncates on nested arrays
in `name.runs`) — both handled in `intake.py`. But the `timedtext` URLs return **HTTP 200
with a zero-byte body** for every format, because they need a session proof-of-origin token
Jarvis doesn't have (verified 2026-08-04). So the no-video-model fallback is title +
description, recorded as `intake: 'youtube-text'`, and the UI says so. **Don't
re-investigate the parsing — the `timedtext` block is the real, current blocker.**

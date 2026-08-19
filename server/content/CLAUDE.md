# Content Analysis (`server/content/`)

`content-store.js` (leaf CRUD, `data/content.json`) · `intake.js` (the free
glance + preparing content for a model) · `investigator.js` (the engine).

**Rebuilt from scratch** to remove a defect that ran deeper than a bug: the
previous build read or watched *everything* the instant it was shared,
against one fixed template ("pull out claims worth checking: earnings,
timescales, guarantees, before-and-after results") — a fact-checking
schema applied to every photo, article, and document regardless of what was
actually asked. Two changes fix both the eagerness and the fixed shape:

- **`intake.js`'s `identify(source)` makes NO model call, ever.** Sharing
  something only works out what it IS — a title, a rough kind, a length or
  size — from cheap metadata (YouTube's oEmbed endpoint, a page's `<title>`,
  a file's size on disk). This is what lets Jarvis say "that's a 40-minute
  video on X — what do you want from it?" and then actually wait, rather than
  reading first and asking second.
- **`investigator.js`'s `examine(contentId, request)` has no fixed output
  shape.** The user's own request is what goes into the one model call that
  does the looking — there is no `claims[]`/`keyPoints[]`/`summary` template
  imposed on every request. For expensive media (video/audio/image/PDF) the
  same call also produces a neutral `observations` note, cached in
  `material`, so a second question about the same thing doesn't have to
  re-send the bytes. A follow-up first tries answering from that cache
  (one cheap text-only call); if the model reports `needsAnotherLook: true`
  because the cached notes genuinely don't cover the new question, it looks
  again for real rather than guessing from a stale note. Text-shaped sources
  (articles, pasted text, plain documents) skip the cache entirely and always
  re-read the full cached **text** fresh, since that's cheap.

`judgeClaim()` is unchanged in behaviour: `research()` runs unconditionally
before every verdict, with no path around it — a model cannot decide it
already knows and skip the lookup. Verdict values are still
`checks out | partly true | misleading | false | can't tell`, and the
step-by-step breakdown is still produced whenever the *underlying activity*
is real, even if the specific claim about it is exaggerated.

Four tools, not three: `share_content` (the free glance + ask),
`examine_content` (free-form follow-up, no `kind` routing), `check_claim`
(rebuild of the old `fact_check`, same research-first guarantee, not
`meta: true` — valid in a scheduled task or briefing), and **`look_it_up`**,
new — plain sourced research with no verdict attached, for "how does this
tool work" / "what does this typically cost" questions that don't fit a
true/false judgement. Before this, research was only reachable *through* a
verdict.

## Gotchas

**YouTube captions are not obtainable by scraping any more.** The watch page
is a cookie-consent wall without a consent cookie (sending
`CONSENT=YES+...; SOCS=CAI` gets the real page), and the caption-track JSON
needs bracket-counting to parse (a regex truncates on nested arrays in
`name.runs`) — both fixed. After both fixes the track list parses correctly,
but the `timedtext` URLs return **HTTP 200 with a zero-byte body** for every
format, because they now need a session proof-of-origin token Jarvis doesn't
have (verified 2026-08-04). So the no-video-model fallback is title +
description, recorded as `intake: 'youtube-text'`, and the UI says so.
**Don't re-investigate this from scratch — the parsing is already correct;
the `timedtext` block is the real, current blocker.**

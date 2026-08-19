# Planning Partner (`server/projects/`)

`project-store.js` (leaf CRUD, `data/projects.json`) · `assistants.js`
(assistant profiles as data, formerly `targets.js`) · `project-engine.js`
(the engine).

**Rebuilt from scratch** (not the same code as before) to remove a specific
defect: the previous build generated a fixed `questions[]`/`answers{}` queue
the instant an idea was mentioned, and everything the user said afterward —
including a challenge to the idea or a change of mind — got filed as "the
answer to question N." **There is no queue any more.** Talking through a
project is just conversation; `prompt.js` tells the model to ask whatever's
genuinely unclear, in its own words, one thing at a time, and to respond in
place to a challenge or tangent rather than advancing a stage. What a project
record keeps instead is `decisions[]` — one entry per thing actually settled,
added by the model calling `noteDecision()` as the discussion produces one,
not gathered as a batch.

Five functions, **each triggered only by an explicit tool call, and none of
them chains into another**:
`startProject` (creates the record — no model call, no background work),
`noteDecision` (appends to `decisions[]` — no model call),
`researchProject`, `writePlan`, `writePrompts`. The last three run in the
background and push `project_progress` over SSE, same reasoning as before
(a plan takes a minute or two, no HTTP request or spoken turn can be held
open that long) — but nothing advances on its own. Research finishing does
not trigger the plan; the plan finishing does not trigger the prompt.

`writePlan`/`writePrompts` both read `conversation.getMessages(sessionId)`
(`conversationContext()`) **and** `decisions[]` **and** the research —
`writePrompts` specifically fixes a bug in the old build, which read only the
finished plan document, so anything decided in conversation that hadn't made
it into the plan text was gone by the time the prompt was written.
`writePrompts` can produce **one prompt or several in sequence** (`prompts:
[{n, title, text}]`, `promptOrder` explaining the recommended order) — the
model decides based on the size of the project; the old build only ever
produced one, regardless.

`assistants.js` is plain data so adding a receiving AI is one entry; the
`custom` entry carries a user-supplied tool name, since the requirement was
explicitly that this not be a fixed list.

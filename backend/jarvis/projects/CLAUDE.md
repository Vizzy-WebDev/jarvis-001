# Planning Partner (`jarvis/projects/`)

`store.py` (leaf CRUD over `data/projects.json`) · `assistants.py` (receiving-AI profiles as
plain data) · `engine.py` (the engine). The model-facing tools are in `tools/project_tools.py`.

**There is no question queue.** Talking through a project is ordinary conversation:
`prompt.py` tells the model to ask whatever is genuinely unclear, one thing at a time, and
to respond in place to a challenge or a change of mind rather than advancing a stage. What
a project record keeps is `decisions[]` — one entry per thing actually settled, added as
the discussion produces one via `note_decision()`, never gathered as a batch.

**Five functions, each triggered only by an explicit tool call, and none chains into
another:** `start_project` (creates the record — no model call, no background work),
`note_decision` (appends to `decisions[]` — no model call), `research_project`,
`write_plan` and `write_prompts`. The last three run in the background and push
`project_progress` over SSE, since a plan takes a minute or two and no HTTP request or
spoken turn can be held open that long. Research finishing does not start the plan; the
plan finishing does not start the prompt.

- `write_plan` and `write_prompts` both read the session transcript
  (`_conversation_context()`), **and** `decisions[]`, **and** the research. Reading only the
  finished plan document would lose anything decided in conversation that never made it
  into the plan text.
- `write_prompts` can produce one prompt or several in sequence (`prompts: [{n, title,
  text}]`, with `promptOrder` explaining the order). The model decides from the size of
  the project.
- **Every background step lands in the conversation, success or failure**
  (`_push_step()`). The full document goes into history so a later "what did that say
  again?" works, and a failed step is recorded too, so the model can honestly say it
  failed. What the model says aloud is governed separately by each tool's
  `spoken_hint` — landing in history is not the same as reciting it aloud.
  `content/investigator.py` follows the same shape.
- `assistants.py` is plain data, so adding a receiving AI is one entry. The `custom` entry
  carries a user-supplied tool name, because the list is deliberately not fixed.

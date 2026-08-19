# How to use your upgraded Jarvis

Everything below is written for using Jarvis, not for editing it. Double-click
**Start Jarvis.bat** the same way you always have — nothing about starting it up
has changed.

---

## The menu (☰)

There's a **hamburger icon** (three lines) at the top-left of the screen — on the
main chat screen, and on every other screen too, so you can jump anywhere without
first going back to the chat. Click it (or say "open models", "open schedule",
"open briefing", or "open profile") to slide out a menu with everything beyond the
main chat:

- **Model Settings** — which AI models Jarvis can use, and how it picks between them
- **Skills** — the abilities Jarvis has, and ones you can add
- **Planning** — turn an idea into a plan and a ready-to-paste prompt
- **Content Analysis** — hand Jarvis a video, article or file and ask about it
- **Scheduled Tasks** — reminders and repeating things, including ones that use a
  specific ability of Jarvis's
- **Morning Briefing** — what your daily briefing includes
- **Profile & Goals** — what Jarvis knows about you

Click the **‹ back arrow** to return to the chat. Your browser's own Back button
also works, since each screen has its own address.

---

## 1. Adding and removing AI models

Open the menu → **Model Settings** → **"+ Add a model."** This opens a popup
(closing it with Cancel, ✕, Esc, or clicking outside all just cancel — nothing is
saved until you click the button that actually adds it).

**To add one specific model:**
1. Pick a **Type**:
   - **Google (Gemini)** or **Anthropic (Claude)** — cloud models. You'll need an
     API key (a password-like code from that company's website — the same kind of
     key Jarvis already asked for before).
   - **OpenAI-compatible** — covers OpenAI itself, and also anything running on
     your own PC like **Ollama** or **LM Studio**. For those, leave the API key
     blank and fill in the **Address** instead (Ollama's is usually
     `http://localhost:11434/v1`).
2. Type the exact **Model** name and click **"Test and add."** Jarvis checks the
   connection actually works before saving it — if something's wrong, it tells you
   what, in plain terms.

**To add several models at once from the same local server** (this is the
common case for Ollama/LM Studio, which usually have more than one model
installed): leave the **Model** field blank and click **"Test and add"** anyway.
Jarvis looks up what's actually available at that address and shows you a
checklist — tick the ones you want (or use "Select all"), then click **"Add
selected."** You only enter the address and key once, no matter how many models
you're adding.

**Your models are grouped by where they came from.** Each address/key you've
added is shown as its own group, with the models that share it listed underneath.
**"Add models"** on a group adds more to that same saved connection without
re-entering the address or key. **"Remove connection"** removes the whole group —
every model in it, and the saved key — in one action (click it twice to confirm).
Removing or disabling a single model, and testing its connection, still works the
same as before, per model.

---

## 2. Auto or manual — who picks the model

Still on the Model Settings screen, under "How Jarvis picks a model":

- **Pick automatically** (on by default) — Jarvis chooses the best model for each
  thing you ask: something fast and cheap for quick chat, something stronger for
  writing, code, or real thinking.
- **Balance** — nudges that choice: *Prefer fastest*, *Balanced* (recommended), or
  *Prefer best quality*.
- Turn **Pick automatically** off to lock Jarvis to one specific model yourself —
  do this from the **gear icon** on the main screen, in the compact "AI model"
  dropdown.

**If your chosen model breaks mid-conversation** (bad key, no internet, an outage),
Jarvis automatically tries your next-best available model, tells you out loud that
it switched and why, and keeps the conversation going without losing context. Once
your preferred model is working again, Jarvis quietly goes back to it on its own —
you don't have to do anything.

**If nothing at all can answer**, Jarvis says so plainly instead of guessing —
something like *"None of my models can handle this right now — check Model
Settings."*

---

## 3. Scheduling things — by voice or by screen

Just talk to Jarvis normally: *"Remind me to stretch every day at 7am,"* or
*"Every weekday at 8, run my morning briefing."* Because this changes something
lasting, Jarvis always **reads the schedule back to you first** and waits for your
yes — with **Yes/No buttons** on screen too, so a tap works as well as saying yes.
This is on purpose: it's the safety net against Jarvis mishearing "seven" as
"eleven."

To cancel or check on something later, just say *"what's on my schedule"* or
*"cancel my stretch reminder."*

Prefer clicking instead of talking? Open the menu → **Scheduled Tasks** →
**"+ New task."** This opens a popup where you set the title, how often it
repeats, and — new — exactly what Jarvis should do when it fires:

- **Just remind me** — Jarvis says a fixed message.
- **Run one specific skill** — pick one of Jarvis's real abilities (checking the
  weather, getting news headlines, reading a web page, opening an app or
  website, searching the web, or checking the time) and fill in whatever it
  needs (a place, a topic, a URL...). Jarvis runs it for real and notes what it
  found in the task's history.
- **Actually do something / answer something** — a free-text instruction, for
  anything more open-ended.
- **Run the morning briefing.**

For "run one specific skill" or the free-text option, a task fires with **nobody
there to answer a question** the way a live conversation could — so instead of
asking each time, Jarvis shows you **exactly what it's about to be allowed to do
automatically** right there in the popup, and you tick **"I understand"** once,
at setup, before you can create it. After that, it just runs on schedule; you can
always see what happened afterward in **Recent Activity**, including which model
handled it and whether it had to switch to a backup one.

You can also pick **which model handles this particular task** (instead of your
usual auto-pick) from the same popup — handy if you want a stronger model for one
recurring thing without changing your everyday default.

The list of tasks (create, turn on/off, run immediately, delete) and Recent
Activity are on the same screen. Anything that runs while Jarvis was closed is
clearly marked **"ran late"** the next time you open it, rather than running
silently or piling up.

---

## 4. The morning briefing

Open the menu → **Morning Briefing**. "What to include" still has the basics:
greeting, date/time, your upcoming reminders, your goals/notes, a suggested focus
for the day, and a custom note of your own.

**Live info sources** is where weather, news, and anything else Jarvis can check
live now lives — click **"+ Add a source,"** pick a skill (weather, headlines, or
any of Jarvis's other abilities), fill in its details (like a city for weather),
and it's added. Turn any source on/off, or remove it, anytime. You can add more
than one — e.g. weather for two different cities.

Click **"Preview my briefing now"** anytime to hear what it would say right now,
without waiting for it to run on schedule.

You can also just tell Jarvis in conversation: *"Add the weather in Lagos to my
morning briefing,"* and it'll confirm the change before saving it, same as
scheduling.

Calendar and email are shown as **"not yet connected"** — those need their own
separate Google sign-in step, which isn't part of this upgrade, but the slots are
there ready for later.

To actually get a briefing every morning, schedule one: *"Every weekday at 7, run
my morning briefing"* (or create a task with "When it runs, Jarvis should: Run the
morning briefing").

**One honest limitation:** Jarvis only runs while its window is open. A 7am
briefing scheduled for while you're asleep won't speak at 7am into an empty room
— it'll be waiting for you the next time you open Jarvis, clearly marked as
having run late.

---

## 5. Profile & Goals

Open the menu → **Profile & Goals** to see everything Jarvis has been told to
remember about you, add a note yourself, or delete one (click **Delete** twice to
confirm). You can also just say *"Remember that I'm working on..."* in
conversation — Jarvis will confirm before saving it. These notes personalize your
morning briefing and its suggestions; they're never sent anywhere else.

---

## 6. Planning something you want to build

This is for when you have an idea — a website, an app, a business, anything —
and you want it turned into a real plan, plus a prompt you can paste into
whichever AI is actually going to build it.

**Starting one.** Either just tell Jarvis (*"I want to build a website where
neighbours can book me to walk their dog"*), or open the menu → **Planning** →
**"+ Plan something."** Say as much as you can — rambling is fine and actually
helps. There's no need to sound technical.

**What happens next, in order:**

1. **Jarvis goes and researches it** — actually reads web pages about what
   building this involves, what it costs, and what usually goes wrong. This is
   real looking-up, not Jarvis guessing from memory.
2. **It comes back with a few questions** — usually three to six, and only the
   ones that genuinely change the plan. It won't ask you anything technical;
   those are its decisions to make, not yours.
3. **You answer** — on the Planning page there's a short form, with example
   answers you can click. Or answer out loud and Jarvis asks them one at a
   time. **Skip anything you're unsure about** — a skipped question is normal,
   and Jarvis just uses its best judgement there.
4. **You get two things**: a plan in plain English, and a ready-to-paste
   prompt.

**The plan** covers what you're building, what the first version should do
(and what to deliberately leave out), how it works, what it'll be built with,
the steps in order, what you'll need to provide, what it'll cost, and what
could go wrong.

**The prompt** is the useful bit. It's written specifically for whoever is
going to build it, because they each need very different things:

| If you'll use... | The prompt is written as... |
|---|---|
| **Claude Code** | File layout, numbered steps, and how to check each step worked |
| **ChatGPT / Claude chat** | A complete brief — it can't see your files, so everything is restated |
| **Cursor / Windsurf / Copilot** | Project rules plus a first specific change |
| **Lovable / v0 / Bolt / Replit** | Screens, what things look like, how it should feel |
| **Anything else** | Pick "Something else — I'll name it" and type the name |

Press **Copy prompt** and paste it wherever you're going. Changed your mind
about who's building it? Use **"Rewrite it for:"** at the bottom — that's quick
and doesn't redo the whole plan.

It takes a minute or two. You can leave the page — it carries on in the
background and tells you when it's done.

---

## 7. Getting Jarvis to look at a video, article or file

Open the menu → **Content Analysis**, or just send Jarvis something while
you're talking to it.

**Three ways to hand it something:**
- **Drag a file onto the page** (or click **Choose a file**) — video, audio, an
  image, or a document
- **Paste a link** — a YouTube video, or any article
- **Paste or dictate the text itself**

**It asks what you want.** This is deliberate. If you didn't say what you're
after, Jarvis reads the thing and then asks, rather than guessing you wanted a
summary. If you *did* say — *"is this guy legit?"*, *"fact-check the bit about
margins"* — it just gets on with it.

**Things you can ask for:**
- **Summarize it**
- **Is this realistic?** — for anything that smells like it's exaggerated for
  views. Jarvis goes and researches it independently, then gives you a verdict:
  *checks out*, *partly true*, *misleading*, *false*, or *can't tell*. It also
  tells you what's being left out. **And when the thing being described is
  actually real and doable, it gives you the honest step-by-step** — what's
  really involved, how long it takes, what it costs, and where most people give
  up.
- **Fact-check a claim** — the page lists the specific claims worth checking,
  each with its own **Check this** button
- **Save the key info** — the useful facts get kept with that item, on that
  page
- **Anything else** — just type the question

Every answer shows the **sources it checked**, so you can see for yourself.

**One important thing to know about videos.** At the top of every item, Jarvis
tells you *how it took the content in*, and this genuinely matters:

- **"Watched the video"** — a model actually watched it, including what was on
  screen. This is what you want. It needs a model that can handle video, which
  today means one of Google's Gemini models (see Model Settings).
- **"Only had the title and description"** — no video-capable model was
  available, so Jarvis is working from what the video *claims* to be. It'll say
  so plainly. A verdict based on this is much weaker, and Jarvis won't pretend
  otherwise.

Ask five questions about the same video and only the first one does the
expensive work — everything after that is quick, because Jarvis keeps what it
learned the first time.

---

## 8. When Jarvis double-checks what it heard

You'll notice three different levels of caution, on purpose:

- **Anything lasting** (scheduling, briefing changes, profile notes, adding/
  removing a model) — Jarvis **always** reads back exactly what it's about to do
  and waits for your yes, every time, regardless of how clearly it heard you.
  This is the layer that catches a mishearing that *sounds* perfectly reasonable,
  which is the dangerous kind.
- **Quick, harmless actions** (opening an app or website) — only double-checked
  if your voice was genuinely unclear.
- **Ordinary conversation** — Jarvis only stops to ask *"did you mean...?"* if
  what it heard doesn't fit what you were just talking about.

You can adjust how trigger-happy that second layer is from Model Settings →
**"Confirm unclear voice actions"**: Check more / Balanced / Check less.

---

## Quick reference

| Want to... | Where |
|---|---|
| Add or remove an AI model | Menu → Model Settings → + Add a model |
| Add several models from one local server at once | Model Settings → + Add a model, leave Model blank, use the checklist |
| Let Jarvis auto-pick, or lock one model | Model Settings, or gear icon on main screen |
| Set a reminder or repeating task | Just ask Jarvis, or Menu → Scheduled Tasks → + New task |
| Have a scheduled task use a specific ability | Scheduled Tasks → + New task → "Run one specific skill" |
| Pick which model handles one particular task | Same popup → "Model for this run" |
| Change what's in your morning briefing | Menu → Morning Briefing |
| Add weather/news/etc. to your briefing | Morning Briefing → Live info sources → + Add a source |
| Tell Jarvis something to remember | Just ask Jarvis, or Menu → Profile & Goals |
| Turn an idea into a plan and a prompt | Just tell Jarvis, or Menu → Planning → + Plan something |
| Get the same plan aimed at a different AI | Planning → open the plan → "Rewrite it for:" |
| Have Jarvis watch a video or read an article | Menu → Content Analysis, or send it a link while talking |
| Check whether something is realistic or hype | Content Analysis → open it → "Is this realistic?" |
| Fact-check one specific claim | Content Analysis → open it → "Check this" beside the claim |
| Adjust how often Jarvis double-checks you | Model Settings → Confirm unclear voice actions |

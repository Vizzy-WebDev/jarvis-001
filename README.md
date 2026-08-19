# Jarvis (v2)

Your personal voice assistant. Talk to it — or type to it — in your browser, and it
answers back, out loud, with real-time streaming replies. It can hold a conversation,
do a few real things on your computer, and now works with your choice of AI model.

## Before the first run

You need one free thing: a Google Gemini API key. This lets Jarvis "think" (and speak,
using Gemini's voices). It takes about a minute and doesn't need a credit card.

1. Go to **[aistudio.google.com/apikey](https://aistudio.google.com/apikey)**
2. Sign in with your Google account
3. Click **"Get API key"**, then **"Create API key"**
4. Copy the long string of letters and numbers it gives you (you'll paste it into
   Jarvis in a moment — no need to save it anywhere else)

## How to start Jarvis

1. Open the `Jarvis-001` folder
2. Double-click **`Start Jarvis.bat`**
3. Two things will happen:
   - A window titled **"Jarvis"** opens and stays open. **Leave this open** the whole
     time you're using Jarvis — closing it turns Jarvis off. You can minimize it.
   - Your browser opens automatically to the Jarvis page.
4. **The first time only**, Jarvis will ask you to paste in the API key from above.
   Paste it and click "Save & Continue." After that, it remembers it — you won't be
   asked again on future runs.

That's it. You should see the Jarvis screen with a big microphone button and a text box.

## How to use it

You can talk **or** type — whichever you prefer in the moment, even mid-conversation.

- **Type a message** and hit Send or Enter. Replies stream in as Jarvis "thinks," rather
  than appearing all at once after a wait.
- **Click the microphone button** (or press the **Space bar**) and start talking. By
  default, Jarvis stays listening so you can talk freely and pause naturally — it's
  smart about telling "you're thinking" pauses (like "um…") apart from "you're actually
  done talking."
- **Interrupt Jarvis** any time by just starting to talk again (or clicking the mic) —
  it stops immediately and listens.
- Your browser will ask for microphone permission the first time — click **Allow**.

### The gear icon (settings)

- **Speak replies out loud** — turn off if you'd rather just read replies silently.
- **Microphone mode** — "Conversation" (talk freely, hands mostly off the mic button)
  or "Push-to-talk" (click each time you want to speak, like v1).
- **Jarvis's voice** — Gemini's more natural voices, or your Windows voice (instant,
  offline, no setup).
- **Voice engine** — see below; this is the big one.
- **Extra-careful pause detection** — makes Jarvis double-check ambiguous pauses with
  an extra step before replying. Slightly slower, occasionally more accurate. Off by
  default.
- **AI model** — switch which AI answers you (see "Using a different AI model" below).

### Two ways Jarvis can talk to you

| | Any-model (default) | Gemini Live |
|---|---|---|
| Works with | Gemini, Claude, or OpenAI | Gemini only |
| Feel | Fast, ~1 second to first words | Near-instant, mid-word interruptible |
| Setup | Nothing extra | Nothing extra (still just your Gemini key) |

Try both from the gear icon and see which feels better to you — there's no wrong
choice, and you can switch anytime.

### Things you can try

- *"What time is it?"* or *"What's today's date?"*
- *"Open YouTube"*, *"Open my email"*, *"Open GitHub"*
- *"Open Notepad"*, *"Open Calculator"*, *"Open Paint"*, *"Open Spotify"*
- *"Search for the weather in Lagos"*
- *"Remind me to… um… call my brother"* — pause mid-sentence and see that it waits
  for you rather than jumping in.
- Or just talk normally — ask it questions, have a conversation. It remembers what
  you've said earlier in the same session.

## Using a different AI model

Jarvis works with Gemini out of the box (free, no card). If you'd rather use Claude
or OpenAI instead:

1. Open Settings (gear icon) → **AI model**
2. Pick Claude or OpenAI from the dropdown
3. Paste in an API key for that provider and click Save

Once saved, that provider becomes active immediately, and you can switch back and
forth anytime — each one remembers its own key. Note: Gemini Live (the faster voice
option above) is Gemini-only regardless of which model is answering your messages.

## Important: use Chrome or Edge

Voice input needs **Google Chrome** or **Microsoft Edge** — Firefox doesn't support
it. If you're on Firefox, Jarvis still works fully by typing; only the microphone is
unavailable.

## When you're done

Just close the **"Jarvis"** window (the one with the server running in it). Your
browser tab can stay open or be closed — it won't do anything without that window.

## If something goes wrong

- **"Could not reach the Jarvis server"** — the "Jarvis" window got closed. Re-run
  `Start Jarvis.bat`.
- **Mic button does nothing / no permission prompt appeared** — click the padlock icon
  in your browser's address bar, find "Microphone," and set it to Allow, then reload
  the page.
- **It rejects your API key** — double check you copied the whole key with no extra
  spaces. You can always generate a new one at the same link above.
- **"You exceeded your current quota" / "high demand"** — this is Google's side, not
  a bug: either a temporary capacity issue (just retry in a minute) or the free tier's
  daily limit. It resets on its own; the app will keep working once it does.
- **Setup screen keeps appearing** — this means no key has been saved successfully yet;
  just paste it in again.

## What this version can and can't do

**Can:** hold a real, low-latency conversation by voice or text, tell the time/date,
open websites, open a handful of common Windows apps, run a web search, switch between
AI models, and choose between two different voice-conversation engines.

**Can't yet:** listen for a wake word like "Hey Jarvis" (you still start it with a
click or Space), work offline, or remember past conversations after you close the
server.

## For later — how it's built (skip this if you're not curious)

- The **server** (`server/`) is a small Node.js program. It only runs on your own PC
  and isn't reachable by anyone else on your network.
- The **interface** (`public/`) is a plain web page — no fancy build tools, so any
  future edits are just "change the file, refresh the browser."
- **Commands** live in `server/skills/` as small, separate files. Each new ability you
  want later (e.g. "set a timer," "check the weather") is a new file in that folder —
  nothing else needs to change.
- **AI models** live in `server/providers/` (Gemini, Claude, OpenAI), all behind one
  shared interface — `server/brain.js` just calls whichever is active.
- **Voice conversation** has two independent engines behind one shared interface, in
  `public/engines/`: `pipeline-engine.js` (any model, browser speech-to-text, Gemini or
  Windows voice output) and `live-engine.js` (Gemini's own real-time voice API, proxied
  through `server/live.js` so your API key never reaches the browser).
- Turn-taking logic (deciding when you're actually done talking) is in
  `public/turn-detector.js`.

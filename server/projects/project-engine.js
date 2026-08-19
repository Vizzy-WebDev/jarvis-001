// The planning engine: rough idea in, MVP plan and ready-to-paste build
// prompt(s) out — but never on its own initiative.
//
// This replaces the old planner.js, and the difference is the whole point of
// the rebuild:
//
//   OLD — mentioning an idea immediately, in the background, researched it
//         and generated a fixed queue of up to six questions. Every reply
//         after that was filed as the answer to the next one in line —
//         including a challenge ("why do I need accounts at all?"), which
//         got recorded as an answer rather than responded to. The instant
//         the queue emptied, the plan wrote itself. No stage needed the
//         user's go-ahead.
//
//   NEW — five separate, dumb functions, each one triggered only by an
//         explicit tool call. Nothing here chains into anything else.
//         Starting a project makes no model call at all. Discussion happens
//         in the conversation itself, in Jarvis's own words — there is no
//         queue for a challenge or a tangent to be mis-filed into, because
//         there is no queue.
//
// `decisions[]` (project-store.js) is what makes the last two steps honest:
// the old build's final prompt was written from the plan document ALONE,
// so anything decided in conversation that didn't make it into the plan
// text was simply gone by the time the prompt was written. Every decision
// noted along the way is read by both writePlan and writePrompts.
//
// A leaf module: imports ai.js, research.js, events.js, conversation.js and
// assistants.js — never the skills loader, the runner, or a scheduler file
// (see CLAUDE.md's circular-import invariant).

import { askModel } from '../ai.js';
import { research, toSearchQuery } from '../research.js';
import { broadcast } from '../events.js';
import { guidanceFor, describeAssistant } from './assistants.js';
import * as store from './project-store.js';
import * as conversation from '../conversation.js';

// How much of the conversation to carry into a plan or a prompt. Enough to
// pick up something mentioned in passing, short enough not to crowd out the
// research and the decisions.
const CONTEXT_MESSAGES = 14;
const CONTEXT_MAX_CHARS = 6000;

/**
 * What's already been said, as plain text for a prompt. Read fresh on every
 * call — this is the one seam a future real memory/persistence system plugs
 * into without anything here needing to change.
 */
function conversationContext(sessionId) {
  if (!sessionId) return '';
  let messages;
  try {
    messages = conversation.getMessages(sessionId);
  } catch {
    return '';
  }
  if (!messages?.length) return '';

  const lines = messages
    .slice(-CONTEXT_MESSAGES)
    .filter((m) => (m.role === 'user' || m.role === 'assistant') && m.text)
    .map((m) => `${m.role === 'user' ? 'They said' : 'I said'}: ${m.text}`);
  if (!lines.length) return '';

  const joined = lines.join('\n');
  const clipped = joined.length > CONTEXT_MAX_CHARS ? `…\n${joined.slice(-CONTEXT_MAX_CHARS)}` : joined;
  return `What we've been talking about (use anything relevant here — do NOT make them repeat it):\n\n${clipped}`;
}

function decisionsBlock(project) {
  const list = project.decisions || [];
  if (!list.length) return '';
  return `Decisions already settled in conversation — treat these as fixed, do not re-ask about them or contradict them:\n${list.map((d) => `- ${d.text}`).join('\n')}`;
}

function announce(project, extra = {}) {
  if (!project) return;
  broadcast({
    type: 'project_progress',
    id: project.id,
    title: project.title,
    status: extra.status || 'working',
    error: project.error || null,
    ...extra,
  });
}

function runInBackground(work) {
  Promise.resolve()
    .then(work)
    .catch((err) => console.error('[project-engine] background job failed:', err));
}

// ---------- 1. start ----------

/** Creates the project. No model call, no background work — this is a notepad opening, not a process starting. `target` is optional and only ever set here if the user already named their assistant unprompted; it can just as well be set later, when writePrompts is actually called. */
export function startProject({ idea, sessionId = 'main', target = null }) {
  let project = store.createProject({ idea, sessionId });
  if (target) project = store.updateProject(project.id, { target }) || project;
  announce(project, { status: 'started' });
  return project;
}

// ---------- 2. note a decision ----------

/** Records something settled during discussion. No model call. */
export function noteDecision(id, text) {
  const project = store.addDecision(id, text);
  if (!project) throw new Error('That project no longer exists.');
  announce(project, { status: 'noted', decision: text });
  return project;
}

// ---------- 3. research ----------

/** Looks into what building this idea actually involves. User-triggered — never runs on its own. */
export function researchProject(id) {
  const project = store.getProject(id);
  if (!project) throw new Error('That project no longer exists.');

  runInBackground(async () => {
    announce(project, { status: 'working', step: 'research' });

    const found = await research(
      `What is involved in building ${project.idea}? Cover the practical steps, typical costs, and common mistakes.`,
      { searchQuery: `${toSearchQuery(project.idea)} cost how to build` }
    );

    const current = store.getProject(id);
    if (!current) return; // deleted mid-job

    if (found.ok) {
      store.updateProject(id, { research: { answer: found.answer, sources: found.sources, via: found.via } });
      store.recordStep(id, 'research', found.modelLabel);
    } else {
      store.updateProject(id, { research: { answer: null, sources: [], via: null, error: found.error } });
    }

    const done = store.getProject(id);
    announce(done, {
      status: found.ok ? 'ready' : 'failed',
      step: 'research',
      document: found.ok ? found.answer : null,
      sources: found.ok ? found.sources : [],
      error: found.ok ? null : found.error,
    });
  });

  return project;
}

// ---------- 4. write the plan ----------

const PLAN_SYSTEM = `You write first-version project plans for someone who is not a programmer and will hand the work to an AI to build.

Rules:
- Plain language throughout. If a technical term is unavoidable, explain it in the same sentence.
- Be decisive. Recommend one approach and say why, rather than listing options.
- Ruthless about scope: the first version should be the smallest thing that is genuinely useful.
- Concrete numbers where you can — real costs, real timescales. Say when you are estimating.
- Never invent facts about services or prices. If you are unsure, say so.
- Treat every settled decision you're given as fixed — do not silently override or re-litigate one.
- Use markdown headings and bullets. This is a document to read, not something spoken aloud.`;

function planPrompt(project) {
  const context = conversationContext(project.sessionId);
  const decisions = decisionsBlock(project);

  return `The idea:

"${project.idea}"

${context ? `${context}\n\n` : ''}${decisions ? `${decisions}\n\n` : ''}${
    project.research?.answer ? `Background research:\n\n${project.research.answer}\n\n` : ''
  }Write the plan with exactly these sections, in this order:

## What you're building
Two or three sentences. What it is and who it's for.

## The first version
What it must do (the short list), what can wait, and what you are deliberately leaving out.

## How it works
Walk through it in plain language, from the user's point of view.

## What it'll be built with
Your recommendation and one line on why. Name specific tools.

## Steps, in order
Numbered. Each step small enough to finish in one sitting.

## What you'll need to provide
Accounts, keys, content, decisions — anything that has to come from them.

## What it'll cost
Realistic figures, and say clearly which are estimates.

## What could go wrong
The few things most likely to trip this up, and what to do about each.`;
}

/** Writes the MVP plan document. Only ever runs when asked — never chains on from research or from questions being "done", because there is no such state here. */
export function writePlan(id) {
  const project = store.getProject(id);
  if (!project) throw new Error('That project no longer exists.');

  runInBackground(async () => {
    announce(project, { status: 'working', step: 'plan' });

    const written = await askModel({ system: PLAN_SYSTEM, prompt: planPrompt(project) });
    const current = store.getProject(id);
    if (!current) return;

    if (!written.ok) {
      const failed = store.updateProject(id, { error: `I couldn't write the plan. ${written.error}` });
      announce(failed, { status: 'failed', step: 'plan', error: written.error });
      return;
    }

    store.recordStep(id, 'plan', written.modelLabel);
    const done = store.updateProject(id, { plan: written.text, error: null });
    announce(done, { status: 'ready', step: 'plan', document: done.plan });
  });

  return project;
}

// ---------- 5. write the build prompt(s) ----------

const PROMPTS_SYSTEM =
  'You write prompts for AI assistants that will actually build something. Output only the prompt text itself ' +
  'in each "text" field — no preamble, no explanation, no surrounding quotes, no commentary about the prompt.';

function promptsPrompt(project, target) {
  const context = conversationContext(project.sessionId);
  const decisions = decisionsBlock(project);

  return `Here is a project plan:

${project.plan}

${context ? `${context}\n\n` : ''}${decisions ? `${decisions}\n\n` : ''}${
    project.research?.answer ? `Background research:\n\n${project.research.answer}\n\n` : ''
  }${guidanceFor(target)}

Decide whether this is small enough to hand over as ONE prompt, or whether it should be split into several
sequential prompts (Prompt 1, Prompt 2, ...) because it is too large or too risky to build correctly in one
step. Most small, well-scoped first versions need only one prompt — do not split needlessly.

Reply as JSON:
{
  "prompts": [
    { "title": "a short label for this prompt, 3-6 words", "text": "the exact prompt text, ready to paste" }
  ],
  "order": "if there is more than one prompt, one or two sentences on the recommended order and why each step depends on the last. null if there is only one prompt."
}`;
}

/**
 * Writes the handoff prompt(s) for the chosen assistant. Reads the plan,
 * every decision, the research, and the conversation — not the plan
 * document alone, which is what let a decision made in discussion vanish by
 * the time the old build wrote its prompt. Can produce one prompt or several
 * in sequence, depending on the size of the project.
 */
export function writePrompts(id, target) {
  const project = store.getProject(id);
  if (!project) throw new Error('That project no longer exists.');
  if (!project.plan) throw new Error("That project doesn't have a written plan yet — write the plan first.");

  const chosen = target || project.target;
  const updated = store.updateProject(id, { target: chosen });

  runInBackground(async () => {
    announce(updated, { status: 'working', step: 'prompts' });

    const written = await askModel({ json: true, system: PROMPTS_SYSTEM, prompt: promptsPrompt(updated, chosen) });
    const current = store.getProject(id);
    if (!current) return;

    if (!written.ok) {
      const failed = store.updateProject(id, { error: `I couldn't write the build prompt. ${written.error}` });
      announce(failed, { status: 'failed', step: 'prompts', error: written.error });
      return;
    }

    const rawPrompts = Array.isArray(written.data?.prompts) ? written.data.prompts : [];
    const prompts = rawPrompts
      .map((p, i) => ({ n: i + 1, title: String(p?.title || `Prompt ${i + 1}`).trim(), text: String(p?.text || '').trim() }))
      .filter((p) => p.text);

    if (!prompts.length) {
      const failed = store.updateProject(id, { error: "I couldn't produce a usable prompt from that — the plan is still ready on its own." });
      announce(failed, { status: 'failed', step: 'prompts', error: failed.error, document: current.plan });
      return;
    }

    store.recordStep(id, 'prompts', written.modelLabel);
    const done = store.updateProject(id, {
      prompts,
      promptOrder: prompts.length > 1 ? String(written.data?.order || '').trim() || null : null,
      error: null,
    });
    announce(done, {
      status: 'ready',
      step: 'prompts',
      document: done.plan,
      prompts: done.prompts,
      promptOrder: done.promptOrder,
      target: describeAssistant(chosen),
    });
  });

  return updated;
}

/** A one-line spoken summary of where a project is — used by voice, which must never read a whole document out loud. */
export function describeStatus(project) {
  if (!project) return "I can't find that project.";
  const name = project.title || 'your project';
  if (project.prompts?.length) return `${name} is ready, with ${project.prompts.length === 1 ? 'a prompt' : `${project.prompts.length} prompts`} for ${describeAssistant(project.target)}.`;
  if (project.plan) return `${name} has a plan written. Say the word when you want the build prompt.`;
  if (project.research) return `I've looked into ${name}. Ready to write the plan whenever you are.`;
  return `${name} is a work in progress — we're still talking it through.`;
}

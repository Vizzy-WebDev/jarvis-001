// Looking into content the user has shared, on their own terms.
//
// This replaces the old analyzer.js, and the difference is the whole point
// of the rebuild:
//
//   OLD  — the instant content arrived, it was read/watched in full and
//          boiled down into a fixed digest (summary, key points, and a list
//          of "claims worth checking" — a fact-checking template applied to
//          everything). Only after that did it ask what the user wanted.
//
//   NEW  — sharing something costs nothing (see intake.js's identify()).
//          Nothing is read or watched until the user says what they want,
//          and when they do, THEIR QUESTION shapes what gets extracted —
//          there is no fixed output shape imposed on every request.
//
// Two engine functions:
//
//   share(source, opts)             — free glance + store. No model call.
//   examine(contentId, request, opts) — the one real "look into this" call.
//
// Expensive media (video/audio/image/PDF) caches a neutral `observations`
// note the first time it's genuinely watched/seen, so a second question
// doesn't have to re-send the bytes — but a follow-up that the cached notes
// can't actually answer says so (`needsAnotherLook`) and looks again for
// real, honestly, rather than guessing from a stale note. Text-shaped
// sources (articles, pasted text, plain documents) are cheap to re-read and
// always read fresh from the cached full text, so they always adapt to
// whatever's now being asked.
//
// judgeClaim() also lives here — the one guarantee carried forward
// unchanged from the old build: research() runs before every verdict, with
// no code path around it, and a real step-by-step breakdown is produced
// whenever the underlying thing is real, even if the specific claim about it
// is exaggerated.
//
// A leaf module: imports ai.js, research.js, events.js, conversation.js,
// media.js and intake.js — never the skills loader, the runner, or a
// scheduler file (see CLAUDE.md's circular-import invariant).

import fs from 'node:fs';
import { askModel, pickModels, whyNoModel } from '../ai.js';
import { research, toSearchQuery } from '../research.js';
import { broadcast } from '../events.js';
import * as conversation from '../conversation.js';
import { fileKind, mimeTypeFor, fetchArticle } from '../media.js';
import { identify, describe, intakeDescription, prepareFor, fetchYoutubeText } from './intake.js';
import * as store from './content-store.js';

const TEXT_FILE_MAX_CHARS = 40000;

function announce(record, extra = {}) {
  if (!record) return;
  broadcast({
    type: 'content_progress',
    id: record.id,
    title: record.identity?.title || 'that',
    status: extra.status || 'working',
    error: extra.error || null,
    ...extra,
  });
}

// ---------- sharing: the free glance, and nothing more ----------

/**
 * Registers a piece of content and works out what it IS — no model call.
 * `instruction` is optional and, when given, is examined right away in the
 * background (the one case where "just ask" would be pointless: the user
 * already said what they want in the same breath they shared it).
 */
export async function share({ source, instruction, sessionId = 'main' }) {
  const identity = await identify(source);

  const material = identity.cachedText
    ? { text: identity.cachedText, intake: source.kind === 'url' ? 'article' : 'text' }
    : null;

  const record = store.createContent({
    source,
    identity: { title: identity.title, kind: identity.kind, detail: identity.detail, thumbnail: identity.thumbnail || null },
    sessionId,
  });
  if (material) store.updateContent(record.id, { material });

  announce(record, { status: 'shared', description: describe(identity), unreachable: identity.unreachable || null });

  const text = String(instruction || '').trim();
  if (text) examine(record.id, text, { sessionId });

  return { record: store.getContent(record.id), identity };
}

// ---------- examining: the one real "look into this" call ----------

const MEDIA_EXAMINE_SYSTEM = `You are looking at a piece of content on the user's behalf, to answer a specific question they just asked. Answer THAT question — do not produce a general summary unless they actually asked for one.

Rules:
- Answer only from what's genuinely in the content. Never add outside facts or guess at what wasn't shown or said — looking things up is a separate ability with its own tool, not yours here.
- If answering properly would require outside information (verification, current data, context this content doesn't contain), say so plainly in the answer instead of inventing it.
- Be specific — quote or closely describe what was actually seen or heard where it matters.
- Neutral, factual tone unless the question itself asks you to judge something.

Reply as JSON:
{
  "answer": "the actual answer, in markdown",
  "observations": "a thorough, neutral note on what you actually perceived — screen content, what was said, what was shown — broad enough that a completely DIFFERENT follow-up question later could be answered from these notes alone, without looking again. Not a summary for the user; your own working notes."
}`;

const CACHED_FOLLOWUP_SYSTEM = `You previously looked at a piece of content and took working notes. Answer the user's new question using those notes.

If the notes genuinely do not cover what's needed to answer this well, don't guess — say so.

Reply as JSON:
{
  "answer": "the answer, if the notes cover it — otherwise a brief note on what's missing",
  "needsAnotherLook": true or false,
  "why": "one short sentence, only if needsAnotherLook is true"
}`;

const TEXT_EXAMINE_SYSTEM = `You are answering a specific question about a piece of content the user shared, using the text given to you.

Rules:
- Answer only from the text given, and any research note also given. If the answer genuinely isn't in either, say so rather than filling it in.
- Answer the question actually asked — do not produce a general summary unless asked for one.
- Use markdown — this is read on screen, not spoken aloud.`;

function contextLine(record) {
  const id = record.identity || {};
  const desc = describe({ title: id.title, kind: id.kind, detail: id.detail });
  return `This is ${desc}${record.source?.url ? ` (${record.source.url})` : ''}.`;
}

function cacheObservations(id, observations, intake) {
  if (!observations) return;
  const record = store.getContent(id);
  if (!record) return;
  store.updateContent(id, { material: { ...(record.material || {}), observations, intake } });
}

function cacheText(id, text, intake) {
  const record = store.getContent(id);
  if (!record) return;
  store.updateContent(id, { material: { ...(record.material || {}), text, intake } });
}

/** True for a source whose content is genuinely text — cheap and safe to re-read on every question, so it's never cached as a one-shot "observation". */
function isTextShaped(source) {
  if (source.kind === 'url' || source.kind === 'text') return true;
  if (source.kind === 'file') {
    const kind = fileKind(source.filePath);
    if (kind === 'video' || kind === 'audio' || kind === 'image') return false;
    return mimeTypeFor(source.filePath) !== 'application/pdf';
  }
  return false;
}

/** The full text for a text-shaped source — from cache if present, otherwise read/fetched and cached for next time. */
async function textFor(record) {
  const cached = record.material?.text;
  if (cached) return { ok: true, text: cached, intake: record.material.intake || 'text' };

  const source = record.source;
  if (source.kind === 'url') {
    const article = await fetchArticle(source.url);
    if (!article.ok) return { ok: false, error: article.error };
    cacheText(record.id, article.text, 'article');
    return { ok: true, text: article.text, intake: 'article' };
  }
  if (source.kind === 'text') {
    cacheText(record.id, source.text || '', 'text');
    return { ok: true, text: source.text || '', intake: 'text' };
  }
  if (source.kind === 'file') {
    try {
      const text = fs.readFileSync(source.filePath, 'utf8').slice(0, TEXT_FILE_MAX_CHARS);
      cacheText(record.id, text, 'document');
      return { ok: true, text, intake: 'document' };
    } catch {
      return { ok: false, error: "I couldn't read that file — it may not be a text document." };
    }
  }
  return { ok: false, error: 'Nothing to read.' };
}

/** Watches/sees a YouTube video for real, falling back to captions/description if nothing can watch it. */
async function examineYoutube(record, request) {
  const candidates = pickModels({ need: { video: true } });
  let lastError = candidates.length ? null : whyNoModel({ video: true });

  for (const entry of candidates) {
    const prepared = await prepareFor(entry, record.source);
    if (!prepared.ok) {
      lastError = prepared.error;
      continue;
    }
    try {
      const result = await askModel({
        modelId: entry.id,
        only: true,
        json: true,
        media: prepared.media,
        system: MEDIA_EXAMINE_SYSTEM,
        prompt: `${contextLine(record)}\n\nQuestion: "${request}"`,
      });
      await prepared.cleanup?.();
      if (result.ok && result.data?.answer) {
        cacheObservations(record.id, result.data.observations, 'video');
        return { ok: true, answer: result.data.answer, intake: 'video', sources: [] };
      }
      lastError = result.error || lastError;
    } catch (err) {
      lastError = err?.message || lastError;
    }
  }

  // Nothing could watch it — fall back to captions/description, honestly.
  const fallback = await fetchYoutubeText(record.source.url);
  if (!fallback.ok) {
    return { ok: false, error: `${fallback.error}${lastError ? ` (${lastError})` : ''}` };
  }
  const parts = [];
  if (fallback.title) parts.push(`Title: ${fallback.title}`);
  if (fallback.author) parts.push(`Channel: ${fallback.author}`);
  if (fallback.description) parts.push(`Description:\n${fallback.description}`);
  if (fallback.transcript) parts.push(`Transcript of what is said:\n${fallback.transcript}`);
  if (!fallback.transcript && !fallback.description) {
    return { ok: false, error: "That video has no captions or description I can read, and I couldn't watch it." };
  }

  const intake = fallback.transcript ? 'captions' : 'youtube-text';
  const text = parts.join('\n\n');
  cacheText(record.id, text, intake);

  const answered = await askModel({
    system:
      TEXT_EXAMINE_SYSTEM +
      (fallback.transcript
        ? '\n\nNote: this is a transcript, not the video itself — you have NOT seen what was on screen. Say so if it matters to the answer.'
        : '\n\nNote: this is only the title and description — you have neither watched nor heard it. Be explicit in the answer about how little you actually have.'),
    prompt: `${contextLine(record)}\n\nContent:\n${text}\n\nQuestion: "${request}"`,
  });
  if (!answered.ok) return { ok: false, error: answered.error };
  return { ok: true, answer: answered.text, intake, sources: [], downgradedReason: lastError };
}

/** Watches/sees/listens to a local video, audio, image or PDF file for real. */
async function examineMediaFile(record, request) {
  const kind = fileKind(record.source.filePath);
  const isPdf = mimeTypeFor(record.source.filePath) === 'application/pdf';
  const need = isPdf ? { video: true } : kind === 'image' ? { vision: true } : kind === 'audio' ? { audio: true } : { video: true };

  const candidates = pickModels({ need });
  if (!candidates.length) return { ok: false, error: whyNoModel(need) };

  let lastError = null;
  for (const entry of candidates) {
    const prepared = await prepareFor(entry, record.source);
    if (!prepared.ok) {
      lastError = prepared.error;
      continue;
    }
    try {
      const result = await askModel({
        modelId: entry.id,
        only: true,
        json: true,
        media: prepared.media,
        system: MEDIA_EXAMINE_SYSTEM,
        prompt: `${contextLine(record)}\n\nQuestion: "${request}"`,
      });
      await prepared.cleanup?.();
      if (result.ok && result.data?.answer) {
        cacheObservations(record.id, result.data.observations, isPdf ? 'document' : kind);
        return { ok: true, answer: result.data.answer, intake: isPdf ? 'document' : kind, sources: [] };
      }
      lastError = result.error || lastError;
    } catch (err) {
      lastError = err?.message || lastError;
    }
  }
  return { ok: false, error: lastError || "None of your models could take that file." };
}

/** Tries to answer from cached observations first; only looks again for real if those notes genuinely don't cover the new question. */
async function examineFresh(record, request) {
  if (record.source.kind === 'youtube') return examineYoutube(record, request);
  return examineMediaFile(record, request);
}

async function runExamine(record, request) {
  if (isTextShaped(record.source)) {
    const text = await textFor(record);
    if (!text.ok) return { ok: false, error: text.error };
    const answered = await askModel({
      system: TEXT_EXAMINE_SYSTEM,
      prompt: `${contextLine(record)}\n\nContent:\n${text.text}\n\nQuestion: "${request}"`,
    });
    if (!answered.ok) return { ok: false, error: answered.error };
    return { ok: true, answer: answered.text, intake: text.intake, sources: [] };
  }

  // Expensive media: try the cached notes first, if there are any.
  const cachedObservations = record.material?.observations;
  if (cachedObservations) {
    const quick = await askModel({
      json: true,
      system: CACHED_FOLLOWUP_SYSTEM,
      prompt: `${contextLine(record)}\n\nWorking notes from when I looked at this:\n${cachedObservations}\n\nNew question: "${request}"`,
    });
    if (quick.ok && quick.data && !quick.data.needsAnotherLook) {
      return { ok: true, answer: quick.data.answer, intake: record.material?.intake || 'video', sources: [] };
    }
    // Notes didn't cover it (or the quick check itself failed) — look again for real.
    const fresh = await examineFresh(record, request);
    if (fresh.ok) fresh.lookedAgain = true;
    return fresh;
  }

  return examineFresh(record, request);
}

/**
 * Puts a finding into the conversation itself, the same join
 * pushDigestToConversation() provided in the old build — this is what lets
 * "does this change my plan?" work later, and what lets fact_check/
 * look_it_up reason about something Jarvis already looked into.
 */
function pushFindingToConversation(record, finding, sessionId) {
  if (!record || !finding) return;
  const body = [
    `(I looked into "${record.identity?.title || 'that'}" — they asked: "${finding.request}"`,
    `How I took it in: ${intakeDescription(finding.intake)}`,
    `\n${finding.answer}`,
    `\nReference id: ${record.id})`,
  ].join('\n');
  try {
    conversation.pushAssistantText(sessionId || record.sessionId || 'main', body);
  } catch (err) {
    console.error(`[investigator] could not add finding for "${record.id}" to the conversation:`, err);
  }
}

function runInBackground(work) {
  Promise.resolve()
    .then(work)
    .catch((err) => console.error('[investigator] examine failed:', err));
}

/**
 * Asks something about content already shared. Kicks off the work in the
 * background and returns immediately — the answer arrives over SSE
 * (`content_progress`) and is pushed into the conversation once ready, the
 * same shape the old ask()/act() used.
 */
export function examine(contentId, request, { sessionId } = {}) {
  const record = store.getContent(contentId);
  if (!record) throw new Error("I can't find that content any more.");
  const text = String(request || '').trim();
  if (!text) throw new Error("I didn't catch what you wanted to know.");

  runInBackground(async () => {
    announce(record, { status: 'working', instruction: text });
    let outcome;
    try {
      outcome = await runExamine(store.getContent(contentId), text);
    } catch (err) {
      console.error(`[investigator] examine "${contentId}" failed:`, err);
      outcome = { ok: false, error: 'Something went wrong looking into that.' };
    }

    if (!outcome.ok) {
      announce(record, { status: 'failed', error: outcome.error });
      return;
    }

    const added = store.addFinding(contentId, {
      request: text,
      answer: outcome.answer,
      intake: outcome.intake,
      sources: outcome.sources,
    });
    if (!added) return; // deleted mid-job
    pushFindingToConversation(added.record, added.finding, sessionId);
    announce(added.record, {
      status: 'ready',
      document: outcome.answer,
      instruction: text,
      lookedAgain: Boolean(outcome.lookedAgain),
      downgradedReason: outcome.downgradedReason || null,
    });
  });

  return record;
}

// ---------- checking a claim ----------

const VERDICT_VALUES = ['checks out', 'partly true', 'misleading', 'false', "can't tell"];

const JUDGE_SYSTEM = `You judge whether a claim holds up, for someone deciding whether to act on it.

Rules:
- Base the verdict on the research you are given, not on your own impressions.
- "can't tell" is a real, acceptable verdict. Use it rather than guessing when the research doesn't settle the question.
- Say what is being left out, not just what is wrong. A claim can be technically true and still misleading about how typical the result is.
- Be specific about conditions: what has to be true for this to work, and how often that is the case.
- Never soften a verdict to be polite, and never sharpen one for effect.`;

/**
 * Judges one claim: research first, then a verdict. `research()` is called
 * unconditionally at the top with no path around it — that is the point.
 * When this lived behind a wording-guess, the guarantee was really "a check
 * is likely to be routed correctly"; a function that always looks up before
 * it judges makes that impossible to skip, including by a model that thinks
 * it already knows.
 */
export async function judgeClaim({ claim, context, modelId, realism = false } = {}) {
  const question = String(claim || '').trim();
  if (!question) return { ok: false, error: 'There was no claim to check.' };

  const found = await research(
    realism
      ? `Is this realistic and typical, or is it exaggerated? "${question}". What actually happens for most people who try this, and what does it require?`
      : `Is this claim true? "${question}". Look for evidence for and against, and typical real-world figures.`,
    {
      modelId,
      searchQuery: toSearchQuery(question),
    }
  );

  const breakdownAsk =
    'Fill in "breakdown" whenever the underlying thing being described is real and someone could actually go ' +
    'and do it — even if this particular claim about it is exaggerated. Give the honest step-by-step: steps in ' +
    'order, how long each really takes, what it costs, what you need before you start, and where most people ' +
    'give up. Set "breakdown" to null only if the whole thing is bogus and nobody should attempt it.';

  const judged = await askModel({
    modelId,
    json: true,
    system: JUDGE_SYSTEM,
    prompt:
      (context ? `${context}\n\n` : '') +
      `The claim being judged: "${question}"\n\n` +
      (found.ok
        ? `Research findings:\n${found.answer}\n\nSources:\n${(found.sources || [])
            .map((s, i) => `[${i + 1}] ${s.title} — ${s.url}`)
            .join('\n')}\n\n`
        : `I could not research this (${found.error}). Say so in your reasoning and lean towards "can't tell".\n\n`) +
      `${breakdownAsk}\n\n` +
      `Reply as JSON:\n{\n  "verdict": one of ${JSON.stringify(VERDICT_VALUES)},\n` +
      `  "confidence": "high" | "medium" | "low",\n` +
      `  "reasoning": "a few sentences explaining the verdict, in plain language",\n` +
      `  "whatsLeftOut": "what is being left out, or null",\n` +
      `  "breakdown": "markdown, step by step" or null\n}`,
  });

  if (!judged.ok) return { ok: false, error: judged.error };

  const data = judged.data || {};
  const verdict = VERDICT_VALUES.includes(String(data.verdict).toLowerCase())
    ? String(data.verdict).toLowerCase()
    : "can't tell";

  const bits = [`**Verdict: ${verdict}**${data.confidence ? ` (${data.confidence} confidence)` : ''}`];
  bits.push(`\nOn this claim: *"${question}"*`);
  if (data.reasoning) bits.push(`\n${data.reasoning}`);
  if (data.whatsLeftOut) bits.push(`\n**What it doesn't tell you:** ${data.whatsLeftOut}`);
  if (data.breakdown) bits.push(`\n## What's actually involved\n\n${data.breakdown}`);
  if (!found.ok) bits.push(`\n*I couldn't look this up independently — ${found.error}*`);

  return {
    ok: true,
    result: bits.join('\n'),
    sources: found.ok ? found.sources : [],
    verdict,
    modelLabel: judged.modelLabel,
    researched: found.ok,
  };
}

/** A one-line spoken summary — voice skills must never read a whole finding out loud. */
export function describeStatus(record) {
  if (!record) return "I can't find that.";
  const name = record.identity?.title || 'that';
  if (!record.findings?.length) return `I've got ${name} — what would you like to know about it?`;
  return `${name} is ready.`;
}

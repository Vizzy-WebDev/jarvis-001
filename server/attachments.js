// Turning files attached in conversation into something a turn can carry.
//
// There are exactly two ways a file can enter a conversation, and choosing
// between them is the whole job of this file:
//
//   INLINE     — small enough to ride along inside the message itself. The
//                model sees the real thing, every turn, for as long as it's
//                in the transcript. Images and short text documents.
//
//   REGISTERED — too expensive to re-send on every turn, AND not something
//                to read automatically. Video and audio are registered with
//                Content Analysis's free glance (server/content/) and
//                nothing more — no watching, no reading happens here. The
//                model is told what the attachment appears to be and asked
//                to find out what the user wants, or to use examine_content
//                straight away if they already said. This used to
//                auto-ingest the file the instant it arrived; that violated
//                the same "don't analyse before being asked" rule
//                share_content enforces for anything shared as a link.
//
// Images deliberately do NOT get registered — they stay in the conversation
// at full fidelity, the way Claude and ChatGPT do it, so "what does the
// third line say?" still works ten turns later. That's a chosen trade: a
// bigger request each turn, in exchange for the image still being genuinely
// there.
//
// A leaf module. Imports media/uploads/ai and content/investigator.js;
// never skills/index.js, models/runner.js or anything that reaches them
// (see CLAUDE.md's circular-import invariant).

import fs from 'node:fs';
import { getUpload } from './uploads.js';
import { fileKind, mimeTypeFor, inlineAttachment } from './media.js';
import { canRun } from './ai.js';
import { share } from './content/investigator.js';
import { describe } from './content/intake.js';
import { extractDocument, isOfficeDocument } from './documents/index.js';

// A text document is pasted into the prompt, so it competes with the rest of
// the conversation for room. Generous enough for a real document, far short
// of a context window.
const TEXT_MAX_CHARS = 40000;
const TEXT_EXTENSIONS = /\.(txt|md|csv|json|rtf|log|ya?ml|xml|html?)$/i;

function isTextDocument(name) {
  return TEXT_EXTENSIONS.test(name || '');
}

/** Clips a document's rendered text to the same budget a plain text attachment gets, with the same honest truncation marker — a big Word doc or a wide workbook shouldn't be able to eat the entire turn's room by itself. */
function clipToBudget(text) {
  if (text.length <= TEXT_MAX_CHARS) return text;
  return `${text.slice(0, TEXT_MAX_CHARS)}\n--- (truncated — the file is longer than this) ---`;
}

// A generic "is this actually text" sniff for extensions TEXT_EXTENSIONS
// doesn't name — code files, config files, anything with no fixed rule.
// Reading the bytes and asking "does this decode as text" scales to every
// such format at once, rather than growing TEXT_EXTENSIONS forever; see
// analysis' own file-reading logic, which already takes this approach.
const TEXT_SNIFF_SAMPLE_BYTES = 8000;
const TEXT_SNIFF_MAX_REPLACEMENT_RATIO = 0.02;

function readAsGenericText(filePath) {
  let buf;
  try {
    buf = fs.readFileSync(filePath);
  } catch {
    return null;
  }
  const sample = buf.subarray(0, TEXT_SNIFF_SAMPLE_BYTES);
  if (sample.includes(0)) return null; // a NUL byte this early means binary, not text

  if (sample.length >= 2 && sample[0] === 0xff && sample[1] === 0xfe) return buf.toString('utf16le').slice(1);
  if (sample.length >= 2 && sample[0] === 0xfe && sample[1] === 0xff) {
    // UTF-16BE has no direct Node encoding — byte-swap into LE, which does.
    const swapped = Buffer.alloc(buf.length);
    for (let i = 0; i + 1 < buf.length; i += 2) {
      swapped[i] = buf[i + 1];
      swapped[i + 1] = buf[i];
    }
    return swapped.toString('utf16le').slice(1);
  }

  const decoded = sample.toString('utf8');
  const replacementCount = (decoded.match(/�/g) || []).length;
  if (decoded.length && replacementCount / decoded.length > TEXT_SNIFF_MAX_REPLACEMENT_RATIO) return null;
  return buf.toString('utf8');
}

/**
 * Prepares every attachment on one turn.
 *
 * Returns:
 *   media       [{kind, mimeType, dataBase64}] — rides inside the message
 *   textBlocks  [string] — document contents, prepended to the user's text
 *   need        {vision?} — what the turn now requires of a model, so
 *               runner.js can rank only models that can actually see it
 *   notes       [string] — plain-language things the model should know and
 *               can tell the user ("that PDF couldn't be read", "I'm going
 *               through the video now")
 */
export async function prepareForTurn(ids, { sessionId = 'main' } = {}) {
  const media = [];
  const textBlocks = [];
  const notes = [];
  const need = {};

  for (const id of ids || []) {
    const upload = getUpload(id);
    if (!upload) {
      notes.push('One of the attached files could not be found any more — it may have been cleared.');
      continue;
    }

    const kind = fileKind(upload.name);
    const mimeType = mimeTypeFor(upload.name);

    // ---- images: inline, and they stay ----
    if (kind === 'image') {
      const inline = inlineAttachment(upload.path, mimeType);
      if (inline.ok) {
        media.push(...inline.media);
        need.vision = true;
      } else if (inline.error === 'too_big') {
        notes.push(`"${upload.name}" is too large to send directly (${Math.round(upload.size / 1024 / 1024)}MB). A smaller copy would work.`);
      } else {
        notes.push(`"${upload.name}" couldn't be read.`);
      }
      continue;
    }

    // ---- text documents: read straight into the prompt, no capability needed ----
    if (isTextDocument(upload.name)) {
      try {
        const text = fs.readFileSync(upload.path, 'utf8');
        const clipped = text.slice(0, TEXT_MAX_CHARS);
        textBlocks.push(
          `--- Contents of the attached file "${upload.name}" ---\n${clipped}` +
            (text.length > clipped.length ? '\n--- (truncated — the file is longer than this) ---' : '\n--- end of file ---')
        );
      } catch {
        notes.push(`"${upload.name}" couldn't be read as text.`);
      }
      continue;
    }

    // ---- PDFs: only a rich-media model takes one; say so plainly otherwise ----
    if (mimeType === 'application/pdf') {
      if (!canRun({ video: true })) {
        notes.push(
          `"${upload.name}" is a PDF, and none of the available models can read one directly right now. ` +
            'A Gemini model can. Tell the user that rather than guessing at the contents.'
        );
        continue;
      }
      const inline = inlineAttachment(upload.path, mimeType);
      if (inline.ok) {
        media.push(...inline.media);
        need.video = true; // the "can take a rich file" ceiling, not literally video
      } else {
        notes.push(`"${upload.name}" is too large to send directly.`);
      }
      continue;
    }

    // ---- Word / Excel / PowerPoint: real structure, not a binary dump ----
    // No capability gate — this reads to Markdown text (server/documents/),
    // so every model can read one, same as a plain text document.
    if (isOfficeDocument(upload.name)) {
      const doc = extractDocument(upload.path);
      if (!doc.ok) {
        notes.push(`"${upload.name}" couldn't be read: ${doc.error}`);
        continue;
      }
      textBlocks.push(`--- Contents of the attached file "${upload.name}" ---\n${clipToBudget(doc.markdown)}`);
      if (doc.note) {
        // The analyze_spreadsheet skill needs this exact id to find the file
        // again — same upload-id space the browser already uses, not a new one.
        notes.push(`About "${upload.name}": ${doc.note} (reference id for analyze_spreadsheet: ${id})`);
      }
      if (doc.images?.length) {
        media.push(...doc.images);
        need.vision = true;
      }
      continue;
    }

    // ---- video and audio: register only — never read or watched here ----
    if (kind === 'video' || kind === 'audio') {
      try {
        const { record, identity } = await share({ source: { kind: 'file', filePath: upload.path }, sessionId });
        notes.push(
          `The user attached "${upload.name}" — ${describe(identity)}. It has NOT been read or watched — ` +
            `reference id: ${record.id}. If they already said what they want done with it in this message, ` +
            'use examine_content with that as the request. Otherwise say plainly what it appears to be and ask ' +
            'what they want, the same as for any shared content.'
        );
      } catch (err) {
        notes.push(`"${upload.name}" couldn't be registered: ${err?.message || 'unknown problem'}.`);
      }
      continue;
    }

    // ---- last resort: is this actually text, whatever its extension? ----
    // Covers code/config/log formats no named list will ever be exhaustive
    // for (.js, .py, .sql, .ini, ...) by checking the real bytes instead.
    const text = readAsGenericText(upload.path);
    if (text !== null) {
      const clipped = text.slice(0, TEXT_MAX_CHARS);
      textBlocks.push(
        `--- Contents of the attached file "${upload.name}" ---\n${clipped}` +
          (text.length > clipped.length ? '\n--- (truncated — the file is longer than this) ---' : '\n--- end of file ---')
      );
      continue;
    }

    notes.push(`"${upload.name}" isn't a kind of file that can be read.`);
  }

  return { media, textBlocks, need, notes };
}

/** Folds the document text and notes into the user's message, so a model with no media support still gets everything that could be read as words. */
export function composeMessage(userText, { textBlocks = [], notes = [] } = {}) {
  const parts = [];
  if (textBlocks.length) parts.push(textBlocks.join('\n\n'));
  if (notes.length) parts.push(`[Note for you, not spoken by the user: ${notes.join(' ')}]`);
  if (userText) parts.push(userText);
  return parts.join('\n\n').trim();
}

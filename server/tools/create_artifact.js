// Output/Artifact generation (root CLAUDE.md's Operational Awareness item
// 3) — real files (documents, spreadsheets, diagrams, code, or whatever
// actually fits the request), landing somewhere real and consistent
// (data/artifacts/, served at GET /api/artifacts/:id) rather than only
// referenced in text. Deliberately format-agnostic BY CONSTRUCTION, not by
// a format list — `name`'s own extension decides the mime type, and only
// two formats (.docx/.xlsx) need real assembly (server/artifacts/writers/);
// everything else is just the given content written as bytes.
//
// **Every artifact is mechanically verified the instant it's created —
// root CLAUDE.md item 4 applied at its freshest, most useful point.** A
// file that fails its own open/parse check (server/ops/verify.js) is
// deleted immediately, never left behind pretending to be a real
// deliverable — the model gets a real error to react to in the SAME turn,
// not a silently broken file discovered later. No retry-then-escalate here
// on purpose: these writers are deterministic, so a mechanical failure is a
// real bug that would fail identically on a retry, not a transient issue
// worth spending Jobs' own one-shot retry budget on.
//
// **Hybrid creation model, per the owner's own explicit choice**: this tool
// has NO confirm gate of its own — an explicit user request creates
// directly. The "propose first, wait for a yes" half of the design lives in
// prompt.js's own instruction (SYSTEM_INSTRUCTION), not a token-gated
// mechanism, since a Jarvis-INITIATED suggestion is a conversational
// judgment call, the same way remember_about_me's own approval flow is.
//
// **One honest capability ceiling, stated plainly rather than silently
// omitted**: raster image generation is not possible — none of the three
// model adapters does image generation, and no image library exists here.
// Diagrams/charts are real (SVG, genuine vector markup); photographs are
// not achievable at all.
//
// **`.pptx` carries its own, narrower honesty caveat than `.docx`/`.xlsx`
// — see `writers/pptx.js`'s own header comment for the full reasoning.**
// This project's one verification technique (round-tripping a generated
// file through its own real reader) genuinely cannot validate a
// PowerPoint deck's required slideMaster/slideLayout/theme chain the way
// it validated docx/xlsx — real PowerPoint has NOT opened a file this
// writer produced. Mechanically verified the same as every other format
// regardless (structural checks only, same blind spot).

import path from 'node:path';
import { saveArtifact, deleteArtifact, artifactFilePath, recordVerification } from '../artifacts/artifact-store.js';
import { verifyFileOpens } from '../ops/verify.js';
import { writeDocx } from '../artifacts/writers/docx.js';
import { writeXlsx } from '../artifacts/writers/xlsx.js';
import { writePptx } from '../artifacts/writers/pptx.js';

const MIME_BY_EXT = {
  '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  '.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
  '.pptx': 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
  '.svg': 'image/svg+xml',
  '.json': 'application/json',
  '.md': 'text/markdown',
  '.csv': 'text/csv',
  '.txt': 'text/plain',
  '.html': 'text/html',
  '.js': 'text/javascript',
  '.py': 'text/x-python',
};

function mimeTypeFor(name) {
  return MIME_BY_EXT[path.extname(String(name || '')).toLowerCase()] || 'text/plain';
}

export default {
  name: 'create_artifact',
  core: true,
  meta: true,
  description:
    'Create a real file the owner can open and download — a document, spreadsheet, presentation, diagram/chart (as real SVG), code file, ' +
    'or any other text-based format. Give it a name WITH a real extension (e.g. "report.docx", "budget.xlsx", "deck.pptx", "diagram.svg", ' +
    '"notes.md") — the extension decides the format. .docx/.xlsx/.pptx get real document/spreadsheet/presentation assembly; everything ' +
    'else is written as the exact text given. Raster/photographic image generation is NOT possible — say so plainly if asked for one, ' +
    'never claim to have made an image that is actually a text file. If the owner did not explicitly ask for a file, propose creating one ' +
    'and wait for a yes before calling this — this tool itself has no confirmation step, so that judgment belongs to you.',
  parameters: {
    type: 'object',
    properties: {
      name: { type: 'string', description: 'File name WITH extension, e.g. "report.docx".' },
      content: { type: 'string', description: 'The file\'s text content. For .docx, plain text — each line becomes a paragraph. Not used for .xlsx/.pptx.' },
      rows: {
        type: 'array',
        items: { type: 'array', items: { type: ['string', 'number'] } },
        description: 'Only for .xlsx — an array of rows, each row an array of cell values (string or number). The first row is typically headers.',
      },
      sheetName: { type: 'string', description: 'Only for .xlsx — the sheet name (defaults to "Sheet1").' },
      slides: {
        type: 'array',
        items: { type: 'string' },
        description: 'Only for .pptx — an array of slides, each slide plain text whose FIRST line is the slide title and remaining lines are its body content, one per line.',
      },
    },
    required: ['name'],
  },
  async run(args, ctx = {}) {
    const name = String(args?.name || '').trim();
    if (!name || !path.extname(name)) {
      return { ok: false, error: 'Give the file a name with a real extension, e.g. "report.docx" or "notes.md".' };
    }
    const ext = path.extname(name).toLowerCase();
    const mimeType = mimeTypeFor(name);

    let content;
    try {
      if (ext === '.docx') {
        content = await writeDocx(args?.content || '');
      } else if (ext === '.xlsx') {
        if (!Array.isArray(args?.rows) || !args.rows.length) {
          return { ok: false, error: 'A spreadsheet needs rows — an array of arrays of cell values.' };
        }
        content = await writeXlsx(args.rows, { sheetName: args?.sheetName });
      } else if (ext === '.pptx') {
        if (!Array.isArray(args?.slides) || !args.slides.length) {
          return { ok: false, error: 'A presentation needs slides — an array of strings, one per slide.' };
        }
        content = await writePptx(args.slides);
      } else {
        if (!args?.content) return { ok: false, error: 'Give the file some real content.' };
        content = args.content;
      }
    } catch (err) {
      return { ok: false, error: `Could not assemble the file: ${err?.message || err}` };
    }

    let artifact;
    try {
      artifact = saveArtifact({ name, mimeType, content, sessionId: ctx.sessionId || null });
    } catch (err) {
      return { ok: false, error: err?.message || 'Could not save the file.' };
    }

    const check = verifyFileOpens(artifactFilePath(artifact.id));
    if (!check.ok) {
      deleteArtifact(artifact.id);
      return { ok: false, error: `Generated but failed its own verification, so it was not kept: ${check.detail}` };
    }
    // Records the real outcome, not just reacts to it — a kept artifact's
    // `verified` column would otherwise stay perpetually null even though a
    // real check already ran and passed.
    recordVerification(artifact.id, { verified: true, detail: null });

    return {
      ok: true,
      id: artifact.id,
      name: artifact.name,
      mimeType: artifact.mimeType,
      size: artifact.size,
      url: `/api/artifacts/${artifact.id}`,
      ui_action: { type: 'artifact_created', id: artifact.id, name: artifact.name, mimeType: artifact.mimeType, size: artifact.size, url: `/api/artifacts/${artifact.id}` },
    };
  },
};

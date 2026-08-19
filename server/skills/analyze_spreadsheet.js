// Skill: answers a question against a WHOLE spreadsheet, not just the first
// few hundred rows a chat turn can afford to inline.
//
// attachments.js already reads a small workbook straight into the message
// as a Markdown table (see documents/xlsx.js) — that's simpler and works
// with any model, so it stays the default. This skill is what a big
// workbook's truncation note points at: instead of pasting more rows (a
// context-window problem, not something more rows fixes), it looks at the
// sheet's SHAPE, writes a short script, and runs it against the REAL, full
// file in the sandbox — the way an analyst would, not a chatbot guessing
// from a sample.
//
// Deliberately NOT confirm:'always' — re-confirming every follow-up
// question about one spreadsheet would make this unusable, and the script
// is generated from the user's own question against a file they attached
// moments ago, with no host folder access. Returning the script alongside
// the answer is the visibility that replaces a confirm prompt: the user can
// see exactly how the number was reached.
//
// A leaf module: imports ai.js, sandbox/runner.js, uploads.js, and
// documents/index.js — never skills/index.js or anything that reaches it.

import { getUpload } from '../uploads.js';
import { officeSpreadsheetToCsv, officeSpreadsheetSheetNames } from '../documents/index.js';
import { askModel } from '../ai.js';
import { runCode as runInSandbox } from '../sandbox/runner.js';

const MAX_ROWS_IN_PROMPT_SAMPLE = 5;
const SANDBOX_TIMEOUT_MS = 15_000;

function extractCode(text) {
  const fenced = /```(?:javascript|js)?\s*([\s\S]*?)```/i.exec(text);
  return (fenced ? fenced[1] : text).trim();
}

async function writeAnalysisScript({ sheetName, header, sampleRows, rowCount, question }) {
  const prompt =
    `A CSV file named "data.csv" sits in the current folder — sheet "${sheetName}" from a spreadsheet, ` +
    `${rowCount} data rows (not counting the header). It is NOT pasted below in full, only a sample, ` +
    `because the real file is too big for this prompt — your script must read the actual file, not this sample.\n\n` +
    `Header: ${header}\nFirst ${sampleRows.length} rows as a sample of the shape:\n${sampleRows.join('\n')}\n\n` +
    `Question to answer: ${question}\n\n` +
    'Write a complete, self-contained Node.js (CommonJS — use require, not import) script that:\n' +
    '- reads "data.csv" with fs.readFileSync("data.csv", "utf8")\n' +
    '- parses it as CSV (a quoted field can contain a comma or a newline — handle that, do not just split on ",")\n' +
    '- computes the real answer to the question over the WHOLE file, not the sample\n' +
    '- ends with exactly one console.log() of a short, plain-language final answer (not JSON, not the raw data)\n' +
    'Reply with ONLY the JavaScript code — no explanation, before or after.';

  return askModel({ prompt, background: true });
}

export default {
  name: 'analyze_spreadsheet',
  description:
    'Answers a question against the FULL contents of an Excel workbook someone attached — use this when a ' +
    'spreadsheet was too large to read in full in the conversation (the attachment note says so and gives a ' +
    'reference id) and the question needs an exact answer covering every row, not just a sample. Not for a small ' +
    'workbook that already read in fully — answer those directly from what is already in the conversation.',
  parameters: {
    type: 'object',
    properties: {
      uploadId: { type: 'string', description: 'The reference id from the attachment note (NOT a filename).' },
      question: { type: 'string', description: "The user's question about the data, in their own words." },
      sheetName: { type: 'string', description: 'Which sheet to analyse, if the workbook has more than one and it matters. Leave out to use the first sheet.' },
    },
    required: ['uploadId', 'question'],
  },
  async run(args) {
    const upload = getUpload(args.uploadId);
    if (!upload) {
      return { ok: false, error: "I can't find that attachment any more — it may have been cleared. Ask the user to attach the file again." };
    }

    let sheetNames;
    try {
      sheetNames = officeSpreadsheetSheetNames(upload.path);
    } catch (err) {
      return { ok: false, error: `That doesn't look like a readable Excel workbook: ${err?.message || 'unknown problem'}.` };
    }
    if (!sheetNames.length) return { ok: false, error: 'That workbook has no readable sheets.' };

    const sheetName = args.sheetName && sheetNames.includes(args.sheetName) ? args.sheetName : sheetNames[0];

    let csv;
    try {
      csv = officeSpreadsheetToCsv(upload.path, sheetName);
    } catch (err) {
      return { ok: false, error: `Could not read sheet "${sheetName}": ${err?.message || 'unknown problem'}.` };
    }

    const lines = csv.split('\n');
    const header = lines[0] || '';
    const rowCount = Math.max(0, lines.length - 1);
    const sampleRows = lines.slice(1, 1 + MAX_ROWS_IN_PROMPT_SAMPLE);

    const written = await writeAnalysisScript({ sheetName, header, sampleRows, rowCount, question: args.question });
    if (!written.ok) return { ok: false, error: written.error };
    const code = extractCode(written.text);
    if (!code) return { ok: false, error: 'Could not work out how to analyse that file.' };

    const result = await runInSandbox({
      language: 'javascript',
      code,
      files: [{ name: 'data.csv', content: csv }],
      timeoutMs: SANDBOX_TIMEOUT_MS,
      allowNetwork: false,
      allowPaths: [],
    });

    if (result.ok && result.stdout?.trim()) {
      return {
        ok: true,
        sheet: sheetName,
        rowCount,
        answer: result.stdout.trim(),
        script: code,
        isolation: result.isolation,
      };
    }

    // One retry, feeding the real error back — not a loop, quota is the
    // binding constraint on this project (see CLAUDE.md), so this tries
    // once more with the actual failure and then reports honestly.
    const retryPrompt =
      `The script you wrote for this task failed:\n\n${code}\n\nError:\n${(result.stderr || result.error || 'no output').slice(0, 2000)}\n\n` +
      'Fix it and reply with ONLY the corrected JavaScript code.';
    const retryWritten = await askModel({ prompt: retryPrompt, background: true });
    if (!retryWritten.ok) {
      return { ok: false, error: `The analysis script failed and could not be fixed: ${result.stderr || result.error || 'no output'}` };
    }
    const retryCode = extractCode(retryWritten.text);
    const retryResult = await runInSandbox({
      language: 'javascript',
      code: retryCode,
      files: [{ name: 'data.csv', content: csv }],
      timeoutMs: SANDBOX_TIMEOUT_MS,
      allowNetwork: false,
      allowPaths: [],
    });

    if (retryResult.ok && retryResult.stdout?.trim()) {
      return {
        ok: true,
        sheet: sheetName,
        rowCount,
        answer: retryResult.stdout.trim(),
        script: retryCode,
        isolation: retryResult.isolation,
      };
    }

    return {
      ok: false,
      error: `Couldn't compute that after two attempts: ${retryResult.stderr || retryResult.error || 'the script produced no output'}`,
    };
  },
};

// The one seam onto server/documents/ — nothing outside this folder should
// import zip.js/xml.js/docx.js/xlsx.js/pptx.js/media.js/charts.js directly.
// Callers (attachments.js, analyzer.js, the analyze_spreadsheet skill) only
// ever need "read this file's real content," not which format it is.
//
// A leaf module: imports node:fs, node:path, and this folder's own modules
// only — safe for skills to import (see CLAUDE.md's circular-import rule).

import fs from 'node:fs';
import path from 'node:path';
import { docxToMarkdown } from './docx.js';
import { pptxToMarkdown } from './pptx.js';
import { xlsxToMarkdown, xlsxSheetToCsv, xlsxSheetNames } from './xlsx.js';

const OFFICE_EXTENSIONS = new Set(['.docx', '.pptx', '.xlsx']);

export function isOfficeDocument(filePath) {
  return OFFICE_EXTENSIONS.has(path.extname(filePath || '').toLowerCase());
}

/**
 * Reads a Word/PowerPoint/Excel file into `{ok, markdown, images, note}`.
 * `images` is always an array (empty for a workbook — spreadsheets don't
 * embed pictures the same way). `note` carries a plain-language truncation
 * or limitation message when there is one; `undefined` otherwise.
 *
 * `maxRowsPerSheet` only matters for `.xlsx` — the caller's context budget,
 * not a fixed constant, since attachments.js and the analyzer have
 * different amounts of room to work with.
 */
export function extractDocument(filePath, { maxRowsPerSheet = 300 } = {}) {
  const ext = path.extname(filePath).toLowerCase();
  let buf;
  try {
    buf = fs.readFileSync(filePath);
  } catch {
    return { ok: false, error: "I couldn't find that file — it may have been cleared." };
  }

  try {
    if (ext === '.docx') {
      const { markdown, images } = docxToMarkdown(buf);
      return { ok: true, markdown, images };
    }
    if (ext === '.pptx') {
      const { markdown, images } = pptxToMarkdown(buf);
      return { ok: true, markdown, images };
    }
    if (ext === '.xlsx') {
      const { markdown, truncated, sheetCount } = xlsxToMarkdown(buf, { maxRowsPerSheet });
      const note = truncated
        ? 'One or more sheets were too large to include in full here — ask to analyse the whole file for an exact ' +
          'answer that covers every row.'
        : undefined;
      return { ok: true, markdown, images: [], note, truncated, sheetCount };
    }
    return { ok: false, error: 'That is not a Word, Excel, or PowerPoint file this reader recognises.' };
  } catch (err) {
    return { ok: false, error: err?.message || 'That file could not be read.' };
  }
}

/** For the sandbox path (2G): a sheet's full data as CSV, no row limit. */
export function officeSpreadsheetToCsv(filePath, sheetName) {
  const buf = fs.readFileSync(filePath);
  return xlsxSheetToCsv(buf, sheetName);
}

/** For the sandbox path (2G): sheet names, so a script can be written against the real ones without inlining the whole file first. */
export function officeSpreadsheetSheetNames(filePath) {
  const buf = fs.readFileSync(filePath);
  return xlsxSheetNames(buf);
}

// Reads an .xlsx workbook: every sheet, as a proper grid (not a flat list of
// cells) — the whole point being that a spreadsheet's columns must line up.
//
// The trap this file exists to avoid: a cell is placed by its own reference
// (e.g. c[r="C5"]), NEVER by iteration order. In real Excel files, a blank
// cell is usually just absent from the XML — walking cells in document order
// and pushing them left-to-right would shift every later column into the
// wrong place the moment a row has a gap. Verified against a real 700-row
// workbook during this build; sparse rows are common, not an edge case.
//
// A leaf module: imports zip.js and xml.js only.

import { readZip } from './zip.js';
import { parseXml, findAll, findChildren, findChild, textContent } from './xml.js';

// Excel stores a date as a plain number (days since 1899-12-30 — the epoch
// is deliberately one day before 1900-01-01 to reproduce a 123-year-old
// spreadsheet bug where 1900 was wrongly treated as a leap year, which
// every real writer still honours today for compatibility). Which numbers
// ARE dates is a separate fact recorded in styles.xml, not the cell itself
// — read here so a report doesn't quietly show "41640" instead of a date.
const BUILTIN_DATE_FORMAT_IDS = new Set([14, 15, 16, 17, 18, 19, 20, 21, 22, 45, 46, 47]);

function excelSerialToIsoDate(serial) {
  const n = Number(serial);
  if (!Number.isFinite(n)) return String(serial);
  const ms = Math.round((n - 25569) * 86400 * 1000); // 25569 = days between 1899-12-30 and 1970-01-01
  const d = new Date(ms);
  if (Number.isNaN(d.getTime())) return String(serial);
  return d.toISOString().slice(0, 10);
}

/** Reads xl/styles.xml and returns a function `isDateStyle(sIndex) -> bool`, resolving both built-in and custom (`<numFmts>`) format ids through each cell's own style index. */
function readDateStyleLookup(files) {
  const buf = files['xl/styles.xml'];
  if (!buf) return () => false;
  const tree = parseXml(buf.toString('utf8'));

  const customDateFormatIds = new Set();
  for (const fmt of findAll(tree, 'numFmt')) {
    const code = (fmt.attrs.formatCode || '').toLowerCase();
    // A format that shows date/month/day/year parts and no time-only markers.
    if (/[ymd]/.test(code) && !/^\[?h+\]?:mm/.test(code)) customDateFormatIds.add(Number(fmt.attrs.numFmtId));
  }

  const cellXfsNode = findAll(tree, 'cellXfs')[0];
  const xfs = cellXfsNode ? findChildren(cellXfsNode, 'xf') : [];
  const isDateByStyleIndex = xfs.map((xf) => {
    const id = Number(xf.attrs.numFmtId || 0);
    return BUILTIN_DATE_FORMAT_IDS.has(id) || customDateFormatIds.has(id);
  });

  return (sIndex) => isDateByStyleIndex[Number(sIndex) || 0] === true;
}

/** "C" -> 3, "AA" -> 27. Column letters only ever go up (no zero, no negatives), so this is a plain base-26 digit walk. */
function colLettersToIndex(letters) {
  let idx = 0;
  for (let i = 0; i < letters.length; i++) idx = idx * 26 + (letters.charCodeAt(i) - 64);
  return idx;
}

const CELL_REF_RE = /^([A-Z]+)(\d+)$/;

function parseCellRef(ref) {
  const m = CELL_REF_RE.exec(ref || '');
  if (!m) return null;
  return { col: colLettersToIndex(m[1]), row: Number(m[2]) };
}

/** The shared-string table every `t="s"` cell indexes into. Rich-text runs (`<r><t>`) are concatenated, same as a plain `<t>`. */
function readSharedStrings(files) {
  const buf = files['xl/sharedStrings.xml'];
  if (!buf) return [];
  const tree = parseXml(buf.toString('utf8'));
  return findAll(tree, 'si').map((si) => textContent(si));
}

/** Sheet name/order from workbook.xml, resolved to their actual worksheet file via the rels part (the id is NOT the file name — always go through the relationship). */
function readSheetList(files) {
  const wb = files['xl/workbook.xml'];
  if (!wb) throw new Error("That workbook is missing its sheet index — it may not be a valid .xlsx.");
  const wbTree = parseXml(wb.toString('utf8'));
  const sheetNodes = findAll(wbTree, 'sheet');

  const relsBuf = files['xl/_rels/workbook.xml.rels'];
  const relTargetById = {};
  if (relsBuf) {
    const relsTree = parseXml(relsBuf.toString('utf8'));
    for (const rel of findAll(relsTree, 'Relationship')) {
      relTargetById[rel.attrs.Id] = rel.attrs.Target;
    }
  }

  return sheetNodes.map((s) => {
    const rId = s.attrs['id']; // r:id, namespace already stripped by xml.js
    const target = relTargetById[rId];
    const path = target ? (target.startsWith('/') ? target.slice(1) : `xl/${target.replace(/^\.?\//, '')}`) : null;
    return { name: s.attrs.name || 'Sheet', path };
  });
}

/** A cell's plain value, resolving shared strings, inline strings, booleans, dates, and numbers/errors — but never a formula's source text, just what it evaluates to. */
function cellValue(cellNode, sharedStrings, isDateStyle) {
  const type = cellNode.attrs.t;
  if (type === 's') {
    const idx = Number(findChild(cellNode, 'v')?.text ?? textContent(findChild(cellNode, 'v') || cellNode));
    return Number.isInteger(idx) ? sharedStrings[idx] ?? '' : '';
  }
  if (type === 'inlineStr') {
    const is = findChild(cellNode, 'is');
    return is ? textContent(is) : '';
  }
  if (type === 'str' || type === 'e') {
    // 'str' = a formula's cached STRING result; 'e' = an error code (#DIV/0! etc).
    const v = findChild(cellNode, 'v');
    return v ? textContent(v) : '';
  }
  if (type === 'b') {
    const v = findChild(cellNode, 'v');
    return textContent(v || cellNode) === '1' ? 'TRUE' : 'FALSE';
  }
  // No `t` attribute at all is Excel's default: a plain number — except a
  // date is ALSO stored as a plain number, distinguishable only via the
  // cell's own style index (`s=`), never the cell's own `t` attribute.
  const v = findChild(cellNode, 'v');
  const raw = v ? textContent(v) : '';
  if (raw && isDateStyle(cellNode.attrs.s)) return excelSerialToIsoDate(raw);
  return raw;
}

/**
 * Reads one worksheet into a grid: `{rows: [[...cells]], formulas: [{ref, formula}], rowCount, colCount, truncatedRows}`.
 * `maxRows` bounds how much is actually materialized — the caller decides
 * the budget (inline Markdown vs. the sandbox path), this just stops early
 * rather than building a grid for a 100k-row sheet nobody asked to inline.
 */
function readWorksheet(xmlStr, sharedStrings, isDateStyle, { maxRows = Infinity } = {}) {
  const tree = parseXml(xmlStr);
  const rowNodes = findAll(tree, 'row');
  const formulas = [];
  let maxCol = 0;
  let truncatedRows = false;

  const rows = [];
  for (const rowNode of rowNodes) {
    if (rows.length >= maxRows) {
      truncatedRows = true;
      break;
    }
    const rowNum = Number(rowNode.attrs.r) || rows.length + 1;
    const line = [];
    for (const cellNode of findChildren(rowNode, 'c')) {
      const ref = parseCellRef(cellNode.attrs.r);
      const colIdx = ref ? ref.col : line.length + 1; // fallback for a malformed/missing r=, rare
      if (colIdx > maxCol) maxCol = colIdx;
      line[colIdx - 1] = cellValue(cellNode, sharedStrings, isDateStyle);

      const fNode = findChild(cellNode, 'f');
      if (fNode) formulas.push({ ref: cellNode.attrs.r || `row ${rowNum}`, formula: textContent(fNode) });
    }
    // Fill gaps left by cells the writer omitted entirely, so every row is
    // the same length and columns stay aligned when rendered.
    for (let c = 0; c < maxCol; c++) if (line[c] === undefined) line[c] = '';
    rows.push(line);
  }

  // A later row can still be the widest — pad every row up to the sheet's real column count.
  for (const line of rows) while (line.length < maxCol) line.push('');

  return { rows, formulas, rowCount: rowNodes.length, colCount: maxCol, truncatedRows };
}

/** One cell escaped for a CSV field — quoted whenever it contains a comma, quote, or newline. */
function csvField(value) {
  const s = String(value ?? '');
  return /[",\n\r]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

function toCsv(rows) {
  return rows.map((line) => line.map(csvField).join(',')).join('\n');
}

/** One cell escaped for a Markdown table — pipes and newlines would otherwise break the table structure. */
function mdField(value) {
  return String(value ?? '').replace(/\|/g, '\\|').replace(/\r?\n/g, ' ');
}

function toMarkdownTable(rows) {
  if (!rows.length) return '(empty sheet)';
  const header = rows[0].map(mdField);
  const lines = [`| ${header.join(' | ')} |`, `| ${header.map(() => '---').join(' | ')} |`];
  for (const line of rows.slice(1)) lines.push(`| ${line.map(mdField).join(' | ')} |`);
  return lines.join('\n');
}

/**
 * Reads an .xlsx into every sheet's grid. `maxRowsPerSheet` bounds how many
 * rows are read per sheet (the caller's budget, see attachments.js's
 * per-sheet split); pass Infinity for the sandbox path, which needs the
 * whole file.
 */
export function readXlsx(buf, { maxRowsPerSheet = Infinity } = {}) {
  const files = readZip(buf);
  const sharedStrings = readSharedStrings(files);
  const sheetList = readSheetList(files);
  const isDateStyle = readDateStyleLookup(files);
  if (!sheetList.length) throw new Error("That workbook has no readable sheets.");

  return sheetList
    .filter((s) => s.path && files[s.path])
    .map((s) => {
      const sheet = readWorksheet(files[s.path].toString('utf8'), sharedStrings, isDateStyle, {
        maxRows: maxRowsPerSheet,
      });
      return { name: s.name, ...sheet };
    });
}

/**
 * The full workbook as Markdown — headings per sheet, a pipe table, and a
 * "Formulas used" section when any formulas were found. Truncation is
 * stated plainly in the text AND returned as `truncated`, so a caller can
 * decide to offer the sandbox path (2G) rather than a reader having to
 * notice the inline note.
 */
export function xlsxToMarkdown(buf, { maxRowsPerSheet = Infinity } = {}) {
  const sheets = readXlsx(buf, { maxRowsPerSheet });
  const parts = [];
  let truncated = false;
  for (const sheet of sheets) {
    parts.push(`## Sheet: ${sheet.name}`);
    parts.push(toMarkdownTable(sheet.rows));
    if (sheet.truncatedRows) {
      truncated = true;
      parts.push(`_(this sheet has ${sheet.rowCount} rows; only the first ${sheet.rows.length} are shown here)_`);
    }
    if (sheet.formulas.length) {
      const shown = sheet.formulas.slice(0, 30);
      parts.push(
        `**Formulas used:** ${shown.map((f) => `${f.ref}: \`=${f.formula}\``).join(', ')}` +
          (sheet.formulas.length > shown.length ? ` (and ${sheet.formulas.length - shown.length} more)` : '')
      );
    }
  }
  return { markdown: parts.join('\n\n'), truncated, sheetCount: sheets.length };
}

/** One sheet's grid as CSV — the format the sandbox actually gets, since it has no spreadsheet library of its own to read Markdown tables back into data. */
export function xlsxSheetToCsv(buf, sheetName) {
  const sheets = readXlsx(buf, { maxRowsPerSheet: Infinity });
  const sheet = sheetName ? sheets.find((s) => s.name === sheetName) : sheets[0];
  if (!sheet) throw new Error(`That workbook has no sheet named "${sheetName}".`);
  return toCsv(sheet.rows);
}

/** Just the sheet names, in order — for the sandbox path to describe the file's shape before writing analysis code. */
export function xlsxSheetNames(buf) {
  return readXlsx(buf, { maxRowsPerSheet: 0 }).map((s) => s.name);
}

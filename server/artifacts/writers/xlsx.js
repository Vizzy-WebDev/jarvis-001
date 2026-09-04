// Writes a minimal, real, valid single-sheet .xlsx from a 2D array of
// rows. Every text cell is written as an inline string (`t="inlineStr"`) —
// deliberately skipping a shared-strings table (xl/sharedStrings.xml),
// which documents/xlsx.js's own reader already supports as an equally
// valid cell type. Simpler to write correctly, at the cost of a slightly
// larger file for a workbook with many repeated strings — a fine trade for
// Jarvis's own generated output, not a general spreadsheet-authoring
// engine.
//
// Verified against this project's OWN existing reader (documents/xlsx.js's
// readXlsx()) as part of this build, same as docx.js's writer.

import { buildOfficeZip } from './office-zip.js';

function escapeXml(text) {
  return String(text ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&apos;');
}

function colLetters(index) {
  // 1-based, matching Excel's own A/B/.../Z/AA/... convention.
  let n = index;
  let out = '';
  while (n > 0) {
    const rem = (n - 1) % 26;
    out = String.fromCharCode(65 + rem) + out;
    n = Math.floor((n - 1) / 26);
  }
  return out;
}

function cellXml(value, colIndex, rowIndex) {
  const ref = `${colLetters(colIndex)}${rowIndex}`;
  if (typeof value === 'number' && Number.isFinite(value)) {
    return `<c r="${ref}"><v>${value}</v></c>`;
  }
  const text = value === null || value === undefined ? '' : String(value);
  return `<c r="${ref}" t="inlineStr"><is><t xml:space="preserve">${escapeXml(text)}</t></is></c>`;
}

const CONTENT_TYPES = `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
</Types>`;

const PACKAGE_RELS = `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>`;

const WORKBOOK_RELS = `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
</Relationships>`;

/** `rows` is an array of arrays — each inner array one row's cell values (string or number). `sheetName` defaults to 'Sheet1'. */
export async function writeXlsx(rows, { sheetName = 'Sheet1' } = {}) {
  const safeSheetName = escapeXml(String(sheetName || 'Sheet1').slice(0, 31)); // Excel's own 31-char sheet-name limit

  const workbookXml = `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets><sheet name="${safeSheetName}" sheetId="1" r:id="rId1"/></sheets>
</workbook>`;

  const rowsXml = (rows || [])
    .map((row, ri) => {
      const cells = (row || []).map((v, ci) => cellXml(v, ci + 1, ri + 1)).join('');
      return `<row r="${ri + 1}">${cells}</row>`;
    })
    .join('');

  const sheetXml = `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <sheetData>${rowsXml}</sheetData>
</worksheet>`;

  return buildOfficeZip({
    '[Content_Types].xml': CONTENT_TYPES,
    '_rels/.rels': PACKAGE_RELS,
    'xl/workbook.xml': workbookXml,
    'xl/_rels/workbook.xml.rels': WORKBOOK_RELS,
    'xl/worksheets/sheet1.xml': sheetXml,
  });
}

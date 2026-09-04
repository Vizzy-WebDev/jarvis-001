// Writes a minimal, real, valid .docx — plain paragraphs (one per input
// line), no styling. Deliberately minimal rather than attempting to mirror
// documents/docx.js's full read-side feature set (headings, bold, tables,
// images) — this is a WRITER for Jarvis's own generated text output, not a
// general document-authoring engine; a user who wants rich formatting is
// better served asking for Markdown or attaching a real template.
//
// Verified against this project's OWN existing reader (documents/docx.js's
// docxToMarkdown()) as part of this build — a file this writer produces
// round-trips through that real, independently-built parser cleanly, which
// is the honest verification available in an environment with no real Word
// to open it in.

import { buildOfficeZip } from './office-zip.js';

function escapeXml(text) {
  return String(text ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&apos;');
}

const CONTENT_TYPES = `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>`;

const PACKAGE_RELS = `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>`;

/** `text` is plain text; a blank line becomes an empty paragraph (a real visual break, not collapsed away). */
export async function writeDocx(text) {
  const lines = String(text ?? '').split(/\r?\n/);
  const paragraphs = lines
    .map((line) => `<w:p><w:r><w:t xml:space="preserve">${escapeXml(line)}</w:t></w:r></w:p>`)
    .join('');

  const documentXml = `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>${paragraphs}<w:sectPr/></w:body>
</w:document>`;

  return buildOfficeZip({
    '[Content_Types].xml': CONTENT_TYPES,
    '_rels/.rels': PACKAGE_RELS,
    'word/document.xml': documentXml,
  });
}

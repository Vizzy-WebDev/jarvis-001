// Reads a Word document (word/document.xml) into Markdown: paragraphs,
// headings, lists, bold/italic, hyperlinks, tables, and embedded pictures at
// roughly their real position in the text. Deliberately not attempting page
// layout — see the plan's own non-goals section for why that's a different
// problem (rendering vs. reading).
//
// A leaf module: imports zip.js, xml.js, and media.js/charts.js from this
// same folder only.

import { readZip } from './zip.js';
import { parseXml, findAll, findChild, findChildren, textContent } from './xml.js';
import { extractImages } from './media.js';
import { extractAllCharts } from './charts.js';

function readRelationships(files, relsPath) {
  const buf = files[relsPath];
  if (!buf) return {};
  const tree = parseXml(buf.toString('utf8'));
  const map = {};
  for (const rel of findAll(tree, 'Relationship')) map[rel.attrs.Id] = rel.attrs.Target;
  return map;
}

/** "Heading1"/"heading 2"/"Title" style ids -> a Markdown heading level, or 0 for body text. */
function headingLevel(styleId) {
  if (!styleId) return 0;
  if (/^title$/i.test(styleId)) return 1;
  const m = /^heading\s*(\d)$/i.exec(styleId);
  return m ? Math.min(6, Number(m[1])) : 0;
}

function isBoldOn(rPr) {
  const b = rPr && findChild(rPr, 'b');
  return Boolean(b) && b.attrs.val !== '0' && b.attrs.val !== 'false';
}

function isItalicOn(rPr) {
  const i = rPr && findChild(rPr, 'i');
  return Boolean(i) && i.attrs.val !== '0' && i.attrs.val !== 'false';
}

/** A single `<w:r>` run's text, with `**`/`*` applied for bold/italic and `<w:tab>`/`<w:br>` translated to real whitespace. */
function renderRun(runNode) {
  let text = '';
  for (const child of runNode.children) {
    if (child.name === 't') text += textContent(child);
    else if (child.name === 'tab') text += '\t';
    else if (child.name === 'br' || child.name === 'cr') text += '\n';
  }
  if (!text) return '';
  const rPr = findChild(runNode, 'rPr');
  const bold = isBoldOn(rPr);
  const italic = isItalicOn(rPr);
  if (bold && italic) return `***${text}***`;
  if (bold) return `**${text}**`;
  if (italic) return `*${text}*`;
  return text;
}

/** A paragraph's rendered text, walking runs directly and runs wrapped in a `<w:hyperlink>` (which links via `_rels`, not a plain URL string). */
function renderParagraphText(pNode, rels) {
  let text = '';
  for (const child of pNode.children) {
    if (child.name === 'r') {
      text += renderRun(child);
    } else if (child.name === 'hyperlink') {
      const target = rels[child.attrs.id];
      const inner = findChildren(child, 'r').map(renderRun).join('');
      text += target && inner ? `[${inner}](${target})` : inner;
    }
  }
  return text.trim();
}

/** Every `<a:blip r:embed="...">` inside a paragraph — a paragraph normally has at most one, but a multi-image paragraph is handled the same way. */
function paragraphImageEmbedIds(pNode) {
  return findAll(pNode, 'blip')
    .map((b) => b.attrs.embed)
    .filter(Boolean);
}

function renderParagraph(pNode, rels) {
  const pPr = findChild(pNode, 'pPr');
  const styleId = pPr && findChild(pPr, 'pStyle')?.attrs.val;
  const level = headingLevel(styleId);
  const isListItem = Boolean(pPr && findChild(pPr, 'numPr'));

  let text = renderParagraphText(pNode, rels);
  if (!text) return '';
  if (level) return `${'#'.repeat(level)} ${text}`;
  if (isListItem) return `- ${text}`;
  return text;
}

function renderTable(tblNode, rels) {
  const rows = findChildren(tblNode, 'tr').map((tr) =>
    findChildren(tr, 'tc').map((tc) =>
      findChildren(tc, 'p')
        .map((p) => renderParagraphText(p, rels))
        .filter(Boolean)
        .join(' ')
        .replace(/\|/g, '\\|')
    )
  );
  if (!rows.length) return '';
  const width = Math.max(...rows.map((r) => r.length));
  const padded = rows.map((r) => [...r, ...Array(width - r.length).fill('')]);
  const lines = [`| ${padded[0].join(' | ')} |`, `| ${padded[0].map(() => '---').join(' | ')} |`];
  for (const row of padded.slice(1)) lines.push(`| ${row.join(' | ')} |`);
  return lines.join('\n');
}

/**
 * Reads a .docx into `{markdown, images}` — `images` are `{kind:'image',
 * mimeType, dataBase64}` ready to ride in a turn's `media` array, already
 * capped and filtered (see media.js). Image position is preserved as an
 * `[Image N]` marker inline in the text; charts are appended as a separate
 * section (position not resolvable without walking the drawing part too —
 * see charts.js).
 */
export function docxToMarkdown(buf) {
  const files = readZip(buf);
  const docBuf = files['word/document.xml'];
  if (!docBuf) throw new Error("That file doesn't look like a valid Word document.");

  const rels = readRelationships(files, 'word/_rels/document.xml.rels');
  const tree = parseXml(docBuf.toString('utf8'));
  const body = findAll(tree, 'body')[0];
  if (!body) throw new Error("That Word document has no readable content.");

  const blocks = [];
  const embedIds = [];
  let imageCounter = 0;

  for (const child of body.children) {
    if (child.name === 'tbl') {
      const md = renderTable(child, rels);
      if (md) blocks.push(md);
      continue;
    }
    if (child.name !== 'p') continue;

    let text = renderParagraph(child, rels);
    const ids = paragraphImageEmbedIds(child);
    for (const id of ids) {
      imageCounter++;
      embedIds.push(id);
      text += `${text ? ' ' : ''}[Image ${imageCounter}]`;
    }
    if (text) blocks.push(text);
  }

  const images = extractImages(files, 'word/_rels/document.xml.rels', embedIds);
  if (images.length < embedIds.length) {
    blocks.push(
      `_(${embedIds.length - images.length} more embedded image(s) in this document were skipped — small icons/bullets, ` +
        'an unreadable format, or past the per-document limit.)_'
    );
  }
  const charts = extractAllCharts(files);
  if (charts.length) blocks.push('## Charts', ...charts);

  return { markdown: blocks.join('\n\n'), images };
}

// Reads a PowerPoint deck: every slide in its real presentation order, text
// grouped by shape, tables, speaker notes, and embedded pictures.
//
// Slide ORDER is not "slide1.xml, slide2.xml, ..." — it's whatever
// ppt/presentation.xml's <p:sldIdLst> says, resolved through the
// presentation's own .rels part. Reading slide files in filename order is a
// real, easy-to-make mistake: PowerPoint reuses and renumbers slideN.xml
// files as a deck is edited, so file-name order silently drifts from the
// order a viewer actually sees.
//
// A leaf module: imports zip.js, xml.js, and media.js from this folder only.

import { readZip } from './zip.js';
import { parseXml, findAll, findChild, findChildren, textContent } from './xml.js';
import { resolveRelationshipTargets, extractImages } from './media.js';
import { extractAllCharts } from './charts.js';

/**
 * Drawingml (used by slides) puts bold/italic on the run-properties element
 * ITSELF as attributes (`<a:rPr b="1"/>`) — unlike wordprocessingml's
 * `<w:b/>` child element in docx.js. Different schema, so this can't share
 * that function.
 */
function renderRun(runNode) {
  const t = findChild(runNode, 't');
  const text = t ? textContent(t) : '';
  if (!text) return '';
  const rPr = findChild(runNode, 'rPr');
  const bold = rPr?.attrs.b === '1' || rPr?.attrs.b === 'true';
  const italic = rPr?.attrs.i === '1' || rPr?.attrs.i === 'true';
  if (bold && italic) return `***${text}***`;
  if (bold) return `**${text}**`;
  if (italic) return `*${text}*`;
  return text;
}

function renderParagraph(pNode) {
  return findChildren(pNode, 'r').map(renderRun).join('').trim();
}

/** All the text inside one shape's `<p:txBody>`/`<a:txBody>`, one paragraph per line. */
function shapeText(txBody) {
  return findChildren(txBody, 'p').map(renderParagraph).filter(Boolean).join('\n');
}

function renderTable(tblNode) {
  const rows = findChildren(tblNode, 'tr').map((tr) =>
    findChildren(tr, 'tc').map((tc) => {
      const txBody = findChild(tc, 'txBody');
      return (txBody ? shapeText(txBody) : '').replace(/\|/g, '\\|').replace(/\n/g, ' ');
    })
  );
  if (!rows.length) return '';
  const width = Math.max(...rows.map((r) => r.length));
  const padded = rows.map((r) => [...r, ...Array(width - r.length).fill('')]);
  const lines = [`| ${padded[0].join(' | ')} |`, `| ${padded[0].map(() => '---').join(' | ')} |`];
  for (const row of padded.slice(1)) lines.push(`| ${row.join(' | ')} |`);
  return lines.join('\n');
}

/** Slide paths, already resolved to real archive paths, in the deck's actual viewing order. */
function orderedSlidePaths(files) {
  const presoBuf = files['ppt/presentation.xml'];
  if (!presoBuf) throw new Error("That file doesn't look like a valid PowerPoint presentation.");
  const presoTree = parseXml(presoBuf.toString('utf8'));
  const sldIds = findAll(presoTree, 'sldId');
  const relTargets = resolveRelationshipTargets(files, 'ppt/_rels/presentation.xml.rels');
  return sldIds.map((s) => relTargets[s.attrs.id]).filter((p) => p && files[p]);
}

/** The relationship id (not target) of a slide's notes part, or null — found by TYPE, not by guessing a matching filename, since slide/notes numbering can drift independently. */
function notesSlidePath(files, slideRelsPath) {
  const buf = files[slideRelsPath];
  if (!buf) return null;
  const tree = parseXml(buf.toString('utf8'));
  const rel = findAll(tree, 'Relationship').find((r) => (r.attrs.Type || '').endsWith('/notesSlide'));
  if (!rel) return null;
  const partDir = slideRelsPath.replace(/_rels\/([^/]+)\.rels$/, '');
  const target = rel.attrs.Target || '';
  const segments = (partDir + target).split('/');
  const out = [];
  for (const seg of segments) {
    if (seg === '' || seg === '.') continue;
    if (seg === '..') out.pop();
    else out.push(seg);
  }
  return out.join('/');
}

/** A slide or notes-slide's shapes as text blocks, in document order — `<p:sp>`/other shape kinds, each holding a `<p:txBody>` or a `<a:tbl>`. */
function slideBlocks(slideTree) {
  const blocks = [];
  const spTree = findAll(slideTree, 'spTree')[0] || slideTree;
  for (const shape of spTree.children) {
    const tbl = findAll(shape, 'tbl')[0];
    if (tbl) {
      const md = renderTable(tbl);
      if (md) blocks.push(md);
      continue;
    }
    const txBody = findChild(shape, 'txBody');
    if (txBody) {
      const text = shapeText(txBody);
      if (text) blocks.push(text);
    }
  }
  return blocks;
}

/**
 * Reads a .pptx into `{markdown, images}` — one `## Slide N` section per
 * slide (shape text, tables, then that slide's speaker notes if any),
 * embedded pictures collected across the whole deck (capped, see media.js).
 */
export function pptxToMarkdown(buf) {
  const files = readZip(buf);
  const slidePaths = orderedSlidePaths(files);
  if (!slidePaths.length) throw new Error('That presentation has no readable slides.');

  const blocks = [];
  const embedIds = [];
  const embedRelsPaths = []; // parallel to embedIds — a picture's rels file lives per-slide, not archive-wide

  slidePaths.forEach((slidePath, i) => {
    const slideTree = parseXml(files[slidePath].toString('utf8'));
    blocks.push(`## Slide ${i + 1}`);
    blocks.push(...slideBlocks(slideTree));

    const relsPath = slidePath.replace(/^(.*)\/([^/]+)\.xml$/, '$1/_rels/$2.xml.rels');
    for (const blip of findAll(slideTree, 'blip')) {
      if (blip.attrs.embed) {
        embedIds.push(blip.attrs.embed);
        embedRelsPaths.push(relsPath);
      }
    }

    const notesPath = notesSlidePath(files, relsPath);
    if (notesPath && files[notesPath]) {
      const notesText = slideBlocks(parseXml(files[notesPath].toString('utf8'))).join('\n');
      if (notesText) blocks.push(`**Speaker notes:** ${notesText}`);
    }
  });

  // Images are resolved per-slide (each slide has its own .rels file), then
  // merged, capping the WHOLE deck rather than each slide individually.
  const images = [];
  const seenRelsPaths = new Set();
  for (const relsPath of embedRelsPaths) {
    if (seenRelsPaths.has(relsPath) || images.length >= 4) continue;
    seenRelsPaths.add(relsPath);
    const idsForThisSlide = embedIds.filter((_, idx) => embedRelsPaths[idx] === relsPath);
    images.push(...extractImages(files, relsPath, idsForThisSlide, { maxImages: 4 - images.length }));
  }

  const charts = extractAllCharts(files);
  if (charts.length) blocks.push('## Charts', ...charts);

  return { markdown: blocks.join('\n\n'), images };
}

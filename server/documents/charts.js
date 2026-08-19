// Reads a chart's underlying data — series names, categories, values — out
// of its chartN.xml part, rather than trying to render the picture.
//
// A chart doesn't store a picture at all: Office draws it fresh from cached
// numbers every time the file opens. Those numbers (<c:numCache>/<c:strCache>)
// are exactly what's wanted here, and reading them as a table is more useful
// to a model than a rasterized image of the chart would be.
//
// Least-verified module in this build (see the build's own plan notes) — no
// local file contains a chart, so this is written carefully against the
// documented OOXML chart schema and needs a real run to confirm, the same
// as CLAUDE.md's own honesty rule for the WSL sandbox backend.
//
// Deliberately NOT positioned within the surrounding document — resolving a
// chart's exact anchor point means walking through a separate drawing part
// this reader doesn't otherwise need. Every chart in the archive is instead
// collected into one appended section; see documents/index.js.
//
// A leaf module: imports xml.js only.

import { parseXml, findAll, findChild, textContent } from './xml.js';

function chartTitleText(chartSpaceNode) {
  const titleNode = findAll(chartSpaceNode, 'title')[0];
  if (!titleNode) return null;
  const text = findAll(titleNode, 't')
    .map((t) => textContent(t))
    .join('')
    .trim();
  return text || null;
}

/** The cached points out of a `<c:numCache>`/`<c:strCache>`, placed by their own `idx` (points can be sparse, same reasoning as xlsx.js's cell placement). */
function cachedPoints(cacheNode) {
  if (!cacheNode) return [];
  const out = [];
  for (const pt of findAll(cacheNode, 'pt')) {
    const idx = Number(pt.attrs.idx || 0);
    const v = findChild(pt, 'v');
    out[idx] = v ? textContent(v) : '';
  }
  return out;
}

// The cache element is never a DIRECT child of <c:tx>/<c:cat>/<c:val> — it's
// one level further in, wrapped in a <c:strRef> or <c:numRef> (e.g.
// tx > strRef > strCache > pt > v). Verified against a real chart part
// during this build: using findChild (direct children only) here silently
// found nothing at all, every time — findAll (recursive) is required.
function firstCache(node) {
  return findAll(node, 'strCache')[0] || findAll(node, 'numCache')[0] || null;
}

function seriesName(serNode) {
  const tx = findChild(serNode, 'tx');
  if (!tx) return null;
  const cache = firstCache(tx);
  if (cache) {
    const points = cachedPoints(cache);
    if (points.length) return points[0];
  }
  const v = findChild(tx, 'v');
  return v ? textContent(v) : null;
}

function seriesCategories(serNode) {
  const cat = findChild(serNode, 'cat');
  return cat ? cachedPoints(firstCache(cat)) : [];
}

function seriesValues(serNode) {
  const val = findChild(serNode, 'val');
  return val ? cachedPoints(firstCache(val)) : [];
}

/** One chart part as a Markdown table, or null if it has no series data to show (an empty/placeholder chart). */
export function chartXmlToMarkdown(xmlStr) {
  const tree = parseXml(xmlStr);
  const serNodes = findAll(tree, 'ser');
  if (!serNodes.length) return null;

  const title = chartTitleText(tree) || 'Untitled chart';
  const categories = seriesCategories(serNodes[0]);
  const series = serNodes.map((ser) => ({ name: seriesName(ser) || 'Series', values: seriesValues(ser) }));

  const header = ['Category', ...series.map((s) => s.name)];
  const rowCount = Math.max(categories.length, ...series.map((s) => s.values.length));
  const lines = [`### Chart: ${title}`, `| ${header.join(' | ')} |`, `| ${header.map(() => '---').join(' | ')} |`];
  for (let i = 0; i < rowCount; i++) {
    const row = [categories[i] ?? '', ...series.map((s) => s.values[i] ?? '')];
    lines.push(`| ${row.map((c) => String(c).replace(/\|/g, '\\|')).join(' | ')} |`);
  }
  return lines.join('\n');
}

/** Every chart part anywhere in the archive, rendered as Markdown tables. One bad chart part is skipped rather than failing the whole document read. */
export function extractAllCharts(files) {
  const chartPaths = Object.keys(files).filter((p) => /\/charts\/chart\d*\.xml$/.test(p));
  const out = [];
  for (const p of chartPaths) {
    try {
      const md = chartXmlToMarkdown(files[p].toString('utf8'));
      if (md) out.push(md);
    } catch {
      // one malformed chart part shouldn't sink the whole document
    }
  }
  return out;
}

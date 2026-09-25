/**
 * Markdown, parsed into plain data — no React, no DOM, so it is unit-tested
 * directly (`test/artifacts.test.mjs`). `components/artifacts/markdown.tsx`
 * turns this into elements.
 *
 * The property that matters is stated here and tested: nothing in this file
 * ever produces HTML. Text stays text; a link is only a link for http(s) and
 * mailto; an image reference becomes its alt text.
 */

export type Block =
  | { t: 'h'; level: number; text: string }
  | { t: 'p'; text: string }
  | { t: 'code'; text: string; lang: string }
  | { t: 'quote'; blocks: Block[] }
  | { t: 'list'; ordered: boolean; start: number; items: Block[][] }
  | { t: 'table'; head: string[]; rows: string[][]; align: ('left' | 'right' | 'center' | null)[] }
  | { t: 'hr' };

const BULLET = /^(\s*)([-*+])\s+(.*)$/;
const NUMBERED = /^(\s*)(\d{1,9})[.)]\s+(.*)$/;

function splitRow(line: string): string[] {
  let row = line.trim();
  if (row.startsWith('|')) row = row.slice(1);
  if (row.endsWith('|') && !row.endsWith('\\|')) row = row.slice(0, -1);
  const cells: string[] = [];
  let current = '';
  for (let i = 0; i < row.length; i += 1) {
    if (row[i] === '\\' && row[i + 1] === '|') { current += '|'; i += 1; continue; }
    if (row[i] === '|') { cells.push(current.trim()); current = ''; continue; }
    current += row[i];
  }
  cells.push(current.trim());
  return cells;
}

const isTableRule = (line: string) => /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(line);

export function parseBlocks(source: string): Block[] {
  const lines = source.replace(/\r\n?/g, '\n').replace(/\t/g, '    ').split('\n');
  const L = (k: number): string => lines[k] ?? '';
  const blocks: Block[] = [];
  let i = 0;
  while (i < lines.length) {
    const line = L(i);
    if (!line.trim()) { i += 1; continue; }

    const fence = line.match(/^\s*(```|~~~)\s*([\w+-]*)/);
    if (fence) {
      const body: string[] = [];
      i += 1;
      while (i < lines.length && !L(i).trim().startsWith(fence[1] ?? '```')) { body.push(L(i)); i += 1; }
      i += 1;
      blocks.push({ t: 'code', text: body.join('\n'), lang: fence[2] ?? '' });
      continue;
    }
    const heading = line.match(/^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$/);
    if (heading) { blocks.push({ t: 'h', level: (heading[1] ?? '#').length, text: heading[2] ?? '' }); i += 1; continue; }
    if (/^\s{0,3}([-*_])(\s*\1){2,}\s*$/.test(line)) { blocks.push({ t: 'hr' }); i += 1; continue; }

    if (/^\s{0,3}>/.test(line)) {
      const inner: string[] = [];
      while (i < lines.length && /^\s{0,3}>/.test(L(i))) {
        inner.push(L(i).replace(/^\s{0,3}>\s?/, '')); i += 1;
      }
      blocks.push({ t: 'quote', blocks: parseBlocks(inner.join('\n')) });
      continue;
    }

    if (line.includes('|') && i + 1 < lines.length && isTableRule(L(i + 1))) {
      const head = splitRow(line);
      const align = splitRow(L(i + 1)).map((cell) => {
        const left = cell.startsWith(':'); const right = cell.endsWith(':');
        return left && right ? 'center' : right ? 'right' : left ? 'left' : null;
      });
      i += 2;
      const rows: string[][] = [];
      while (i < lines.length && L(i).includes('|') && L(i).trim()) { rows.push(splitRow(L(i))); i += 1; }
      blocks.push({ t: 'table', head, rows, align });
      continue;
    }

    const listStart = line.match(BULLET) || line.match(NUMBERED);
    if (listStart) {
      const ordered = !line.match(BULLET);
      const baseIndent = (listStart[1] ?? '').length;
      const items: string[][] = [];
      while (i < lines.length) {
        const current = L(i);
        const marker = ordered ? current.match(NUMBERED) : current.match(BULLET);
        if (marker && (marker[1] ?? '').length === baseIndent) {
          items.push([marker[3] ?? '']); i += 1; continue;
        }
        const indent = current.match(/^(\s*)/)?.[1]?.length ?? 0;
        if (items.length && current.trim() && indent > baseIndent) {
          items[items.length - 1]?.push(current.slice(Math.min(indent, baseIndent + 2))); i += 1; continue;
        }
        if (items.length && !current.trim() && i + 1 < lines.length
            && (L(i + 1).match(/^(\s*)/)?.[1]?.length ?? 0) > baseIndent) {
          items[items.length - 1]?.push(''); i += 1; continue;
        }
        break;
      }
      blocks.push({ t: 'list', ordered, start: ordered ? Number(listStart[2] ?? 1) : 1,
                    items: items.map((item) => parseBlocks(item.join('\n'))) });
      continue;
    }

    const para: string[] = [];
    while (i < lines.length && L(i).trim() && !/^\s{0,3}(#{1,6}\s|>|```|~~~)/.test(L(i))
           && !L(i).match(BULLET) && !L(i).match(NUMBERED)
           && !(L(i).includes('|') && i + 1 < lines.length && isTableRule(L(i + 1)))) {
      para.push(L(i).trim()); i += 1;
    }
    if (!para.length) { para.push(line.trim()); i += 1; }
    blocks.push({ t: 'p', text: para.join('\n') });
  }
  return blocks;
}


export type Inline =
  | { t: 'text'; text: string }
  | { t: 'code'; text: string }
  | { t: 'image'; alt: string }
  | { t: 'link'; href: string; children: Inline[] }
  | { t: 'strong' | 'em' | 'del'; children: Inline[] };

export const SAFE_LINK = /^(https?:|mailto:)/i;
// A link target may hold one level of parentheses, so `(javascript:alert(1))`
// is consumed whole rather than leaving a stray ")".
const TARGET = String.raw`\(((?:[^()\s]|\([^()\s]*\))*)[^)]*\)`;
const INLINE = new RegExp(
  String.raw`(\`+)([\s\S]*?[^\`])\1(?!\`)` +
  String.raw`|!\[([^\]]*)\]` + TARGET +
  String.raw`|\[([^\]]+)\]` + TARGET +
  String.raw`|(\*\*|__)(?=\S)([\s\S]*?\S)\7` +
  String.raw`|(\*|_)(?=\S)([\s\S]*?\S)\9` +
  String.raw`|~~(?=\S)([\s\S]*?\S)~~` +
  String.raw`|(https?:\/\/[^\s<]+[^\s<.,;:!?)\]'"])`);

export function parseInline(text: string): Inline[] {
  const out: Inline[] = [];
  let rest = text;
  while (rest) {
    const match = rest.match(INLINE);
    if (!match || match.index === undefined) { out.push({ t: 'text', text: rest }); break; }
    if (match.index > 0) out.push({ t: 'text', text: rest.slice(0, match.index) });
    if (match[1]) out.push({ t: 'code', text: match[2] ?? '' });
    else if (match[0].startsWith('![')) out.push({ t: 'image', alt: match[3] ?? '' });
    else if (match[5] !== undefined) {
      const href = match[6] ?? '';
      const children = parseInline(match[5]);
      if (SAFE_LINK.test(href)) out.push({ t: 'link', href, children });
      else out.push(...children);
    } else if (match[7]) out.push({ t: 'strong', children: parseInline(match[8] ?? '') });
    else if (match[9]) out.push({ t: 'em', children: parseInline(match[10] ?? '') });
    else if (match[11] !== undefined) out.push({ t: 'del', children: parseInline(match[11]) });
    else if (match[12]) out.push({ t: 'link', href: match[12], children: [{ t: 'text', text: match[12] }] });
    rest = rest.slice(match.index + match[0].length);
  }
  return out;
}

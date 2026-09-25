/**
 * The artifact viewer's pure pieces — no React, no DOM — so they are unit-tested
 * directly (`test/artifacts.test.mjs`): the lock put on a model-written web page,
 * reading CSV, sizes and labels.
 */

import type { ArtifactKind } from './api-types';

/** What every model-written page is held to, whatever it says about itself.
 *  Scripts and styles may be inline (it is a self-contained page); nothing may be
 *  fetched, sent, framed, submitted or used as a base for relative links. */
export const FRAME_POLICY = [
  "default-src 'none'", "script-src 'unsafe-inline'", "style-src 'unsafe-inline'",
  'img-src data: blob:', 'font-src data:', 'media-src data: blob:', "connect-src 'none'",
  "form-action 'none'", "base-uri 'none'", "frame-src 'none'", "object-src 'none'",
  "worker-src 'none'", "manifest-src 'none'",
].join('; ');

/** The page with the policy as the very first thing the parser meets — before
 *  any of its own markup, so nothing it contains can run ahead of it or loosen it
 *  (a second policy can only ever add restrictions). */
export function lockedDocument(html: string): string {
  const meta = `<meta http-equiv="Content-Security-Policy" content="${FRAME_POLICY}">`;
  const doctype = html.match(/^\s*<!doctype[^>]*>/i);
  return doctype ? `${doctype[0]}${meta}${html.slice(doctype[0].length)}` : `${meta}${html}`;
}

const TEXT_KINDS: ArtifactKind[] = ['markdown', 'code', 'text', 'data', 'web'];

export function isTextKind(kind: ArtifactKind, name: string): boolean {
  return TEXT_KINDS.includes(kind) || (kind === 'image' && name.toLowerCase().endsWith('.svg'));
}

export function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(bytes < 10 * 1024 ? 1 : 0)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export const KIND_LABEL: Record<ArtifactKind, string> = {
  document: 'Word document', spreadsheet: 'Spreadsheet', presentation: 'Presentation',
  pdf: 'PDF', markdown: 'Markdown', web: 'Web page', image: 'Image', audio: 'Audio',
  data: 'Data', code: 'Code', text: 'Text', other: 'File',
};

export function parseDelimited(text: string, delimiter: string): string[][] {
  const rows: string[][] = [];
  let row: string[] = [];
  let cell = '';
  let quoted = false;
  for (let i = 0; i < text.length; i += 1) {
    const ch = text[i];
    if (quoted) {
      if (ch === '"' && text[i + 1] === '"') { cell += '"'; i += 1; }
      else if (ch === '"') quoted = false;
      else cell += ch;
    } else if (ch === '"' && cell === '') quoted = true;
    else if (ch === delimiter) { row.push(cell); cell = ''; }
    else if (ch === '\n' || ch === '\r') {
      if (ch === '\r' && text[i + 1] === '\n') i += 1;
      row.push(cell); rows.push(row); row = []; cell = '';
    } else cell += ch;
  }
  if (cell !== '' || row.length) { row.push(cell); rows.push(row); }
  return rows;
}


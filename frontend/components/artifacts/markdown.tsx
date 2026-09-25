'use client';

/**
 * Markdown to React elements, for showing a Markdown artifact (or a Word /
 * PowerPoint preview, which the backend returns as Markdown).
 *
 * In-repo rather than a library because this front end takes no UI dependencies
 * beyond Tailwind, and because the property that matters is easy to state and
 * check here: **it never produces raw HTML**. Every piece of text becomes a
 * React text node, so an artifact containing `<script>` shows those characters
 * rather than running them. Links are real links only for http(s) and mailto,
 * and open in a new tab; an image reference shows its alt text (an artifact is
 * shown offline and must not fetch from anywhere).
 *
 * Covers what models actually write: headings, paragraphs, bold/italic/strike,
 * inline code, fenced code, block quotes, bullet and numbered lists (nested by
 * indentation), tables and rules.
 */

import { Fragment, type ReactNode } from 'react';

import { type Block, type Inline, parseBlocks, parseInline } from '@/lib/markdown';

export function renderInline(text: string, keyPrefix = 'i'): ReactNode[] {
  return toNodes(parseInline(text), keyPrefix);
}

const LINK = 'text-accent underline underline-offset-2';

function toNodes(parts: Inline[], key: string): ReactNode[] {
  return parts.flatMap((part, index): ReactNode[] => {
    const k = `${key}-${index}`;
    switch (part.t) {
      case 'text': return withBreaks(part.text, k);
      case 'code': return [<code key={k} className="rounded bg-white/[0.07] px-1 py-0.5 font-mono text-[0.9em]">{part.text}</code>];
      case 'image': return [<span key={k} className="italic text-ink-muted">[image{part.alt ? `: ${part.alt}` : ''}]</span>];
      case 'link': return [<a key={k} href={part.href} target="_blank" rel="noopener noreferrer" className={LINK}>{toNodes(part.children, k)}</a>];
      case 'strong': return [<strong key={k} className="font-semibold text-ink">{toNodes(part.children, k)}</strong>];
      case 'em': return [<em key={k}>{toNodes(part.children, k)}</em>];
      case 'del': return [<s key={k}>{toNodes(part.children, k)}</s>];
      default: return [];
    }
  });
}

function withBreaks(text: string, key: string): ReactNode[] {
  return text.split('\n').flatMap((part, index) =>
    index === 0 ? [<Fragment key={`${key}-${index}`}>{part}</Fragment>]
      : [<br key={`${key}-br${index}`} />, <Fragment key={`${key}-${index}`}>{part}</Fragment>]);
}

const HEADING_CLASS = ['', 'text-[22px] font-semibold mt-5 mb-3', 'text-[18px] font-semibold mt-5 mb-2.5',
  'text-[16px] font-semibold mt-4 mb-2', 'text-[14.5px] font-semibold mt-4 mb-2',
  'text-[14px] font-semibold mt-3 mb-1.5', 'text-[13.5px] font-semibold mt-3 mb-1.5'];

function renderBlocks(blocks: Block[], key: string): ReactNode[] {
  return blocks.map((block, index) => {
    const k = `${key}-${index}`;
    switch (block.t) {
      case 'h': {
        const Tag = `h${Math.min(block.level + 1, 6)}` as 'h2';
        return <Tag key={k} className={`text-ink ${HEADING_CLASS[block.level]} first:mt-0`}>{renderInline(block.text, k)}</Tag>;
      }
      case 'p':
        return <p key={k} className="my-2.5 leading-relaxed">{renderInline(block.text, k)}</p>;
      case 'code':
        return (
          <pre key={k} className="my-3 overflow-x-auto rounded-lg border border-surface-border bg-black/30 p-3 font-mono text-[12.5px] leading-relaxed text-ink">
            <code>{block.text}</code>
          </pre>
        );
      case 'quote':
        return <blockquote key={k} className="my-3 border-l-2 border-accent/40 pl-3 text-ink-muted">{renderBlocks(block.blocks, k)}</blockquote>;
      case 'hr':
        return <hr key={k} className="my-5 border-surface-border" />;
      case 'list': {
        const items = block.items.map((item, j) => (
          <li key={`${k}-${j}`} className="my-1 pl-1 [&>p]:my-0">{renderBlocks(item, `${k}-${j}`)}</li>
        ));
        return block.ordered
          ? <ol key={k} start={block.start} className="my-2.5 list-decimal pl-6">{items}</ol>
          : <ul key={k} className="my-2.5 list-disc pl-6">{items}</ul>;
      }
      case 'table':
        return (
          <div key={k} className="my-3 overflow-x-auto">
            <table className="w-full border-collapse text-[13px]">
              <thead>
                <tr>{block.head.map((cell, j) => (
                  <th key={j} style={{ textAlign: block.align[j] ?? 'left' }}
                      className="border-b border-surface-border px-2.5 py-1.5 font-semibold text-ink">{renderInline(cell, `${k}h${j}`)}</th>
                ))}</tr>
              </thead>
              <tbody>
                {block.rows.map((row, r) => (
                  <tr key={r} className="odd:bg-white/[0.02]">
                    {block.head.map((_, j) => (
                      <td key={j} style={{ textAlign: block.align[j] ?? 'left' }}
                          className="border-b border-surface-border/60 px-2.5 py-1.5 align-top">{renderInline(row[j] ?? '', `${k}r${r}c${j}`)}</td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        );
      default:
        return null;
    }
  });
}

export function Markdown({ source, className = '' }: { source: string; className?: string }) {
  return (
    <div className={`text-[14px] text-ink/90 ${className}`} data-testid="markdown">
      {renderBlocks(parseBlocks(source), 'md')}
    </div>
  );
}

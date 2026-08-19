// A small markdown renderer that builds real DOM nodes.
//
// Everything Jarvis says in conversation is plain spoken text — no markdown,
// by design (see server/prompt.js). Plans, fact-check verdicts and research
// answers are the exception: they're documents you read on screen, and they
// come back with headings, lists and emphasis.
//
// Why not innerHTML: this text is written by an AI model, from web pages it
// fetched. It is not trusted content. Every screen in this app builds nodes
// with createElement/textContent for exactly that reason (see _helpers.js's
// header), and this file keeps that rule — the only HTML elements that ever
// exist here are ones this file creates itself, and all text goes in through
// textContent.
//
// Deliberately partial. It handles what models actually produce — headings,
// bullet and numbered lists, bold, italic, inline code, code blocks, links,
// blockquotes, rules — and renders anything else as plain text rather than
// growing into a full CommonMark implementation.

/** Inline formatting: **bold**, *italic*, `code`, and [text](url). Appends to `parent` as nodes, never as HTML. */
function renderInline(parent, text) {
  // One pass, alternating between matches and the plain text between them.
  const pattern = /(\*\*|__)(.+?)\1|(\*|_)(.+?)\3|`([^`]+)`|\[([^\]]+)\]\(([^)\s]+)\)/g;
  let lastIndex = 0;
  let match;

  while ((match = pattern.exec(text))) {
    if (match.index > lastIndex) {
      parent.appendChild(document.createTextNode(text.slice(lastIndex, match.index)));
    }

    if (match[2] !== undefined) {
      parent.appendChild(Object.assign(document.createElement('strong'), { textContent: match[2] }));
    } else if (match[4] !== undefined) {
      parent.appendChild(Object.assign(document.createElement('em'), { textContent: match[4] }));
    } else if (match[5] !== undefined) {
      parent.appendChild(Object.assign(document.createElement('code'), { textContent: match[5] }));
    } else if (match[6] !== undefined) {
      const href = match[7];
      // Only http/https become real links. A javascript: or data: URL in
      // model-written text must never become a clickable element.
      if (/^https?:\/\//i.test(href)) {
        const a = document.createElement('a');
        a.href = href;
        a.textContent = match[6];
        a.target = '_blank';
        a.rel = 'noopener noreferrer';
        parent.appendChild(a);
      } else {
        parent.appendChild(document.createTextNode(match[6]));
      }
    }

    lastIndex = pattern.lastIndex;
  }

  if (lastIndex < text.length) {
    parent.appendChild(document.createTextNode(text.slice(lastIndex)));
  }
}

function flushList(container, listState) {
  if (!listState.el) return;
  container.appendChild(listState.el);
  listState.el = null;
  listState.ordered = false;
}

/**
 * Renders markdown into `container` (which is cleared first).
 * Returns the container, so it can be used inline.
 */
export function renderMarkdown(container, markdown) {
  container.innerHTML = '';
  container.classList.add('md');

  const lines = String(markdown || '').split(/\r?\n/);
  const listState = { el: null, ordered: false };
  let paragraph = null;
  let codeBlock = null;

  const flushParagraph = () => {
    if (paragraph) {
      container.appendChild(paragraph);
      paragraph = null;
    }
  };

  for (const line of lines) {
    // Fenced code blocks swallow everything until the closing fence, so no
    // markdown inside them is interpreted.
    const fence = /^\s*```/.test(line);
    if (fence) {
      if (codeBlock) {
        container.appendChild(codeBlock);
        codeBlock = null;
      } else {
        flushParagraph();
        flushList(container, listState);
        const pre = document.createElement('pre');
        pre.appendChild(document.createElement('code'));
        codeBlock = pre;
      }
      continue;
    }
    if (codeBlock) {
      const code = codeBlock.firstChild;
      code.textContent += (code.textContent ? '\n' : '') + line;
      continue;
    }

    if (!line.trim()) {
      flushParagraph();
      flushList(container, listState);
      continue;
    }

    const heading = /^(#{1,6})\s+(.*)$/.exec(line);
    if (heading) {
      flushParagraph();
      flushList(container, listState);
      // Headings inside a document start at h3 — the screen's own <h2> is
      // the section title, and jumping back up to h1 inside it would be
      // wrong for anyone navigating by headings.
      const level = Math.min(6, heading[1].length + 2);
      const el = document.createElement(`h${level}`);
      renderInline(el, heading[2].trim());
      container.appendChild(el);
      continue;
    }

    if (/^\s*([-*_])\s*\1\s*\1[\s*_-]*$/.test(line)) {
      flushParagraph();
      flushList(container, listState);
      container.appendChild(document.createElement('hr'));
      continue;
    }

    const quote = /^\s*>\s?(.*)$/.exec(line);
    if (quote) {
      flushParagraph();
      flushList(container, listState);
      const bq = document.createElement('blockquote');
      renderInline(bq, quote[1]);
      container.appendChild(bq);
      continue;
    }

    const bullet = /^\s*[-*+]\s+(.*)$/.exec(line);
    const numbered = /^\s*\d+[.)]\s+(.*)$/.exec(line);
    if (bullet || numbered) {
      flushParagraph();
      const ordered = Boolean(numbered);
      if (!listState.el || listState.ordered !== ordered) {
        flushList(container, listState);
        listState.el = document.createElement(ordered ? 'ol' : 'ul');
        listState.ordered = ordered;
      }
      const li = document.createElement('li');
      renderInline(li, (bullet ? bullet[1] : numbered[1]).trim());
      listState.el.appendChild(li);
      continue;
    }

    flushList(container, listState);
    if (!paragraph) paragraph = document.createElement('p');
    else paragraph.appendChild(document.createTextNode(' '));
    renderInline(paragraph, line.trim());
  }

  if (codeBlock) container.appendChild(codeBlock);
  flushParagraph();
  flushList(container, listState);

  return container;
}

/** A <div class="md"> containing the rendered markdown — the common case. */
export function markdownBlock(markdown, className = 'md-doc') {
  const el = document.createElement('div');
  el.className = className;
  renderMarkdown(el, markdown);
  return el;
}

/**
 * A read-only box holding exactly `text`, with a Copy button. Used for the
 * generated handoff prompt, where the whole point is getting the text onto
 * the clipboard unchanged — so it's deliberately NOT markdown-rendered.
 */
export function copyableBlock(text, { label = 'Copy' } = {}) {
  const wrapper = document.createElement('div');
  wrapper.className = 'copy-block';

  const pre = document.createElement('pre');
  pre.className = 'copy-block-text';
  pre.textContent = String(text || '');

  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'btn btn-primary copy-block-btn';
  btn.textContent = label;
  btn.addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText(String(text || ''));
      btn.textContent = 'Copied';
    } catch {
      // Clipboard access can be refused; selecting the text is a workable
      // fallback and tells the user what to do next.
      const range = document.createRange();
      range.selectNodeContents(pre);
      const sel = window.getSelection();
      sel.removeAllRanges();
      sel.addRange(range);
      btn.textContent = 'Press Ctrl+C';
    }
    setTimeout(() => {
      btn.textContent = label;
    }, 2500);
  });

  wrapper.append(btn, pre);
  return wrapper;
}

// The artifact viewer's pure logic: what a model-written file can and cannot
// become on screen.
//
// Markdown never becomes HTML and never makes a script link; a web page always
// gets its lock first; a CSV reads the way a spreadsheet would read it.

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { FRAME_POLICY, lockedDocument, parseDelimited, formatSize } from '../.test-build/lib/artifacts.js';
import { parseBlocks, parseInline } from '../.test-build/lib/markdown.js';

// --- Markdown ------------------------------------------------------------------------------

test('headings, lists, a table and a code block are recognised', () => {
  const blocks = parseBlocks('# Title\n\n- a\n- b\n\n| x | y |\n|---|--:|\n| 1 | 2 |\n\n```py\nprint(1)\n```');
  assert.deepEqual(blocks.map((b) => b.t), ['h', 'list', 'table', 'code']);
  assert.equal(blocks[2].align[1], 'right');
  assert.equal(blocks[3].text, 'print(1)');
});

test('a nested list stays nested under its item', () => {
  const [list] = parseBlocks('1. one\n2. two\n   - inner\n3. three');
  assert.equal(list.ordered, true);
  assert.equal(list.items.length, 3);
  assert.equal(list.items[1][1].t, 'list');
});

test('raw HTML stays text: nothing in the output is markup', () => {
  const inline = parseInline("<script>alert('x')</script> <b>hi</b>");
  assert.deepEqual(inline, [{ t: 'text', text: "<script>alert('x')</script> <b>hi</b>" }]);
});

test('only http(s) and mailto become links; a script link is plain text, whole', () => {
  const safe = parseInline('[ok](https://example.com)');
  assert.equal(safe[0].t, 'link');
  const bad = parseInline('see [click](javascript:alert(1)) now');
  assert.ok(bad.every((part) => part.t !== 'link'));
  assert.equal(bad.map((p) => p.text).join(''), 'see click now');
});

test('an image reference becomes its alt text and fetches nothing', () => {
  assert.deepEqual(parseInline('![a chart](https://evil.example/x.png)'), [{ t: 'image', alt: 'a chart' }]);
});

test('bold, italic, code and strikethrough', () => {
  const parts = parseInline('**b** *i* `c` ~~d~~');
  assert.deepEqual(parts.filter((p) => p.t !== 'text').map((p) => p.t), ['strong', 'em', 'code', 'del']);
});

// --- the lock on a web page ------------------------------------------------------------------

test('the policy is the first thing in the page, after any doctype', () => {
  const locked = lockedDocument('<!DOCTYPE html><html><head><script>x()</script>');
  assert.ok(locked.startsWith('<!DOCTYPE html><meta http-equiv="Content-Security-Policy"'));
  assert.ok(locked.indexOf('Content-Security-Policy') < locked.indexOf('<script>'));
  assert.ok(lockedDocument('<p>hi</p>').startsWith('<meta http-equiv="Content-Security-Policy"'));
});

test('the policy lets nothing in or out', () => {
  for (const rule of ["default-src 'none'", "connect-src 'none'", "form-action 'none'", "base-uri 'none'",
                      "frame-src 'none'"]) {
    assert.ok(FRAME_POLICY.includes(rule), rule);
  }
  assert.ok(!/https?:/.test(FRAME_POLICY), 'no network origin is ever allowed');
});

// --- CSV -------------------------------------------------------------------------------------

test('quoted commas, doubled quotes and CRLF read like a spreadsheet would', () => {
  assert.deepEqual(parseDelimited('a,b\r\n"x, y","say ""hi"""\r\n', ','),
                   [['a', 'b'], ['x, y', 'say "hi"']]);
  assert.deepEqual(parseDelimited('a\tb\n1\t2', '\t'), [['a', 'b'], ['1', '2']]);
});

test('sizes read naturally', () => {
  assert.equal(formatSize(512), '512 B');
  assert.equal(formatSize(2048), '2.0 KB');
  assert.equal(formatSize(3 * 1024 * 1024), '3.0 MB');
});

// Where a streaming reply gets broken up so it can be spoken as it arrives.
//
// The fiddly part of both playback queues, and pure, so it is tested directly.
// Getting it wrong is heard two ways: choppy speech if it flushes too eagerly,
// or seconds of silence at the start of a reply if it waits for a whole long
// sentence.

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { takeSpeakable } from '../.test-build/lib/voice/chunker.js';

test('whole sentences come out in order, the tail stays behind', () => {
  const { pieces, rest, flushed } = takeSpeakable('One. Two! Three? And a half', true);
  assert.deepEqual(pieces, ['One.', 'Two!', 'Three?']);
  assert.equal(rest, 'And a half');
  assert.equal(flushed, true);
});

test('nothing goes early once the reply is already speaking', () => {
  // After the first flush, a clause is not enough — full sentences only, or the
  // rest of the reply sounds chopped up.
  const { pieces, rest } = takeSpeakable('a long clause, and more to come', true);
  assert.deepEqual(pieces, []);
  assert.equal(rest, 'a long clause, and more to come');
});

test('the OPENING clause is allowed to go early, exactly once', () => {
  // Otherwise a long opening sentence leaves the assistant silent for seconds
  // while its text is already on screen.
  const first = takeSpeakable('That depends on a few things, and the first one is', false);
  assert.deepEqual(first.pieces, ['That depends on a few things,']);
  assert.equal(first.flushed, true);
  assert.equal(first.rest, 'and the first one is');

  // A second clause in the same reply waits for a real sentence end.
  const second = takeSpeakable(`${first.rest} whether, in fact, it rained`, first.flushed);
  assert.deepEqual(second.pieces, [], 'it went early a second time');
});

test('a tiny opening clause is not worth flushing on its own', () => {
  // "Sure," alone sounds choppy and costs a whole synthesis round trip.
  const { pieces, flushed } = takeSpeakable('Sure, ', false);
  assert.deepEqual(pieces, []);
  assert.equal(flushed, false);
});

test('an opening with no punctuation at all still gets bounded', () => {
  const long = 'a'.repeat(95);
  const { pieces, rest, flushed } = takeSpeakable(long, false);
  assert.deepEqual(pieces, [long]);
  assert.equal(rest, '');
  assert.equal(flushed, true);
});

test('an empty buffer produces nothing and changes nothing', () => {
  assert.deepEqual(takeSpeakable('', false), { pieces: [], rest: '', flushed: false });
  assert.deepEqual(takeSpeakable('   ', false), { pieces: [], rest: '   ', flushed: false });
});

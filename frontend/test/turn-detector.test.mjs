// When has someone finished talking?
//
// Worth testing directly because it is the one part of the voice layer that is
// pure: no microphone, no audio context, no network. The wait it produces is
// paid on EVERY voice turn before the request is even sent, so getting it wrong
// is felt as latency on the fast path and as being cut off on the slow one.

import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  computeWaitMs, looksComplete, SilenceWatcher, WAIT_MS,
} from '../.test-build/lib/voice/turn-detector.js';

test('a sentence trailing off waits longest', () => {
  assert.equal(computeWaitMs('so I was thinking um'), WAIT_MS.filler);
  assert.equal(computeWaitMs('open the'), WAIT_MS.trailing);
  assert.equal(computeWaitMs('remind me to call her and'), WAIT_MS.trailing);
});

test('a finished question answers almost at once', () => {
  assert.equal(computeWaitMs('what time is it'), WAIT_MS.complete);
  assert.equal(computeWaitMs('That is all.'), WAIT_MS.complete);
});

test('a plain command counts as finished', () => {
  // The most common thing anyone says to an assistant. Without this every clean
  // command fell through to the slow generic wait — the most common case,
  // missed entirely.
  assert.equal(computeWaitMs('open youtube'), WAIT_MS.complete);
  assert.equal(computeWaitMs('search for cheap flights'), WAIT_MS.complete);
});

test('one bare word is never a finished thought', () => {
  // "Open" is as likely to be the start of "Open YouTube".
  assert.equal(looksComplete('Open'), false);
  assert.equal(computeWaitMs('Open'), WAIT_MS.default);
});

test('nothing said yet gets the ordinary wait', () => {
  assert.equal(computeWaitMs(''), WAIT_MS.default);
  assert.equal(computeWaitMs(null), WAIT_MS.default);
  assert.equal(computeWaitMs('   '), WAIT_MS.default);
});

test('a trailing word beats a full stop that is not there', () => {
  assert.equal(looksComplete('what about the'), false);
});

// --- silence, not elapsed time ------------------------------------------------

/** A microphone that says whatever the test tells it to. */
function mic(loud = false) {
  return { loud, getLevel: () => (mic.loud ? 1 : 0), isSpeaking() { return this.loud; } };
}

test('the wait is measured against real silence, not wall-clock time', () => {
  // The gap this whole class exists to close: restarting a timeout on every
  // transcript event measures time since the recogniser last spoke up, which is
  // not the same as how long the room has been quiet. Someone still audibly
  // talking, in a gap the recogniser emits nothing for, used to have their turn
  // ended out from under them.
  const source = mic();
  const watcher = new SilenceWatcher(source, { sampleMs: 10 });
  let fired = 0;
  watcher.arm(500, () => { fired += 1; });

  source.loud = true;
  for (let now = 0; now <= 2000; now += 100) watcher.tick(now);
  assert.equal(fired, 0, 'it fired while the mic was still hearing speech');

  source.loud = false;
  watcher.tick(2100);
  watcher.tick(2400);
  assert.equal(fired, 0, 'it fired before the full wait had passed in silence');
  watcher.tick(2700);
  assert.equal(fired, 1);
});

test('re-arming restarts the quiet clock', () => {
  const source = mic();
  const watcher = new SilenceWatcher(source, { sampleMs: 10 });
  let fired = 0;
  watcher.arm(300, () => { fired += 1; });

  watcher.tick(0);
  watcher.tick(200);
  watcher.arm(300, () => { fired += 1; });  // another transcript arrived
  watcher.tick(250);
  assert.equal(fired, 0, 'the earlier quiet stretch was counted after re-arming');
  watcher.tick(600);
  assert.equal(fired, 1);
});

test('it never assumes the room is already quiet when armed', () => {
  // The next sample decides, which costs one tick and avoids firing instantly
  // off a stale reading.
  const source = mic();
  const watcher = new SilenceWatcher(source, { sampleMs: 10 });
  let fired = 0;
  watcher.arm(0, () => { fired += 1; });
  assert.equal(fired, 0);
  watcher.tick(0);
  assert.equal(fired, 1);
});

test('cancelling stops it firing at all', () => {
  const source = mic();
  const watcher = new SilenceWatcher(source, { sampleMs: 10 });
  let fired = 0;
  watcher.arm(100, () => { fired += 1; });
  watcher.cancel();
  watcher.tick(0);
  watcher.tick(500);
  assert.equal(fired, 0);
});

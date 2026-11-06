// playQueue.peekNext() (queue-advance UX): consumers that render "Up next"
// (the results card's countdown strip) need to know WHAT follows without
// reaching into queue internals. Extract the playQueue IIFE from app.js and
// drive it against a playSong stub.
'use strict';
const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

function makeQueue() {
    const src = fs.readFileSync(path.join(__dirname, '..', '..', 'static', 'app.js'), 'utf8');
    const start = src.indexOf('window.feedBack.playQueue = (function () {');
    assert.ok(start !== -1, 'playQueue IIFE found in app.js');
    const end = src.indexOf('})();', start);
    assert.ok(end !== -1, 'playQueue IIFE terminator found');
    const iife = src.slice(start, end + 5);
    const played = [];
    // A minimal core bus: the queue listens for song:ended (one advance per
    // finished song), so tests end the current song before advancing.
    const handlers = {};
    const bus = {
        on: (type, fn) => { (handlers[type] = handlers[type] || []).push(fn); },
        emit: (type, detail) => { (handlers[type] || []).forEach((fn) => fn({ detail })); },
    };
    const sandbox = {
        window: {
            feedBack: bus,
            playSong: (fn, arr, opts) => played.push({ fn, arr, opts }),
            fbNotify: null,
        },
        encodeURIComponent,
    };
    // eslint-disable-next-line no-new-func
    new Function('window', 'encodeURIComponent', iife)(sandbox.window, encodeURIComponent);
    const q = sandbox.window.feedBack.playQueue;
    // The current song ends, then the queue moves on (what the player does).
    const endAndAdvance = () => { bus.emit('song:ended', {}); return q.advance(); };
    return { q, played, bus, endAndAdvance };
}

test('peekNext exposes the following track without mutating the queue', () => {
    const { q, played, endAndAdvance } = makeQueue();
    assert.strictEqual(q.peekNext(), null);              // idle queue → null
    q.start(['a.sloppak', 'b.sloppak', 'c.sloppak'], { source: 'My list' });
    assert.deepStrictEqual(q.peekNext(), { filename: 'b.sloppak', index: 1, total: 3 });
    assert.deepStrictEqual(q.peekNext(), { filename: 'b.sloppak', index: 1, total: 3 }); // pure
    assert.strictEqual(played.length, 1);                // peeking never plays
    endAndAdvance();
    assert.deepStrictEqual(q.peekNext(), { filename: 'c.sloppak', index: 2, total: 3 });
    endAndAdvance();
    assert.strictEqual(q.peekNext(), null);              // last track → nothing next
    assert.strictEqual(q.remaining(), 0);
});

test('peekNext is null after clear', () => {
    const { q } = makeQueue();
    q.start(['a.sloppak', 'b.sloppak']);
    q.clear();
    assert.strictEqual(q.peekNext(), null);
});

// A gig/album/playlist queue must survive a playSong wrapper that drops the
// options object.
//
// The queue tells playSong "don't clear the queue I'm driving" via
// options.fromQueue. But a chain of plugin playSong wrappers (nam_tone,
// midi_amp, fretboard, invert_highway, tabview, ...) forward only
// (filename, arrangement) and silently drop the 3rd arg. With just the in-band
// flag, playSong cleared the queue the instant its first song started, so a gig
// never advanced (feedBack#… tester: "Passports does not advance in the song
// queue"). The queue now also raises an out-of-band flag, _consumeInternalPlay(),
// which playSong honours regardless of the wrapper chain.

// The real clear-guard from session.js, driven against the queue.
function clearGuard(win, options) {
    const pq = win.feedBack && win.feedBack.playQueue;
    const queueDriven = (options && options.fromQueue)
        || (pq && typeof pq._consumeInternalPlay === 'function' && pq._consumeInternalPlay());
    if (!queueDriven && pq) pq.clear();
}

test('the queue survives a playSong that drops the options arg', () => {
    const { q } = makeQueue();
    // Rebind the queue's window.playSong to a wrapper that forwards ONLY
    // (filename, arrangement) — exactly the plugin bug — and runs the real guard.
    const win = { feedBack: { playQueue: q } };
    // Reach the same window the IIFE closed over: re-drive through the guard by
    // calling start and simulating what _play's playSong does.
    // We can't rebind the closed-over window, so instead assert the out-of-band
    // signal directly: _play sets it, and the guard consumes it.
    q.start(['a.sloppak', 'b.sloppak', 'c.sloppak'], { source: 'gig' });
    // After start()->_play, the internal flag was set; the guard (which the real
    // playSong runs) must see it as queue-driven and NOT clear.
    win.feedBack.playQueue = q;
    clearGuard(win, undefined /* wrapper dropped options */);
    assert.strictEqual(q.active(), true, 'a dropped options arg must not clear the queue');
    assert.strictEqual(q.remaining(), 2, 'the queue must still have its remaining tracks');
});

test('_consumeInternalPlay is one-shot — a later MANUAL play still clears', () => {
    const { q } = makeQueue();
    q.start(['a.sloppak', 'b.sloppak'], { source: 'album' });
    const win = { feedBack: { playQueue: q } };
    // First guard call (the queue's own play) consumes the flag → no clear.
    clearGuard(win, undefined);
    assert.strictEqual(q.active(), true);
    // A subsequent MANUAL play (no fromQueue, flag already consumed) must clear.
    clearGuard(win, undefined);
    assert.strictEqual(q.active(), false, 'a manual play after the queue play must abandon the queue');
});

test('fromQueue in options still works on its own (in-band path)', () => {
    const { q } = makeQueue();
    q.start(['a.sloppak', 'b.sloppak'], { source: 'gig' });
    // consume the internal flag first so ONLY options.fromQueue is under test
    q._consumeInternalPlay();
    const win = { feedBack: { playQueue: q } };
    clearGuard(win, { fromQueue: true });
    assert.strictEqual(q.active(), true, 'options.fromQueue alone must still keep the queue');
});

// isContinuation(): true for song 2..N of a set, false for the first song / a
// standalone play. The venue uses it to fly in once on arrival, then carry the
// room between songs instead of replaying the arrival flyover every track
// (tester: "it showed the flyover intro again" on a gig's second song).
test('isContinuation is false on the first song, true after advancing', () => {
    const { q, endAndAdvance } = makeQueue();
    assert.strictEqual(q.isContinuation(), false, 'idle queue is not a continuation');
    q.start(['a.sloppak', 'b.sloppak', 'c.sloppak'], { source: 'gig' });
    assert.strictEqual(q.isContinuation(), false, 'the FIRST song of a set is an arrival, not a continuation');
    endAndAdvance();
    assert.strictEqual(q.isContinuation(), true, 'song 2 is a continuation — no re-flyover');
    endAndAdvance();
    assert.strictEqual(q.isContinuation(), true, 'song 3 too');
    q.clear();
    assert.strictEqual(q.isContinuation(), false, 'a cleared queue is not a continuation');
});

// One advance per finished song. A results card's own Up-Next countdown
// (note_detect) advanced the queue a SECOND time after the playthrough had
// already moved on, skipping the song that had just started (track 2 of an
// album jumped to track 3 a few seconds in).
test('advance moves on only once per finished song: a second / stale advance is ignored', () => {
    const { q, played, bus } = makeQueue();
    q.start(['a.sloppak', 'b.sloppak', 'c.sloppak'], { source: 'album' });
    assert.strictEqual(q.advance(), false, 'mid-song advance (song not ended) is refused');
    assert.strictEqual(q.peekNext().index, 1, 'still on track 1');
    bus.emit('song:ended', {});
    assert.strictEqual(q.currentEnded(), true);
    assert.strictEqual(q.advance(), true, 'after the end it moves on');
    assert.strictEqual(q.currentEnded(), false, 'the new track has not ended');
    assert.strictEqual(q.advance(), false, 'a second advance for the same end is ignored (no skip)');
    assert.strictEqual(played.length, 2, 'b played once, c never');
    assert.deepStrictEqual(q.peekNext(), { filename: 'c.sloppak', index: 2, total: 3 });
});

test('a duplicate song:ended (HTML + JUCE emitters) still allows exactly one advance', () => {
    const { q, bus } = makeQueue();
    q.start(['a.sloppak', 'b.sloppak', 'c.sloppak']);
    bus.emit('song:ended', {});
    bus.emit('song:ended', {});
    assert.strictEqual(q.advance(), true);
    assert.strictEqual(q.advance(), false);
    assert.strictEqual(q.remaining(), 1);
});

test('advance({ force: true }) still skips mid-song on purpose; a stale advance on the last track never clears the set', () => {
    const { q, bus } = makeQueue();
    q.start(['a.sloppak', 'b.sloppak']);
    assert.strictEqual(q.advance({ force: true }), true, 'explicit skip');
    assert.strictEqual(q.advance(), false, 'stale advance while the last track plays');
    assert.strictEqual(q.active(), true, 'the set was not cleared under the player');
    bus.emit('song:ended', {});
    assert.strictEqual(q.advance(), false, 'past the last track: nothing next');
    assert.strictEqual(q.active(), false, 'and the finished set clears');
});

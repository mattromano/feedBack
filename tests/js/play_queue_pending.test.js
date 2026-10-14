// playQueue "up next" (the pending list) + follow-the-arrangement on advance.
//
// v3's player is fullscreen and leaving it abandons the active queue, so a
// queue is BUILT in the library (enqueue) and started afterwards
// (startPending). The list persists per browser, de-dupes, and emits
// 'queue:changed'. On an advance onto an unpinned slot the queue raises a
// one-shot arrangement-name hint (the part the user was just playing) that
// playSong forwards to the highway WS. Extract the playQueue IIFE from app.js
// and drive it against stubs.
'use strict';
const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const KEY = 'feedBack.playQueue.pending';

function memStorage(seed) {
    const m = new Map(Object.entries(seed || {}));
    return {
        getItem: (k) => (m.has(k) ? m.get(k) : null),
        setItem: (k, v) => { m.set(k, String(v)); },
        _map: m,
    };
}

function makeQueue(opts) {
    const o = opts || {};
    const src = fs.readFileSync(path.join(__dirname, '..', '..', 'static', 'app.js'), 'utf8');
    const start = src.indexOf('window.feedBack.playQueue = (function () {');
    assert.ok(start !== -1, 'playQueue IIFE found in app.js');
    const end = src.indexOf('})();', start);
    assert.ok(end !== -1, 'playQueue IIFE terminator found');
    const iife = src.slice(start, end + 5);
    const played = [];
    const events = [];
    const toasts = [];
    const win = {
        feedBack: { emit: (name, detail) => events.push({ name, detail }) },
        playSong: (fn, arr, popts) => played.push({ fn: decodeURIComponent(fn), arr, opts: popts }),
        fbNotify: { show: (t) => toasts.push(t) },
        localStorage: 'storage' in o ? o.storage : memStorage(),
        highway: { getSongInfo: () => win._songInfo },
        _songInfo: o.songInfo || {},
    };
    new Function('window', 'encodeURIComponent', iife)(win, encodeURIComponent);
    return { q: win.feedBack.playQueue, played, events, toasts, win };
}

test('enqueue collects into pending without playing, and de-dupes', () => {
    const { q, played, toasts } = makeQueue();
    assert.strictEqual(q.enqueue(['a.sloppak', { filename: 'b.sloppak', title: 'B', artist: 'X' }]), 2);
    assert.strictEqual(q.enqueue(['a.sloppak', 'c.sloppak']), 1, 'a is already waiting');
    assert.deepStrictEqual(q.pending().map((it) => it.filename), ['a.sloppak', 'b.sloppak', 'c.sloppak']);
    assert.deepStrictEqual(q.pending()[1], { filename: 'b.sloppak', title: 'B', artist: 'X' });
    assert.strictEqual(played.length, 0, 'building the queue never plays');
    assert.strictEqual(q.active(), false);
    assert.strictEqual(toasts.length, 2);
    assert.strictEqual(toasts[0].title, 'Added to queue');
    assert.strictEqual(q.enqueue([null, '', { title: 'no file' }]), 0, 'junk items are ignored');
});

test('pending() is a copy — callers cannot mutate the queue', () => {
    const { q } = makeQueue();
    q.enqueue(['a.sloppak']);
    const p = q.pending();
    p.push({ filename: 'zzz' });
    p[0].filename = 'hacked';
    assert.deepStrictEqual(q.pending().map((it) => it.filename), ['a.sloppak']);
});

test('removePending / movePending / clearPending edit the list and emit queue:changed', () => {
    const { q, events } = makeQueue();
    q.enqueue(['a', 'b', 'c', 'd'], { silent: true });
    events.length = 0;
    assert.strictEqual(q.movePending(3, 0), true);
    assert.deepStrictEqual(q.pending().map((it) => it.filename), ['d', 'a', 'b', 'c']);
    assert.strictEqual(q.movePending(0, 9), false, 'out-of-range move is a no-op');
    assert.strictEqual(q.removePending(1), true);
    assert.deepStrictEqual(q.pending().map((it) => it.filename), ['d', 'b', 'c']);
    assert.strictEqual(q.removePending(7), false);
    q.clearPending();
    assert.deepStrictEqual(q.pending(), []);
    assert.ok(events.length >= 3 && events.every((e) => e.name === 'queue:changed'));
    assert.strictEqual(events[events.length - 1].detail.pending, 0);
});

test('the pending list persists per browser and survives a reload', () => {
    const storage = memStorage();
    const first = makeQueue({ storage });
    first.q.enqueue([{ filename: 'a.sloppak', title: 'A' }, 'b.sloppak'], { silent: true });
    assert.ok(storage._map.has(KEY));
    const second = makeQueue({ storage });   // a fresh page load
    assert.deepStrictEqual(second.q.pending().map((it) => it.filename), ['a.sloppak', 'b.sloppak']);
    assert.strictEqual(second.q.pending()[0].title, 'A');
});

test('corrupt or unavailable storage degrades to an empty list, never throws', () => {
    const bad = makeQueue({ storage: memStorage({ [KEY]: '{not json' }) });
    assert.deepStrictEqual(bad.q.pending(), []);
    const throwing = {
        getItem: () => { throw new Error('denied'); },
        setItem: () => { throw new Error('denied'); },
    };
    const { q } = makeQueue({ storage: throwing });
    assert.strictEqual(q.enqueue(['a'], { silent: true }), 1);
    assert.deepStrictEqual(q.pending().map((it) => it.filename), ['a']);
    const none = makeQueue({ storage: undefined });
    assert.strictEqual(none.q.enqueue(['a'], { silent: true }), 1);
});

test('startPending hands the list to start() and empties it', () => {
    const { q, played } = makeQueue();
    q.enqueue(['a', 'b', 'c'], { silent: true });
    assert.strictEqual(q.startPending(), true);
    assert.deepStrictEqual(q.pending(), []);
    assert.strictEqual(q.active(), true);
    assert.strictEqual(q.source(), 'Queue');
    assert.deepStrictEqual(played.map((p) => p.fn), ['a']);
    assert.strictEqual(played[0].opts.fromQueue, true);
    q.advance(); q.advance();
    assert.deepStrictEqual(played.map((p) => p.fn), ['a', 'b', 'c']);
    assert.strictEqual(makeQueue().q.startPending(), false, 'nothing queued → nothing starts');
});

test('startPending({ from }) plays from that song and leaves the ones above waiting', () => {
    const { q, played } = makeQueue();
    q.enqueue(['a', 'b', 'c'], { silent: true });
    q.startPending({ from: 1 });
    assert.deepStrictEqual(played.map((p) => p.fn), ['b']);
    assert.strictEqual(q.remaining(), 1);
    assert.deepStrictEqual(q.pending().map((it) => it.filename), ['a']);
});

test('startPending({ shuffle }) shuffles via start()', () => {
    const { q, played } = makeQueue();
    const orig = Math.random;
    Math.random = () => 0;   // Fisher-Yates with j=0 every step → rotates
    try {
        q.enqueue(['a', 'b', 'c'], { silent: true });
        q.startPending({ shuffle: true });
    } finally { Math.random = orig; }
    while (q.hasNext()) q.advance();
    assert.deepStrictEqual(played.map((p) => p.fn).sort(), ['a', 'b', 'c']);
    assert.notDeepStrictEqual(played.map((p) => p.fn), ['a', 'b', 'c']);
});

test('enqueue while a queue is playing appends to the live queue, not pending', () => {
    const { q, played } = makeQueue();
    q.start(['a', 'b']);
    assert.strictEqual(q.enqueue(['c', 'b'], { silent: true }), 1, 'b is already coming up');
    assert.deepStrictEqual(q.pending(), []);
    assert.strictEqual(q.remaining(), 2);
    while (q.hasNext()) q.advance();
    assert.deepStrictEqual(played.map((p) => p.fn), ['a', 'b', 'c']);
});

test('clearing the active queue (a manual play) keeps the pending list', () => {
    const { q } = makeQueue();
    q.enqueue(['x', 'y'], { silent: true });
    q.start(['a', 'b']);       // e.g. Play album while songs wait in "up next"
    q.clear();                 // playSong's clear-guard on a manual play
    assert.strictEqual(q.active(), false);
    assert.deepStrictEqual(q.pending().map((it) => it.filename), ['x', 'y']);
});

// Follow-the-arrangement: song 2..N of a queue opens on the part the user was
// playing. playSong consumes the hint once (see session.js) and sends it as the
// WS `arrangement_name`, only when no explicit arrangement index was given.
test('an advance raises a one-shot arrangement hint from the current song', () => {
    const { q, win } = makeQueue({ songInfo: { arrangement: 'PART RHYTHM', arrangement_smart_name: 'Rhythm' } });
    q.start(['a', 'b', 'c']);
    assert.strictEqual(q._consumeArrangementHint(), null, 'the first song keeps the normal default');
    q.advance();
    assert.strictEqual(q._consumeArrangementHint(), 'Rhythm', 'smart name preferred');
    assert.strictEqual(q._consumeArrangementHint(), null, 'consumed once');
    win._songInfo = { arrangement: 'Bass' };
    q.advance();
    assert.strictEqual(q._consumeArrangementHint(), 'Bass', 'falls back to the raw name');
});

test('a pinned slot arrangement wins over the follow hint', () => {
    const { q, played } = makeQueue({ songInfo: { arrangement_smart_name: 'Rhythm' } });
    q.start(['a', 'b', 'c'], { arrangements: [0, 2, undefined] });
    q.advance();
    assert.strictEqual(played[1].arr, 2);
    assert.strictEqual(q._consumeArrangementHint(), null, 'pinned → no hint');
    q.advance();
    assert.strictEqual(played[2].arr, undefined);
    assert.strictEqual(q._consumeArrangementHint(), 'Rhythm', 'unpinned → follow');
});

test('no highway / no song info → no hint (never throws)', () => {
    const { q, win } = makeQueue();
    win.highway = null;
    q.start(['a', 'b']);
    q.advance();
    assert.strictEqual(q._consumeArrangementHint(), null);
});

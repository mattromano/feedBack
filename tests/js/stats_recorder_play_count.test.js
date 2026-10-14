// Native play count (feedBack#49): the stats recorder flags ONE flush per song
// load as a play once the session has really played for PLAY_COUNT_MIN_S
// (scored or not), and flags a scored run that reached the natural end with no
// misses as a full combo. Loads the REAL static/v3/stats-recorder.js into a vm
// with a fake event bus, a controllable clock and a recording fetch.

'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const SRC = fs.readFileSync(path.join(__dirname, '..', '..', 'static', 'v3', 'stats-recorder.js'), 'utf8');

function boot({ failPosts = 0 } = {}) {
    const handlers = {};
    const emitted = [];
    const posts = [];
    let now = 1000;   // the recorder treats a 0 span start as "not playing"
    let failing = failPosts;
    const feedBack = {
        on(ev, fn) { (handlers[ev] = handlers[ev] || []).push(fn); },
        emit(ev, detail) { emitted.push({ ev, detail }); (handlers[ev] || []).forEach((fn) => fn({ detail })); },
    };
    const sandbox = {
        window: { feedBack },
        performance: { now: () => now },
        fetch: async (url, opts) => {
            const body = JSON.parse(opts.body);
            posts.push(body);
            if (failing > 0) { failing--; throw new Error('offline'); }
            return { ok: true, json: async () => ({ stats: {} }) };
        },
        console,
    };
    vm.createContext(sandbox);
    vm.runInContext(SRC, sandbox);
    const fire = (ev, detail) => (handlers[ev] || []).forEach((fn) => fn({ detail: detail || {} }));
    const flush = () => new Promise((r) => setImmediate(r));
    return { fire, posts, emitted, flush, advance: (s) => { now += s * 1000; } };
}

test('an unscored play past 30 s is counted once, on the flush that crosses it', async () => {
    const r = boot();
    r.fire('song:loading', { filename: 'a.sloppak', arrangement: 0 });
    r.fire('song:play');
    r.advance(20);
    r.fire('song:pause', { time: 20 });          // 20 s: not a play yet
    await r.flush();
    r.fire('song:resume');
    r.advance(15);
    r.fire('song:pause', { time: 35 });          // 35 s total: this flush counts
    await r.flush();
    r.fire('song:resume');
    r.advance(60);
    r.fire('song:ended', { time: 95 });          // same load: never counted twice
    await r.flush();
    const counted = r.posts.filter((b) => b.count_play);
    assert.equal(counted.length, 1);
    assert.equal(counted[0].lastPlayPosition, 35);
    assert.ok(r.posts.every((b) => !b.full_combo));
    assert.equal(r.emitted.filter((e) => e.ev === 'stats:play-counted').length, 1);
    // An unscored play must not masquerade as a scored run.
    assert.equal(r.emitted.filter((e) => e.ev === 'stats:recorded').length, 0);
});

test('a short play is not counted, and a new load starts a fresh count', async () => {
    const r = boot();
    r.fire('song:loading', { filename: 'a.sloppak', arrangement: 0 });
    r.fire('song:play');
    r.advance(10);
    r.fire('song:stop', { time: 10 });
    await r.flush();
    assert.ok(r.posts.every((b) => !b.count_play));
    r.fire('song:loading', { filename: 'a.sloppak', arrangement: 0 });
    r.fire('song:play');
    r.advance(31);
    r.fire('song:stop', { time: 31 });
    await r.flush();
    assert.equal(r.posts.filter((b) => b.count_play).length, 1);
});

test('a miss-free scored run that reaches the end is a full combo', async () => {
    const r = boot();
    r.fire('song:loading', { filename: 'fc.sloppak', arrangement: 1 });
    r.fire('song:play');
    for (let i = 0; i < 12; i++) r.fire('note:hit');
    r.advance(120);
    r.fire('song:ended', { time: 120 });
    await r.flush();
    const scored = r.posts.find((b) => b.score != null);
    assert.ok(scored, 'scored POST sent');
    assert.equal(scored.full_combo, true);
    assert.equal(scored.count_play, true);
    assert.equal(scored.arrangement, 1);
});

test('a miss, or quitting before the end, is not a full combo', async () => {
    const r = boot();
    r.fire('song:loading', { filename: 'x.sloppak', arrangement: 0 });
    r.fire('song:play');
    r.fire('note:hit'); r.fire('note:miss'); r.fire('note:hit');
    r.advance(100);
    r.fire('song:ended', { time: 100 });
    await r.flush();
    r.fire('song:loading', { filename: 'y.sloppak', arrangement: 0 });
    r.fire('song:play');
    r.fire('note:hit'); r.fire('note:hit');
    r.advance(100);
    r.fire('song:stop', { time: 100 });          // user quit: not a natural end
    await r.flush();
    assert.ok(r.posts.every((b) => !b.full_combo));
    assert.equal(r.posts.filter((b) => b.count_play).length, 2);   // both were still plays
});

test('a failed POST re-arms the count so a later flush carries it', async () => {
    const r = boot({ failPosts: 1 });
    r.fire('song:loading', { filename: 'a.sloppak', arrangement: 0 });
    r.fire('song:play');
    r.advance(40);
    r.fire('song:pause', { time: 40 });          // counted, but the POST fails
    await r.flush();
    r.fire('song:resume');
    r.advance(5);
    r.fire('song:pause', { time: 45 });          // re-armed: this one carries it
    await r.flush();
    const counted = r.posts.filter((b) => b.count_play);
    assert.equal(counted.length, 2);
    assert.equal(counted[1].lastPlayPosition, 45);
});

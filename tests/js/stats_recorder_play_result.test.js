// RockHeroSmith publishes `play:result` (play-layer contract §3) for drums /
// 5-fret / vocals plays. The recorder posts ONE scored session per player so
// stats, XP and progression fire; real guitar players are skipped (note
// detection already records them) and practice runs are never scored.
// Loads the REAL static/v3/stats-recorder.js into a vm with a fake bus.

'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const SRC = fs.readFileSync(path.join(__dirname, '..', '..', 'static', 'v3', 'stats-recorder.js'), 'utf8');

function boot() {
    const handlers = {};
    const emitted = [];
    const posts = [];
    const notified = [];
    const feedBack = {
        on(ev, fn) { (handlers[ev] = handlers[ev] || []).push(fn); },
        emit(ev, detail) { emitted.push({ ev, detail }); (handlers[ev] || []).forEach((fn) => fn({ detail })); },
    };
    const sandbox = {
        window: {
            feedBack,
            v3Progression: { notify: async (s) => { notified.push(s); }, get: () => ({}) },
        },
        performance: { now: () => 1000 },
        fetch: async (url, opts) => {
            posts.push({ url, body: JSON.parse(opts.body) });
            return { ok: true, json: async () => ({ stats: {}, progression: { ok: 1 } }) };
        },
        console,
    };
    vm.createContext(sandbox);
    vm.runInContext(SRC, sandbox);
    const fire = (ev, detail) => (handlers[ev] || []).forEach((fn) => fn({ detail: detail || {} }));
    const flush = () => new Promise((r) => setImmediate(r));
    return { fire, posts, emitted, notified, flush };
}

function player(over) {
    return Object.assign({
        player: 'drums', instrument: 'drums', part: 'drums', input: 'kit',
        difficulty: 'expert', arrangement: null, score: 98765, stars: 4,
        accuracy: 0.95, hits: 190, misses: 10, overhits: 3, bestStreak: 120,
        fullCombo: false, sections: [],
    }, over || {});
}

function result(players, over) {
    return Object.assign({
        filename: 'song.feedpak', sessionId: 'sess-1', band: players.length > 1,
        practice: false, players, durationSec: 200,
    }, over || {});
}

test('a solo drums result posts one scored part session', async () => {
    const r = boot();
    r.fire('play:result', result([player()]));
    await r.flush();
    const stats = r.posts.filter((p) => p.url === '/api/stats');
    assert.equal(stats.length, 1);
    const b = stats[0].body;
    assert.equal(b.filename, 'song.feedpak');
    assert.equal(b.part, 'drums');
    assert.equal(b.difficulty, 'expert');
    assert.equal(b.score, 98765);
    assert.equal(b.accuracy, 0.95);
    assert.equal(b.hits, 190);
    assert.equal(b.misses, 10);
    assert.equal(b.bestStreak, 120);
    assert.equal(b.stars, 4);
    assert.equal(b.session_id, 'sess-1');
    assert.equal(b.player, 'drums');
    assert.equal(b.input, 'kit');
    assert.equal(b.full_combo, undefined);
    // Play time and play counts ride the normal song:* path — never doubled here.
    assert.equal(b.seconds, undefined);
    assert.equal(b.count_play, undefined);
    assert.equal(b.lastPlayPosition, undefined);
    const rec = r.emitted.filter((e) => e.ev === 'stats:recorded');
    assert.equal(rec.length, 1);
    assert.equal(rec[0].detail.filename, 'song.feedpak');
    assert.equal(rec[0].detail.part, 'drums');
    assert.equal(rec[0].detail.difficulty, 'expert');
    assert.equal(r.notified.length, 1);
});

test('a band posts each non-guitar player and skips real guitar', async () => {
    const r = boot();
    r.fire('play:result', result([
        player(),
        player({ player: 'guitar', instrument: 'guitar', part: 'lead', input: 'audio' }),
        player({ player: 'ctl1', instrument: 'five_fret', part: 'bass', input: 'controller',
                 difficulty: 'hard', arrangement: 2, fullCombo: true, accuracy: 1, misses: 0 }),
        player({ player: 'vocals', instrument: 'vocals', part: 'vocals', input: 'mic' }),
    ]));
    await r.flush();
    const parts = r.posts.filter((p) => p.url === '/api/stats').map((p) => p.body.part);
    assert.deepEqual(parts, ['drums', 'five_fret:bass', 'vocals']);
    const ff = r.posts.find((p) => p.body.part === 'five_fret:bass').body;
    assert.equal(ff.arrangement, 2);
    assert.equal(ff.full_combo, true);
    assert.equal(ff.player, 'ctl1');
});

test('practice results and empty runs are never scored', async () => {
    const r = boot();
    r.fire('play:result', result([player()], { practice: true }));
    r.fire('play:result', result([player({ hits: 0, misses: 0 })], { sessionId: 's2' }));
    r.fire('play:result', { players: [player()] });   // no filename
    await r.flush();
    assert.equal(r.posts.length, 0);
    assert.equal(r.emitted.filter((e) => e.ev === 'stats:recorded').length, 0);
});

test('a repeated play:result for the same session posts once', async () => {
    const r = boot();
    r.fire('play:result', result([player()]));
    r.fire('play:result', result([player()]));
    await r.flush();
    assert.equal(r.posts.length, 1);
});

test('a drums play:result does not disturb the guitar note tally', async () => {
    const r = boot();
    r.fire('song:loading', { filename: 'song.feedpak', arrangement: 0 });
    r.fire('note:hit');
    r.fire('note:hit');
    r.fire('note:miss');
    r.fire('play:result', result([player()]));
    r.fire('song:ended', { time: 100 });
    await r.flush();
    const guitar = r.posts.filter((p) => p.body.part === undefined && p.body.score !== undefined);
    assert.equal(guitar.length, 1);
    assert.equal(guitar[0].body.hits, 2);
    assert.equal(guitar[0].body.misses, 1);
    assert.equal(r.posts.filter((p) => p.body.part === 'drums').length, 1);
});

test('a band result emits stats:recorded once, after every post settles', async () => {
    const r = boot();
    r.fire('play:result', result([
        player(),
        player({ player: 'ctl1', instrument: 'five_fret', part: 'bass', input: 'controller', arrangement: 2 }),
        player({ player: 'vocals', instrument: 'vocals', part: 'vocals', input: 'mic' }),
    ]));
    await r.flush();
    await r.flush();
    assert.equal(r.posts.length, 3);
    const rec = r.emitted.filter((e) => e.ev === 'stats:recorded');
    assert.equal(rec.length, 1, 'one venue-crowd reaction per result, not per player');
    assert.equal(rec[0].detail.filename, 'song.feedpak');
    assert.equal(rec[0].detail.parts.map((p) => p.part).join(','), 'drums,five_fret:bass,vocals');
    assert.equal(r.notified.length, 3);
});

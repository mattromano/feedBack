'use strict';
// Achievements: RockHeroSmith's `play:judgment` hits/misses feed the same
// per-note Feat counters as note detection (minus real guitar, which note
// detection already counts), and `play:result` posts the drum/controller
// Feat deltas (full combo, expert clear, streak, pro drums).

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const SRC = fs.readFileSync(path.join(__dirname, '..', '..', '..', '..', 'plugins', 'achievements', 'screen.js'), 'utf8');

function boot() {
    const handlers = {};
    const posts = [];
    const feedBack = {
        on(ev, fn) { (handlers[ev] = handlers[ev] || []).push(fn); },
        emit() {},
    };
    const document = {
        readyState: 'complete',
        addEventListener() {},
        getElementById: () => null,
        querySelector: () => null,
    };
    const window = { feedBack, requestAnimationFrame: (fn) => fn() };
    const sandbox = {
        window, document, console,
        localStorage: { getItem: () => null, setItem() {} },
        fetch: async (url, opts) => {
            if (opts && opts.body) posts.push({ url, body: JSON.parse(opts.body) });
            return { ok: true, json: async () => ({}) };
        },
    };
    sandbox.window.document = document;
    vm.createContext(sandbox);
    vm.runInContext(SRC, sandbox);
    const fire = (ev, detail) => (handlers[ev] || []).forEach((fn) => fn({ detail: detail || {} }));
    const flush = () => new Promise((r) => setImmediate(r));
    const activity = () => posts.filter((p) => p.url.endsWith('/activity')).map((p) => p.body);
    return { fire, flush, activity };
}

function j(kind, over) {
    return Object.assign({ filename: 'a.feedpak', sessionId: 's', player: 'drums',
        instrument: 'drums', part: 'drums', input: 'kit', kind, points: 25 }, over || {});
}

test('play:judgment hits and misses count like note:hit / note:miss', async () => {
    const r = boot();
    r.fire('song:loading', { filename: 'a.feedpak' });
    for (let i = 0; i < 5; i++) r.fire('play:judgment', j('hit'));
    r.fire('play:judgment', j('overhit'));          // breaks the streak
    r.fire('play:judgment', j('hit'));
    r.fire('play:judgment', j('hit'));
    r.fire('play:judgment', j('sustain'));          // not a note
    r.fire('play:judgment', j('bonus'));            // not a note
    r.fire('play:judgment', j('hit', { player: 'guitar', instrument: 'guitar' }));  // note detect's job
    r.fire('song:ended', { time: 60 });
    await r.flush();
    const a = r.activity();
    assert.equal(a.length, 1);
    assert.equal(a[0].notes, 7);
    assert.equal(a[0].in_song_streak, 5);
    assert.equal(a[0].song_done, 1);
});

test('streaks are per player in a band', async () => {
    const r = boot();
    r.fire('song:loading', { filename: 'a.feedpak' });
    for (let i = 0; i < 4; i++) {
        r.fire('play:judgment', j('hit'));
        r.fire('play:judgment', j('miss', { player: 'ctl1', instrument: 'five_fret', part: 'guitar' }));
    }
    r.fire('song:ended', { time: 60 });
    await r.flush();
    assert.equal(r.activity()[0].in_song_streak, 4);
    assert.equal(r.activity()[0].notes, 4);
});

test('judgments outside a song are ignored', async () => {
    const r = boot();
    r.fire('play:judgment', j('hit'));
    r.fire('song:ended', { time: 1 });
    await r.flush();
    assert.equal(r.activity().length, 0);
});

test('play:result posts controller feat deltas, skipping guitar and practice', async () => {
    const r = boot();
    const players = [
        { player: 'drums', instrument: 'drums', part: 'drums', difficulty: 'expert',
          hits: 300, misses: 0, bestStreak: 300, fullCombo: true, proDrums: true },
        { player: 'ctl1', instrument: 'five_fret', part: 'guitar', difficulty: 'hard',
          hits: 200, misses: 3, bestStreak: 150, fullCombo: false },
        { player: 'guitar', instrument: 'guitar', part: 'lead', difficulty: 'expert',
          hits: 900, misses: 0, bestStreak: 900, fullCombo: true },
    ];
    r.fire('play:result', { filename: 'a.feedpak', sessionId: 's1', practice: true, players });
    await r.flush();
    assert.equal(r.activity().length, 0);
    r.fire('play:result', { filename: 'a.feedpak', sessionId: 's1', practice: false, players });
    r.fire('play:result', { filename: 'a.feedpak', sessionId: 's1', practice: false, players });  // repeat
    await r.flush();
    const a = r.activity();
    assert.equal(a.length, 1);
    assert.equal(a[0].full_combos, 1);
    assert.equal(a[0].expert_clears, 1);
    assert.equal(a[0].controller_streak, 300);
    assert.equal(a[0].pro_drums_clears, 1);
    assert.ok(!a[0].song_done, 'a result is not another finished song');
    assert.ok(!a[0].notes, 'notes already came from play:judgment');
});

test('a failed run is not a clear', async () => {
    const r = boot();
    r.fire('play:result', { filename: 'a.feedpak', sessionId: 's2', practice: false, players: [
        { player: 'drums', instrument: 'drums', part: 'drums', difficulty: 'expert',
          hits: 10, misses: 50, bestStreak: 8, fullCombo: false, failed: true },
    ] });
    await r.flush();
    const a = r.activity();
    assert.equal(a.length, 1);
    assert.equal(a[0].expert_clears, 0);
    assert.equal(a[0].controller_streak, 8);
});

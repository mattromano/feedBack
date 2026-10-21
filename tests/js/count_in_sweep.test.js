'use strict';
// The count-in rewind sweep drives the shared display clock (display-time.js)
// and a user seek/pause during it cancels it. Imports the real count-in.js and
// transport.js under node with stubbed browser globals.

const { test, before, beforeEach, afterEach } = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const { pathToFileURL } = require('node:url');

const JS = path.join(__dirname, '..', '..', 'static', 'js');
const load = (f) => import(pathToFileURL(path.join(JS, f)).href);

let ci, dt, tr, S, loops;
let events = [], rafQueue, audioEl, seekResolve;

before(async () => {
    audioEl = { paused: false, currentTime: 0, pause() { this.paused = true; }, play: () => Promise.resolve(), addEventListener() {} };
    const el = () => ({ style: {}, remove() {}, options: [], value: '', textContent: '', className: '', classList: { add() {}, remove() {}, toggle() {} }, setAttribute() {}, querySelector: () => null, appendChild() {} });
    global.window = global;
    global.document = { getElementById: (id) => (id === 'audio' ? audioEl : el()), createElement: el, body: { appendChild() {} } };
    global.localStorage = { getItem: () => null, setItem() {}, removeItem() {} };
    global.requestAnimationFrame = (fn) => { rafQueue.push(fn); return rafQueue.length; };
    global.cancelAnimationFrame = () => {};
    // playClick builds oscillators; a no-op Web Audio graph is enough.
    const node = () => ({ connect() {}, start() {}, stop() {}, frequency: {}, gain: { setValueAtTime() {}, exponentialRampToValueAtTime() {} } });
    global.AudioContext = class { constructor() { this.currentTime = 0; this.destination = {}; } createOscillator() { return node(); } createGain() { return node(); } };
    global.window.highway = { setTime() {}, getTime: () => 0, getBPM: () => 120, getNotes: () => [], getChords: () => [] };
    const bus = new EventTarget();
    bus.emit = (event, detail) => bus.dispatchEvent(new CustomEvent(event, { detail }));
    bus.on = (event, fn) => bus.addEventListener(event, fn);
    global.window.feedBack = bus;
    dt = await load('display-time.js');
    S = (await load('player-state.js')).S;
    tr = await load('transport.js');
    (await load('host.js')).configureHost({ _updateEditRegionBtn() {} });
    loops = await load('loops.js');
    ci = await load('count-in.js');
    loops.setLoop(10, 20);
    for (const n of ['count-in:sweep', 'song:pause', 'song:seek']) {
        window.feedBack.on(n, (e) => events.push({ n, d: e.detail }));
    }
});

beforeEach(() => {
    dt.clearSweepTime();
    ci._cancelCountIn();
    events = [];
    rafQueue = [];
    S.isPlaying = true;
});

afterEach(() => { ci._cancelCountIn(); });

async function startSweep() {
    await ci.startCountIn();
    assert.equal(rafQueue.length, 1, 'rewind frame scheduled');
}

test('a sweep frame sets display time and emits count-in:sweep', async () => {
    await startSweep();
    rafQueue.shift()(performance.now() + 100);
    const sweeps = events.filter((e) => e.n === 'count-in:sweep');
    assert.equal(sweeps.length, 1);
    assert.equal(dt.getDisplayTime(() => 99), sweeps[0].d.time);
    assert.notEqual(sweeps[0].d.time, 99);
});

test('_cancelCountIn clears the sweep time', async () => {
    await startSweep();
    rafQueue.shift()(performance.now() + 100);
    assert.notEqual(dt.getDisplayTime(() => 99), 99);
    ci._cancelCountIn();
    assert.equal(dt.getDisplayTime(() => 99), 99);
});

test('a user seek during the sweep cancels it and returns display time to audio', async () => {
    await startSweep();
    rafQueue.shift()(performance.now() + 100);
    tr._audioSeek(5, 'seek-by');
    assert.equal(dt.getDisplayTime(() => 99), 99);
    assert.equal(ci.isCountingIn(), false);
    assert.equal(S.isPlaying, false);
    assert.equal(events.filter((e) => e.n === 'song:pause').length, 1);
});

test('play/pause during the sweep cancels it', async () => {
    await startSweep();
    rafQueue.shift()(performance.now() + 100);
    tr.togglePlay().catch(() => {});
    assert.equal(dt.getDisplayTime(() => 99), 99);
    assert.equal(ci.isCountingIn(), false);
});

test('the count-in loop-wrap seek does not cancel the count-in', async () => {
    await startSweep();
    tr._audioSeek(10, 'loop-wrap');
    assert.equal(ci.isCountingIn(), true);
});

const flush = async () => { for (let i = 0; i < 6; i++) await new Promise((r) => setImmediate(r)); };

// Run `fn` with setTimeout captured into a queue so the count-in's click
// ticks can be stepped without waiting out real beats.
async function withFakeTimers(fn) {
    const real = global.setTimeout;
    const timers = [];
    global.setTimeout = (cb) => { timers.push(cb); return timers.length; };
    try { return await fn(timers); } finally { global.setTimeout = real; }
}

function onBus(name, fn) {
    window.feedBack.on(name, fn);
    return () => window.feedBack.removeEventListener(name, fn);
}

test('programmatic seeks do not cancel the count-in', async () => {
    for (const reason of ['loop-set', 'plugin-command', 'playback-command', 'session-resume',
        'arrangement-restore', 'playback-start', 'audio-element-shim', 'jump-fix', null]) {
        await startSweep();
        tr._audioSeek(5, reason);
        assert.equal(ci.isCountingIn(), true, `reason ${reason} must not cancel`);
        ci._cancelCountIn();
        rafQueue = [];
        await flush();
    }
});

test('a song-restart seek cancels the count-in', async () => {
    await startSweep();
    tr._audioSeek(0, 'song-restart');
    assert.equal(ci.isCountingIn(), false);
});

test('a setLoop seek inside a loop:restart handler leaves the count-in running and it still plays', async () => {
    let played = 0;
    const origPlay = audioEl.play;
    audioEl.play = () => { played++; return Promise.resolve(); };
    // note_detect's drill conductor re-arms the loop on every wrap.
    const off = onBus('loop:restart', () => { loops.setLoop(10, 20); });
    try {
        await startSweep();
        await withFakeTimers(async (timers) => {
            rafQueue.shift()(performance.now() + 10_000);
            await flush();
            assert.equal(ci.isCountingIn(), true, 'still counting in after loop:restart');
            S.isPlaying = false;
            while (timers.length) timers.shift()();
            await flush();
        });
        assert.equal(played, 1, 'audio started after the count');
        assert.equal(S.isPlaying, true);
        assert.equal(events.filter((e) => e.n === 'song:pause').length, 0);
    } finally {
        off();
        audioEl.play = origPlay;
    }
});

test('even a user seek reason inside the count-in\'s own loop:restart dispatch does not cancel it', async () => {
    const off = onBus('loop:restart', () => { tr._audioSeek(10, 'seek-by'); });
    try {
        await startSweep();
        await withFakeTimers(async () => {
            rafQueue.shift()(performance.now() + 10_000);
            await flush();
            assert.equal(ci.isCountingIn(), true);
        });
    } finally { off(); }
});

test('the immediate (section-practice) path survives a setLoop in its loop:restart handler', async () => {
    const off = onBus('loop:restart', () => { loops.setLoop(10, 20); });
    try {
        await withFakeTimers(async () => {
            await ci.startCountIn({ immediate: true });
            assert.equal(ci.isCountingIn(), true);
            await flush();
            assert.equal(ci.isCountingIn(), true);
        });
    } finally { off(); }
});

test('the sweep stays pinned at loop A until the loop-wrap seek lands', async () => {
    await startSweep();
    await withFakeTimers(async () => {
        rafQueue.shift()(performance.now() + 10_000);
        // The loop-wrap seek is still queued: audio has not moved yet.
        assert.equal(dt.getDisplayTime(() => 19.9), 10);
        await flush();
        assert.equal(dt.getDisplayTime(() => 19.9), 19.9, 'released once the seek resolves');
    });
});

test('an off-target loop-wrap seek releases the pinned sweep', async () => {
    await startSweep();
    // Mimic a clamp: the element ignores the write and stays near loop B.
    Object.defineProperty(audioEl, 'currentTime', { configurable: true, get: () => 19.9, set() {} });
    try {
        await withFakeTimers(async () => {
            rafQueue.shift()(performance.now() + 10_000);
            assert.equal(dt.getDisplayTime(() => 19.9), 10);
            await flush();
            assert.equal(ci.isCountingIn(), false, 'aborted');
            assert.equal(dt.getDisplayTime(() => 19.9), 19.9);
        });
    } finally {
        delete audioEl.currentTime;
        audioEl.currentTime = 0;
    }
});

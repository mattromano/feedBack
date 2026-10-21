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

let ci, dt, tr, S;
let events = [], rafQueue, audioEl, seekResolve;

before(async () => {
    audioEl = { paused: false, currentTime: 0, pause() { this.paused = true; }, play: () => Promise.resolve(), addEventListener() {} };
    const el = () => ({ style: {}, options: [], value: '', textContent: '', className: '', classList: { add() {}, remove() {}, toggle() {} }, setAttribute() {}, querySelector: () => null, appendChild() {} });
    global.window = global;
    global.document = { getElementById: (id) => (id === 'audio' ? audioEl : el()), createElement: el, body: { appendChild() {} } };
    global.localStorage = { getItem: () => null, setItem() {}, removeItem() {} };
    global.requestAnimationFrame = (fn) => { rafQueue.push(fn); return rafQueue.length; };
    global.cancelAnimationFrame = () => {};
    global.window.highway = { setTime() {}, getTime: () => 0, getBPM: () => 120, getNotes: () => [], getChords: () => [] };
    const bus = new EventTarget();
    bus.emit = (event, detail) => bus.dispatchEvent(new CustomEvent(event, { detail }));
    bus.on = (event, fn) => bus.addEventListener(event, fn);
    global.window.feedBack = bus;
    dt = await load('display-time.js');
    S = (await load('player-state.js')).S;
    tr = await load('transport.js');
    (await load('host.js')).configureHost({ _updateEditRegionBtn() {} });
    const loops = await load('loops.js');
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

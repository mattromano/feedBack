'use strict';
// Core count-in announces its overlay on the bus (count-in:tick / count-in:hide)
// so a band stage can draw its own 3-2-1. Stubs follow count_in_sweep.test.js.

const { test, before, beforeEach, afterEach } = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const { pathToFileURL } = require('node:url');

const JS = path.join(__dirname, '..', '..', 'static', 'js');
const load = (f) => import(pathToFileURL(path.join(JS, f)).href);

let ci, events = [], appended = [];

before(async () => {
    const audioEl = { paused: false, currentTime: 0, pause() { this.paused = true; }, play: () => Promise.resolve(), addEventListener() {} };
    const el = () => ({ id: '', style: {}, remove() {}, options: [], value: '', textContent: '', className: '', innerHTML: '', classList: { add() {}, remove() {}, toggle() {} }, setAttribute() {}, querySelector: () => null, appendChild() {} });
    global.window = global;
    global.document = {
        getElementById: (id) => (id === 'audio' ? audioEl : el()),
        createElement: el,
        body: { appendChild(e) { appended.push(e); } },
    };
    global.localStorage = { getItem: () => null, setItem() {}, removeItem() {} };
    global.requestAnimationFrame = () => 1;
    global.cancelAnimationFrame = () => {};
    global.window.highway = { setTime() {}, getTime: () => 0, getBPM: () => 120, getNotes: () => [], getChords: () => [] };
    const bus = new EventTarget();
    bus.emit = (event, detail) => bus.dispatchEvent(new CustomEvent(event, { detail }));
    bus.on = (event, fn) => bus.addEventListener(event, fn);
    global.window.feedBack = bus;
    (await load('host.js')).configureHost({ _updateEditRegionBtn() {} });
    ci = await load('count-in.js');
    for (const n of ['count-in:tick', 'count-in:hide']) {
        window.feedBack.on(n, (e) => events.push({ n, d: e.detail }));
    }
});

beforeEach(() => { ci._cancelCountIn(); events = []; appended = []; });
afterEach(() => { ci._cancelCountIn(); });

test('showCountOverlay emits count-in:tick with n and total and ids the overlay', () => {
    ci.showCountOverlay(2, 4);
    const ticks = events.filter((e) => e.n === 'count-in:tick');
    assert.equal(ticks.length, 1);
    assert.deepEqual(ticks[0].d, { n: 2, total: 4 });
    assert.equal(appended.length, 1);
    assert.equal(appended[0].id, 'count-in-overlay');
});

test('hideCountOverlay(true) emits completed; a second hide emits nothing', () => {
    ci.showCountOverlay(1, 4);
    events = [];
    ci.hideCountOverlay(true);
    assert.deepEqual(events, [{ n: 'count-in:hide', d: { completed: true } }]);
    ci.hideCountOverlay(true);
    assert.equal(events.length, 1);
});

test('cancelling a count-in reports it as not completed', () => {
    ci.showCountOverlay(1, 4);
    events = [];
    ci._cancelCountIn();
    const hides = events.filter((e) => e.n === 'count-in:hide');
    assert.deepEqual(hides.map((h) => h.d), [{ completed: false }]);
});

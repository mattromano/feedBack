// Build-a-Band: fret-number labels scale independently of note size so they stay
// readable in a narrower lane. labelFont() (static/js/highway-draw.js) is the single
// source of the label font; highway.setLabelScale / setContainer (static/highway.js).
'use strict';

const { test, before } = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const { pathToFileURL } = require('node:url');

const store = new Map();
let draw;
let hwMod;

before(async () => {
    global.window = global;
    global.addEventListener = () => {};
    global.removeEventListener = () => {};
    global.document = { getElementById: () => null, documentElement: { clientWidth: 1000, clientHeight: 700 } };
    global.localStorage = {
        getItem: (k) => (store.has(k) ? store.get(k) : null),
        setItem: (k, v) => store.set(k, String(v)),
        removeItem: (k) => store.delete(k),
    };
    const root = path.join(__dirname, '..', '..', 'static');
    draw = await import(pathToFileURL(path.join(root, 'js', 'highway-draw.js')).href);
    hwMod = await import(pathToFileURL(path.join(root, 'highway.js')).href).catch((e) => ({ error: e }));
});

test('labelFont default scale', () => {
    assert.equal(draw.labelFont({}, 20), 'bold 20px sans-serif');
});

test('labelFont scales and rounds', () => {
    assert.equal(draw.labelFont({ _labelScale: 1.5 }, 15), 'bold 23px sans-serif');
});

function makeHighway() {
    if (hwMod.error || typeof window.createHighway !== 'function') return null;
    return window.createHighway();
}

function fakeCanvas() {
    return { style: {}, width: 0, height: 0, getContext: () => null };
}

test('setLabelScale clamps, persists, ignores junk', (t) => {
    const hw = makeHighway();
    if (!hw) return t.skip('highway.js not importable under node: ' + (hwMod.error && hwMod.error.message));
    assert.equal(hw.getLabelScale(), 1);
    hw.setLabelScale(9);
    assert.equal(hw.getLabelScale(), 2.5);
    hw.setLabelScale(0.1);
    assert.equal(hw.getLabelScale(), 0.75);
    hw.setLabelScale(1.5);
    assert.equal(store.get('highwayLabelScale'), '1.5');
    hw.setLabelScale(NaN);
    hw.setLabelScale(undefined);
    assert.equal(hw.getLabelScale(), 1.5);
});

test('setContainer sizes from the element and observes it', (t) => {
    const hw = makeHighway();
    if (!hw) return t.skip('highway.js not importable under node: ' + (hwMod.error && hwMod.error.message));
    const observers = [];
    global.ResizeObserver = class {
        constructor(cb) { this.cb = cb; this.observed = []; this.disconnected = false; observers.push(this); }
        observe(el) { this.observed.push(el); }
        disconnect() { this.disconnected = true; }
    };
    const canvas = fakeCanvas();
    hw.init(canvas);
    assert.equal(canvas.style.width, '1000px');
    const el = { getBoundingClientRect: () => ({ width: 400, height: 300 }) };
    hw.setContainer(el);
    assert.equal(canvas.style.width, '400px');
    assert.equal(canvas.style.height, '300px');
    assert.equal(observers.length, 1);
    assert.deepEqual(observers[0].observed, [el]);
    el.getBoundingClientRect = () => ({ width: 250, height: 200 });
    observers[0].cb();
    assert.equal(canvas.style.width, '250px');
    hw.setContainer(null);
    assert.equal(observers[0].disconnected, true);
    assert.equal(canvas.style.width, '1000px');
    delete global.ResizeObserver;
});

test('setContainer is safe without ResizeObserver', (t) => {
    const hw = makeHighway();
    if (!hw) return t.skip('highway.js not importable under node: ' + (hwMod.error && hwMod.error.message));
    delete global.ResizeObserver;
    const canvas = fakeCanvas();
    hw.init(canvas);
    const el = { getBoundingClientRect: () => ({ width: 320, height: 240 }) };
    assert.doesNotThrow(() => hw.setContainer(el));
    assert.equal(canvas.style.width, '320px');
    assert.doesNotThrow(() => hw.setContainer(null));
});

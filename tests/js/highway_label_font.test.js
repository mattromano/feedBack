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

function stubResizeObserver() {
    const observers = [];
    global.ResizeObserver = class {
        constructor(cb) { this.cb = cb; this.observed = []; this.disconnected = false; observers.push(this); }
        observe(el) { this.observed.push(el); }
        disconnect() { this.disconnected = true; }
    };
    return observers;
}

test('stop() disconnects the container observer', (t) => {
    const hw = makeHighway();
    if (!hw) return t.skip('highway.js not importable under node: ' + (hwMod.error && hwMod.error.message));
    const observers = stubResizeObserver();
    try {
        hw.init(fakeCanvas());
        hw.setContainer({ getBoundingClientRect: () => ({ width: 400, height: 300 }) });
        assert.equal(observers.length, 1);
        hw.stop();
        assert.equal(observers[0].disconnected, true);
        // A later setContainer(null) must not try to disconnect it again via a stale ref.
        observers[0].disconnected = false;
        hw.setContainer(null);
        assert.equal(observers[0].disconnected, false, 'observer reference was dropped');
    } finally { delete global.ResizeObserver; }
});

test('init() disconnects an existing container observer', (t) => {
    const hw = makeHighway();
    if (!hw) return t.skip('highway.js not importable under node: ' + (hwMod.error && hwMod.error.message));
    const observers = stubResizeObserver();
    try {
        hw.init(fakeCanvas());
        hw.setContainer({ getBoundingClientRect: () => ({ width: 400, height: 300 }) });
        hw.init(fakeCanvas());
        assert.equal(observers[0].disconnected, true);
        observers[0].disconnected = false;
        hw.setContainer(null);
        assert.equal(observers[0].disconnected, false, 'observer reference was dropped');
    } finally { delete global.ResizeObserver; }
});

test('setLabelScale { persist: false } applies without writing storage', (t) => {
    const hw = makeHighway();
    if (!hw) return t.skip('highway.js not importable under node: ' + (hwMod.error && hwMod.error.message));
    store.set('highwayLabelScale', '1.25');
    hw.setLabelScale(2, { persist: false });
    assert.equal(hw.getLabelScale(), 2);
    assert.equal(store.get('highwayLabelScale'), '1.25', 'storage untouched');
    hw.setLabelScale(9, { persist: false });
    assert.equal(hw.getLabelScale(), 2.5, 'still clamped');
    hw.setLabelScale(1.75);
    assert.equal(store.get('highwayLabelScale'), '1.75', 'default still persists');
});

// Custom renderers (3D highway) draw their own labels, so the scale has to
// reach them through the per-instance draw bundle — not a global — so each
// splitscreen / band lane honours its own highway's setting.
function swappableCanvas() {
    const c = fakeCanvas();
    c.cloneNode = () => swappableCanvas();
    c.replaceWith = () => {};
    return c;
}

function captureBundle(hw) {
    let seen = null;
    hw.setRenderer({ contextType: '2d', init(_c, b) { seen = { labelScale: b.labelScale }; }, draw() {} });
    return seen;
}

test('draw bundle carries the highway instance label scale', (t) => {
    const hw = makeHighway();
    if (!hw) return t.skip('highway.js not importable under node: ' + (hwMod.error && hwMod.error.message));
    hw.init(swappableCanvas());
    hw.setLabelScale(1, { persist: false });
    assert.equal(captureBundle(hw).labelScale, 1);
    hw.setLabelScale(2.25, { persist: false });
    assert.equal(captureBundle(hw).labelScale, 2.25);
});

test('two highways expose independent bundle label scales', (t) => {
    const a = makeHighway();
    const b = makeHighway();
    if (!a || !b) return t.skip('highway.js not importable under node');
    a.init(swappableCanvas());
    b.init(swappableCanvas());
    a.setLabelScale(2.5, { persist: false });
    b.setLabelScale(0.75, { persist: false });
    assert.equal(captureBundle(a).labelScale, 2.5);
    assert.equal(captureBundle(b).labelScale, 0.75);
});

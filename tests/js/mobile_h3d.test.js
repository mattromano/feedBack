// Phone app (static/mobile/h3d.js): loading the bundled 3D Highway plugin as
// the player's renderer, its WebGL2 gate, phone defaults and the 3D/2D toggle.
'use strict';

const { test, before, beforeEach } = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const { pathToFileURL } = require('node:url');

let h3d;
before(async () => {
    h3d = await import(pathToFileURL(path.join(__dirname, '..', '..', 'static', 'mobile', 'h3d.js')).href);
});

function fakeStorage(init = {}) {
    const m = new Map(Object.entries(init));
    return {
        getItem: (k) => (m.has(k) ? m.get(k) : null),
        setItem: (k, v) => { m.set(k, String(v)); },
        map: m,
    };
}

function fakeDoc({ webgl2 = true } = {}) {
    const appended = [];
    const lost = { n: 0 };
    const mk = (tag) => ({
        tagName: tag, dataset: {}, removed: false,
        remove() { this.removed = true; },
        getContext(type) {
            if (type !== 'webgl2' || !webgl2) return null;
            return { getExtension: () => ({ loseContext() { lost.n++; } }) };
        },
    });
    const doc = {
        appended, lost,
        createElement: mk,
        querySelector: (sel) => appended.find((n) => n.tagName === 'link' && sel.includes(n.dataset.pluginId)) || null,
        head: { appendChild: (n) => appended.push(n) },
        body: { appendChild: (n) => appended.push(n) },
    };
    return doc;
}

beforeEach(() => {
    h3d._resetForTests();
    globalThis.localStorage = fakeStorage();
});

test('h3d: highway mode defaults to 3D and round-trips the 2D choice', () => {
    assert.equal(h3d.getHighwayMode(), '3d');
    h3d.setHighwayMode('2d');
    assert.equal(globalThis.localStorage.getItem(h3d.HW_MODE_KEY), '2d');
    assert.equal(h3d.getHighwayMode(), '2d');
    h3d.setHighwayMode('bogus');
    assert.equal(h3d.getHighwayMode(), '3d');
});

test('h3d: mode falls back to 3D when storage throws', () => {
    globalThis.localStorage = { getItem() { throw new Error('blocked'); }, setItem() { throw new Error('blocked'); } };
    assert.equal(h3d.getHighwayMode(), '3d');
    assert.doesNotThrow(() => h3d.setHighwayMode('2d'));
});

test('h3d: phone defaults are seeded only where the user has no value', () => {
    const ls = fakeStorage({ h3d_bg_bloom: 'true' });
    h3d.seedPhoneDefaults(ls);
    assert.equal(ls.getItem('h3d_bg_bloom'), 'true', 'explicit choice kept');
    const fresh = fakeStorage();
    h3d.seedPhoneDefaults(fresh);
    assert.equal(fresh.getItem('h3d_bg_bloom'), 'false');
});

test('h3d: WebGL2 probe is memoized and releases its context', () => {
    const doc = fakeDoc({ webgl2: true });
    assert.equal(h3d.canRun3D(doc), true);
    assert.equal(doc.lost.n, 1);
    assert.equal(h3d.canRun3D(fakeDoc({ webgl2: false })), true, 'memoized');
    h3d._resetForTests();
    assert.equal(h3d.canRun3D(fakeDoc({ webgl2: false })), false);
});

test('h3d: loader injects the classic screen.js + plugin.css (query-less URLs) and resolves the factory', async () => {
    const doc = fakeDoc();
    const win = {};
    const p = h3d.loadH3DFactory(win, doc);
    assert.equal(h3d.loadH3DFactory(win, doc), p, 'one load in flight');
    const link = doc.appended.find((n) => n.tagName === 'link');
    const script = doc.appended.find((n) => n.tagName === 'script');
    assert.equal(link.href, '/api/plugins/highway_3d/assets/plugin.css');
    assert.equal(link.rel, 'stylesheet');
    assert.equal(script.src, '/api/plugins/highway_3d/screen.js');
    assert.equal(script.type, undefined, 'classic script, not a module');
    const factory = () => ({ draw() {} });
    win.feedBackViz_highway_3d = factory;
    script.onload();
    assert.equal(await p, factory);
    assert.equal(await h3d.loadH3DFactory(win, doc), factory, 'already registered: no reload');
    assert.equal(globalThis.localStorage.getItem('h3d_bg_bloom'), 'false', 'defaults seeded before load');
});

test('h3d: a failed load rejects and is retried on the next call', async () => {
    const doc = fakeDoc();
    const win = {};
    const p = h3d.loadH3DFactory(win, doc);
    const script = doc.appended.find((n) => n.tagName === 'script');
    script.onerror();
    await assert.rejects(p, /failed to load/);
    assert.ok(script.removed);
    const p2 = h3d.loadH3DFactory(win, doc);
    assert.notEqual(p2, p);
    const s2 = doc.appended.filter((n) => n.tagName === 'script')[1];
    s2.onload(); // loaded but never registered the factory
    await assert.rejects(p2, /did not register/);
    assert.equal(doc.appended.filter((n) => n.tagName === 'link').length, 1, 'stylesheet injected once');
});

test('h3d: renderer is tagged like viz.js and opts out of the audio-reactive <audio> tap', () => {
    let seen = null;
    const inner = {
        draw() {},
        init(canvas, bundle) { seen = { self: this, canvas, bgReactive: bundle && bundle.bgReactive }; this.readyPromise = 'rp'; },
    };
    const r = h3d.createH3DRenderer(() => inner);
    assert.equal(r, inner);
    assert.equal(r.pluginId, 'highway_3d');
    assert.equal(r.source, 'highway_3d');
    const bundle = { notes: [] };
    r.init('C', bundle);
    assert.equal(seen.self, r, 'init keeps `this` so readyPromise lands on the installed renderer');
    assert.equal(seen.canvas, 'C');
    assert.equal(seen.bgReactive, false);
    assert.equal(r.readyPromise, 'rp');
    assert.doesNotThrow(() => r.init('C', undefined));
});

test('h3d: an invalid renderer is rejected', () => {
    assert.throws(() => h3d.createH3DRenderer(() => null), /invalid renderer/);
    assert.throws(() => h3d.createH3DRenderer(() => ({ init() {} })), /invalid renderer/);
});

test('h3d: zoom defaults to close and feeds the plugin framing bridge', () => {
    assert.equal(h3d.getZoom(), 'close');
    globalThis.localStorage.setItem('h3d_aspect_tune2', JSON.stringify({ enabled: false, heightMul: 0.3 }));
    const win = { __h3dAspectTune: { baseVfov: 70, enabled: false } };
    h3d.applyZoom(win);
    assert.equal(win.__h3dAspectTune.baseVfov, h3d.ZOOM_VFOV.close);
    const saved = JSON.parse(globalThis.localStorage.getItem('h3d_aspect_tune2'));
    assert.equal(saved.baseVfov, h3d.ZOOM_VFOV.close);
    assert.equal(saved.heightMul, 0.3, 'other framing fields kept');
    assert.equal(saved.enabled, true, 'the bridge only applies baseVfov while enabled');
    assert.ok(saved.startAspect > 100, 'wide-pane Hor+ and pose nudges stay off');
    h3d.setZoom('normal');
    const normal = JSON.parse(globalThis.localStorage.getItem('h3d_aspect_tune2'));
    assert.equal(normal.baseVfov, 70);
    assert.equal(normal.enabled, false);
    h3d.setZoom('bogus');
    assert.equal(h3d.getZoom(), 'close');
});

test('h3d: corrupt saved framing is replaced, not thrown on', () => {
    globalThis.localStorage.setItem('h3d_aspect_tune2', '[not json');
    assert.doesNotThrow(() => h3d.applyZoom({}));
    assert.deepEqual(JSON.parse(globalThis.localStorage.getItem('h3d_aspect_tune2')), { enabled: true, splitOnly: false, baseVfov: h3d.ZOOM_VFOV.close, startAspect: 1000 });
});

test('h3d: resolution defaults to sharp (never auto-downscaled)', () => {
    assert.equal(h3d.getQuality(), 'sharp');
    assert.equal(h3d.minRenderScale(), 1);
    h3d.setQuality('auto');
    assert.equal(h3d.minRenderScale(), 0.5);
    h3d.setQuality('x');
    assert.equal(h3d.getQuality(), 'sharp');
});

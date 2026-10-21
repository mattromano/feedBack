'use strict';
// feedBack.library.pick: the library pick session (static/v3/library-pick.js).
const { test } = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const { createLibraryPick, mergeParams, isPickBackKey } = require('../../static/v3/library-pick.js');

function setup() {
    const emits = [];
    const shows = [];
    let api = null;
    const onShow = [];
    api = createLibraryPick({
        emit: (type, detail) => emits.push({ type, detail }),
        showScreen: (id) => {
            shows.push(id);
            onShow.forEach((fn) => fn(id));
        },
    });
    return { api, emits, shows, onShow };
}

const CHIP = { id: 'p', label: 'P', params: { has_drums: '1' }, on: true };

function settled(promise) {
    const sentinel = { pending: true };
    return Promise.race([promise, Promise.resolve(sentinel)]).then((v) => v !== sentinel);
}

function fakeDoc({ byId = {}, bySelector = {} } = {}) {
    return {
        getElementById: (id) => (id in byId ? byId[id] : null),
        querySelector: (sel) => (sel in bySelector ? bySelector[sel] : null),
    };
}

function hiddenOverlay(hidden) {
    return { classList: { contains: (c) => (c === 'hidden' ? hidden : false) } };
}

function key(over = {}) {
    return Object.assign({
        key: 'Escape', ctrlKey: false, metaKey: false, altKey: false, shiftKey: false,
        defaultPrevented: false, target: { tagName: 'DIV', isContentEditable: false },
    }, over);
}

test('mergeParams joins on chips and unions a repeated key', () => {
    const out = mergeParams([
        { id: 'a', on: true, params: { x: 'Lead,Rhythm' } },
        { id: 'b', on: false, params: { y: '1' } },
        { id: 'c', on: true, params: { x: 'Rhythm,Bass', z: 'pitch' } },
    ]);
    assert.deepStrictEqual(out, { x: 'Lead,Rhythm,Bass', z: 'pitch' });
});

test('pick opens the songs screen and exposes a session', () => {
    const { api, emits, shows } = setup();
    api.pick({ title: 'T', filters: [CHIP], returnTo: 'plugin-x' });
    assert.deepStrictEqual(shows, ['v3-songs']);
    assert.deepStrictEqual(emits[0], { type: 'library:pick-changed', detail: { active: true } });
    assert.strictEqual(api.session().title, 'T');
    assert.strictEqual(api.session().confirmLabel, 'Use this song');
    assert.deepStrictEqual(api.params(), { has_drums: '1' });
});

test('select then confirm resolves picked, ends first, then returns', async () => {
    const { api, onShow } = setup();
    const p = api.pick({ title: 'T', filters: [CHIP], returnTo: 'plugin-x' });
    let sessionAtReturn;
    onShow.push((id) => { if (id === 'plugin-x') sessionAtReturn = api.session(); });
    assert.strictEqual(api.select({ filename: 'a.sloppak', title: 'A' }), true);
    assert.strictEqual(api.confirm(), true);
    assert.deepStrictEqual(await p, {
        item: { filename: 'a.sloppak', title: 'A' }, filters: { p: true }, reason: 'picked',
    });
    assert.strictEqual(sessionAtReturn, null);
});

test('confirm without a selection does nothing', async () => {
    const { api } = setup();
    const p = api.pick({ filters: [CHIP] });
    assert.strictEqual(api.confirm(), false);
    assert.strictEqual(await settled(p), false);
});

test('select needs a filename', () => {
    const { api } = setup();
    api.pick({});
    assert.strictEqual(api.select({}), false);
    assert.strictEqual(api.select({ filename: '' }), false);
});

test('back resolves back with null and returns', async () => {
    const { api, shows } = setup();
    const p = api.pick({ filters: [CHIP], returnTo: 'plugin-x' });
    assert.strictEqual(api.back(), true);
    const r = await p;
    assert.strictEqual(r.item, null);
    assert.strictEqual(r.reason, 'back');
    assert.strictEqual(shows[shows.length - 1], 'plugin-x');
});

test('toggle flips a chip and the result carries it', async () => {
    const { api } = setup();
    const p = api.pick({ filters: [CHIP] });
    assert.strictEqual(api.toggle('p'), true);
    assert.deepStrictEqual(api.params(), {});
    assert.strictEqual(api.toggle('nope'), false);
    api.back();
    assert.deepStrictEqual((await p).filters, { p: false });
});

test('leaving the songs screen resolves left without navigating', async () => {
    const { api, shows } = setup();
    const p = api.pick({ filters: [CHIP], returnTo: 'plugin-x' });
    api.onScreenChanged('v3-songs');
    assert.notStrictEqual(api.session(), null);
    api.onScreenChanged('v3-home');
    assert.strictEqual((await p).reason, 'left');
    assert.deepStrictEqual(shows, ['v3-songs']);
});

test('a stale screen:changed while Songs is still active keeps the session', async () => {
    const shows = [];
    const api = createLibraryPick({ showScreen: (id) => shows.push(id), activeScreen: () => 'v3-songs' });
    const p = api.pick({ filters: [CHIP], returnTo: 'plugin-x' });
    api.onScreenChanged('plugin-x');
    assert.notStrictEqual(api.session(), null);
    assert.strictEqual(await settled(p), false);
    assert.deepStrictEqual(shows, ['v3-songs']);
});

test('screen:changed with another screen really active resolves left', async () => {
    const shows = [];
    const api = createLibraryPick({ showScreen: (id) => shows.push(id), activeScreen: () => 'v3-home' });
    const p = api.pick({ filters: [CHIP], returnTo: 'plugin-x' });
    api.onScreenChanged('v3-home');
    assert.strictEqual((await p).reason, 'left');
    assert.strictEqual(api.session(), null);
    assert.deepStrictEqual(shows, ['v3-songs']);
});

test('the bootstrap passes the active screen to the pick session', () => {
    const src = fs.readFileSync(path.join(__dirname, '..', '..', 'static', 'v3', 'library-pick.js'), 'utf8');
    assert.match(src, /activeScreen: \(\) => \{ const a = root\.document\.querySelector\('\.screen\.active'\); return a \? a\.id : undefined; \}/);
});

test('a second pick replaces the first', async () => {
    const { api } = setup();
    const first = api.pick({ title: 'one' });
    api.pick({ title: 'two' });
    assert.strictEqual((await first).reason, 'replaced');
    assert.strictEqual(api.session().title, 'two');
});

test('a preselected song is kept', async () => {
    const { api } = setup();
    const p = api.pick({ selected: { filename: 'b' } });
    assert.strictEqual(api.session().selected.filename, 'b');
    assert.strictEqual(api.confirm(), true);
    const r = await p;
    assert.strictEqual(r.reason, 'picked');
    assert.deepStrictEqual(r.item, { filename: 'b' });
});

test('isPickBackKey: only a plain Escape on v3-songs', () => {
    const doc = fakeDoc({ byId: { 'v3-songs-overlay': hiddenOverlay(true) } });
    assert.strictEqual(isPickBackKey(key(), doc, 'v3-songs'), true);
    assert.strictEqual(isPickBackKey(key(), fakeDoc(), 'v3-songs'), true);
    assert.strictEqual(isPickBackKey(key({ shiftKey: true }), doc, 'v3-songs'), false);
    assert.strictEqual(isPickBackKey(key({ defaultPrevented: true }), doc, 'v3-songs'), false);
    assert.strictEqual(isPickBackKey(key({ target: { tagName: 'INPUT' } }), doc, 'v3-songs'), false);
    const openOverlay = fakeDoc({ byId: { 'v3-songs-overlay': hiddenOverlay(false) } });
    assert.strictEqual(isPickBackKey(key(), openOverlay, 'v3-songs'), false);
    const modal = fakeDoc({ bySelector: { '[role="dialog"][aria-modal="true"], .feedBack-modal': {} } });
    assert.strictEqual(isPickBackKey(key(), modal, 'v3-songs'), false);
    assert.strictEqual(isPickBackKey(key(), doc, 'v3-home'), false);
});

test('handleKey goes back and prevents default', async () => {
    const { api } = setup();
    const doc = fakeDoc({ bySelector: { '.screen.active': { id: 'v3-songs' } } });
    let prevented = false;
    const e = key({ preventDefault: () => { prevented = true; } });
    assert.strictEqual(api.handleKey(e, doc), false);
    assert.strictEqual(prevented, false);
    const p = api.pick({});
    assert.strictEqual(api.handleKey(e, doc), true);
    assert.strictEqual(prevented, true);
    assert.strictEqual((await p).reason, 'back');
});

test('index.html loads library-pick.js after app.js and before songs.js', () => {
    const html = fs.readFileSync(path.join(__dirname, '../../static/v3/index.html'), 'utf8');
    const pick = html.indexOf('/static/v3/library-pick.js');
    const app = html.indexOf('src="/static/app.js"');
    assert.ok(pick >= 0, 'library-pick.js is loaded');
    assert.ok(app >= 0, 'app.js is loaded');
    assert.ok(app < pick, 'library-pick.js must load after app.js (window.feedBack + showScreen)');
    assert.ok(pick < html.indexOf('/static/v3/songs.js'));
});

test('a rejected showScreen promise is caught (no unhandled rejection)', async () => {
    const unhandled = [];
    const onUnhandled = (reason) => unhandled.push(reason);
    process.on('unhandledRejection', onUnhandled);
    try {
        const api = createLibraryPick({
            emit: () => {},
            showScreen: () => Promise.reject(new Error('no such screen')),
        });
        const p = api.pick({ returnTo: 'plugin-missing' });
        api.back();
        assert.strictEqual((await p).reason, 'back');
        await new Promise((r) => setImmediate(r));
        await new Promise((r) => setImmediate(r));
        assert.deepStrictEqual(unhandled, []);
    } finally {
        process.off('unhandledRejection', onUnhandled);
    }
});

'use strict';
// feedBack.ui.registerPlayMode: the play-mode registry (static/v3/play-modes.js).
const { test } = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const { createPlayModes, normalizeMode, splitHeroHtml, cardButtonsHtml } = require('../../static/v3/play-modes.js');

const esc = (s) => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');

function setup() {
    const emits = [];
    const api = createPlayModes({ emit: (type, detail) => emits.push({ type, detail }) });
    return { api, emits };
}

test('normalizeMode needs id and label', () => {
    assert.strictEqual(normalizeMode({}), null);
    assert.strictEqual(normalizeMode({ id: 'a' }), null);
    assert.strictEqual(normalizeMode({ label: 'A' }), null);
    const m = normalizeMode({ id: 'a', label: 'A' });
    assert.strictEqual(m.startLabel, 'Play A');
    assert.strictEqual(m.order, 100);
    assert.strictEqual(m.startPress, false);
    assert.strictEqual(m.tour, null);
    assert.strictEqual(m.cardAction, null);
});

test('register emits and unregister emits once', () => {
    const { api, emits } = setup();
    const off = api.register({ id: 'a', label: 'A' });
    assert.deepStrictEqual(emits, [{ type: 'ui:play-modes-changed', detail: { ids: ['a'] } }]);
    off();
    assert.strictEqual(emits.length, 2);
    assert.deepStrictEqual(emits[1].detail, { ids: [] });
    off();
    assert.strictEqual(emits.length, 2);
});

test('duplicate id is ignored', () => {
    const { api } = setup();
    const warn = console.warn;
    const warned = [];
    console.warn = (...a) => warned.push(a);
    let off2;
    try {
        api.register({ id: 'a', label: 'First' });
        off2 = api.register({ id: 'a', label: 'Second' });
    } finally { console.warn = warn; }
    assert.deepStrictEqual(warned, [['[play-modes] duplicate play mode ignored:', 'a']]);
    assert.deepStrictEqual(api.list().map((m) => m.label), ['First']);
    off2();
    assert.deepStrictEqual(api.list().map((m) => m.label), ['First']);
});

test('invalid spec gives a no-op unregister', () => {
    const { api, emits } = setup();
    const off = api.register({ id: 'a' });
    off();
    assert.strictEqual(emits.length, 0);
});

test('list sorts by order then id', () => {
    const { api } = setup();
    api.register({ id: 'c', label: 'C', order: 5 });
    api.register({ id: 'b', label: 'B' });
    api.register({ id: 'a', label: 'A' });
    assert.deepStrictEqual(api.list().map((m) => m.id), ['c', 'a', 'b']);
});

test('heroMode needs renderHero and start', () => {
    const { api } = setup();
    api.register({ id: 'a', label: 'A', start() {} });
    assert.strictEqual(api.heroMode(), null);
    api.register({ id: 'b', label: 'B', start() {}, renderHero() {} });
    assert.strictEqual(api.heroMode().id, 'b');
});

test('cardActions filters and survives a throw', () => {
    const { api } = setup();
    api.register({ id: 'a', label: 'A', cardAction: { isAvailable: (s) => s.ok, run() {} } });
    api.register({ id: 'b', label: 'B', cardAction: { isAvailable() { throw new Error('x'); }, run() {} } });
    assert.deepStrictEqual(api.cardActions({ ok: true }), [{ id: 'a', label: 'A', title: 'Play with A' }]);
    assert.deepStrictEqual(api.cardActions({ ok: false }), []);
});

test('runCardAction passes the song', () => {
    const { api } = setup();
    const got = [];
    api.register({ id: 'a', label: 'A', cardAction: { run: (s) => got.push(s) } });
    api.register({ id: 'b', label: 'B', cardAction: { run() { throw new Error('boom'); } } });
    api.register({ id: 'c', label: 'C' });
    const song = { ok: true };
    assert.strictEqual(api.runCardAction('a', song), true);
    assert.strictEqual(got[0], song);
    assert.strictEqual(api.runCardAction('zzz', song), false);
    assert.strictEqual(api.runCardAction('c', song), false);
    const warn = console.warn;
    console.warn = () => {};
    try { assert.strictEqual(api.runCardAction('b', song), true); } finally { console.warn = warn; }
});

test('start runs a mode and startPressWanted reflects startPress', () => {
    const { api } = setup();
    let n = 0;
    api.register({ id: 'a', label: 'A', start: () => { n++; } });
    assert.strictEqual(api.start('a'), true);
    assert.strictEqual(n, 1);
    assert.strictEqual(api.start('nope'), false);
    assert.strictEqual(api.startPressWanted(), false);
    api.register({ id: 'b', label: 'B', startPress: true });
    assert.strictEqual(api.startPressWanted(), true);
});

test('splitHeroHtml without a mode returns the solo html unchanged', () => {
    const solo = '<div id="solo"></div>';
    assert.strictEqual(splitHeroHtml(solo, null, esc), solo);
});

test('splitHeroHtml escapes', () => {
    const mode = normalizeMode({ id: 'a', label: '<b>' });
    const html = splitHeroHtml('<i>solo</i>', mode, esc);
    assert.ok(html.includes('&lt;b&gt;'));
    assert.ok(!html.includes('<b>'));
    assert.ok(html.includes('id="v3-hero-mode"'));
    assert.ok(html.includes('data-play-mode-start="a"'));
    assert.ok(html.startsWith('<div id="v3-hero-split" class="lg:col-span-2 v3-hero-split"><i>solo</i>'));
});

test('cardButtonsHtml of [] is empty and escapes', () => {
    assert.strictEqual(cardButtonsHtml([], esc), '');
    const html = cardButtonsHtml([{ id: 'a', label: '<L>', title: 'T"' }], esc);
    assert.ok(html.includes('data-play-mode-card="a"'));
    assert.ok(html.includes('&lt;L&gt;'));
    assert.ok(html.includes('title="T&quot;"'));
});

test('index.html loads play-modes.js before dashboard.js and songs.js', () => {
    const html = fs.readFileSync(path.join(__dirname, '../../static/v3/index.html'), 'utf8');
    const at = (n) => html.indexOf('/static/v3/' + n);
    assert.ok(at('play-modes.js') > 0);
    assert.ok(at('play-modes.js') < at('dashboard.js'));
    assert.ok(at('play-modes.js') < at('songs.js'));
});

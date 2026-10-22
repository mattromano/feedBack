// setViz on a plugin view must move the viz picker to it. app.js re-reads the
// picker on every song:ready ('auto' -> _autoMatchViz), so a picker left on
// Auto swapped a just-chosen 5-Fret view back to the guitar highway once the
// song loaded (the library's "Play as" 5-Fret Guitar showed the 3D highway).

'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const { pathToFileURL } = require('node:url');

function picker(values, value) {
    const options = values.map((v) => ({ value: v, text: v }));
    return { options, value };
}

test('setViz(<plugin id>) moves the picker to that view', async () => {
    const sel = picker(['auto', 'default', 'highway_3d', 'five_fret'], 'auto');
    const store = new Map();
    const installed = [];
    global.localStorage = { getItem: (k) => (store.has(k) ? store.get(k) : null), setItem: (k, v) => store.set(k, String(v)), removeItem: (k) => store.delete(k) };
    global.document = { getElementById: (id) => (id === 'viz-picker' ? sel : null), querySelector: () => null };
    global.window = {
        highway: { setRenderer: (r) => installed.push(r), getSongInfo: () => ({}) },
        feedBackViz_five_fret: () => ({ draw() {} }),
        feedBack: {},
    };
    const viz = await import(pathToFileURL(path.join(__dirname, '..', '..', 'static', 'js', 'viz.js')).href);
    viz.setViz('five_fret');
    assert.equal(installed.length, 1);
    assert.equal(store.get('vizSelection'), 'five_fret');
    assert.equal(sel.value, 'five_fret');
});

// mattromano build: the visualization pickers list only the main highways
// (Auto, 3D Highway, the Drums view, Keys Highway 3D). Hidden renderers stay
// registered and selectable programmatically (a saved vizSelection, Auto,
// the instrument override), and Auto falls back to the 3D Highway instead of
// the built-in 2D one when no specialised view claims a song.
'use strict';

const { test, before, beforeEach } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { pathToFileURL } = require('node:url');

const VIZ_JS = path.join(__dirname, '..', '..', 'static', 'js', 'viz.js');
let viz;
const installed = [];
let songInfo = {};
let sel;
let webgl2 = true;

function factory(id, matches, extra) {
    const f = function () { return { id, draw() {} }; };
    f.matchesArrangement = matches;
    Object.assign(f, extra || {});
    return f;
}

before(async () => {
    sel = { value: 'auto', options: [] };
    global.localStorage = { getItem: () => null, setItem() {}, removeItem() {} };
    global.document = {
        getElementById: (id) => (id === 'viz-picker' ? sel : null),
        querySelector: () => null,
        createElement: () => ({ getContext: (t) => (t === 'webgl2' && webgl2 ? {} : null) }),
    };
    global.window = global;
    window.feedBack = { on() {}, off() {}, emit() {} };
    window.highway = {
        getSongInfo: () => songInfo,
        setRenderer: (r) => installed.push(r ? r.id : null),
    };
    viz = await import(pathToFileURL(VIZ_JS).href);
});

beforeEach(() => {
    installed.length = 0;
    sel.value = 'auto';
    sel.options = ['auto', 'default', 'highway_3d', 'venue', 'drum_highway_3d', 'drums', 'jumpingtab',
        'keys_highway_3d', 'piano', 'staffview', 'tabview', 'some_new_viz']
        .map((value) => ({ value, text: value, hidden: false }));
    window.feedBackViz_highway_3d = factory('highway_3d', (si) => /\b(?:lead|rhythm|bass|combo|guitar)\b/i.test(si.arrangement || ''),
        { contextType: 'webgl2' });
    window.feedBackViz_drums = factory('drums', (si) => /drums/i.test(si.arrangement || ''));
    window.feedBackViz_drum_highway_3d = factory('drum_highway_3d', () => false, { contextType: 'webgl2' });
    window.feedBackViz_keys_highway_3d = factory('keys_highway_3d', (si) => !!si.has_notation, { contextType: 'webgl2' });
    window.feedBackViz_piano = factory('piano', () => false);
});

test('only the main highways stay visible in the picker', () => {
    viz._applyPickerHideList(sel);
    const visible = sel.options.filter((o) => !o.hidden).map((o) => o.value);
    assert.deepEqual(visible, ['auto', 'highway_3d', 'drums', 'keys_highway_3d', 'some_new_viz']);
    // Hidden entries are still options, so a saved selection or Auto can use them.
    assert.equal(sel.options.length, 12);
});

test('the hide list is shared for other pickers (split view)', () => {
    assert.equal(typeof window._vizHiddenInPicker, 'function');
    for (const id of ['default', 'venue', 'drum_highway_3d', 'piano', 'tabview', 'jumpingtab', 'staffview', 'slopscale']) {
        assert.equal(window._vizHiddenInPicker(id), true, id);
    }
    for (const id of ['auto', 'highway_3d', 'drums', 'keys_highway_3d']) {
        assert.equal(window._vizHiddenInPicker(id), false, id);
    }
});

test('_populateVizPicker applies the hide list', () => {
    const src = fs.readFileSync(VIZ_JS, 'utf8');
    const body = src.slice(src.indexOf('export async function _populateVizPicker'));
    assert.match(body.slice(0, body.indexOf('\n}\n')), /_applyPickerHideList\(sel\)/);
});

test('Auto: a guitar part still goes to the 3D Highway', () => {
    songInfo = { arrangement: 'Lead', arrangement_index: 0 };
    viz._autoMatchViz();
    assert.deepEqual(installed, ['highway_3d']);
});

test('Auto: a part nothing claims falls back to the 3D Highway, not the 2D one', () => {
    songInfo = { arrangement: 'Vocals', arrangement_index: 4, arrangements: [{ index: 4, notes: 12 }] };
    viz._autoMatchViz();
    assert.deepEqual(installed, ['highway_3d']);
});

test('Auto: a notation-only part keeps the old fallback (no empty 3D board)', () => {
    songInfo = { arrangement: 'Score', arrangement_index: 1, has_notation: false,
        arrangements: [{ index: 1, notes: 0 }] };
    songInfo.has_notation = true;
    window.feedBackViz_keys_highway_3d = factory('keys_highway_3d', () => false, { contextType: 'webgl2' });
    viz._autoMatchViz();
    assert.deepEqual(installed, [null]);
});

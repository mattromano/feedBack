// mattromano build: on an explicit guitar view (Classic 2D / 3D Highway / Venue),
// a Drums or keys part is shown by its own instrument view for that song only
// (static/js/viz.js _instrumentVizOverride); guitar parts never are.
'use strict';

const { test, before, beforeEach } = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const { pathToFileURL } = require('node:url');

let viz;
const installed = [];
let songInfo = {};
let sel;

function factory(id, matches, extra) {
    const f = function () { return { id, draw() {} }; };
    f.matchesArrangement = matches;
    Object.assign(f, extra || {});
    return f;
}

before(async () => {
    sel = { value: 'highway_3d', options: [] };
    global.localStorage = { getItem: () => null, setItem() {}, removeItem() {} };
    global.document = {
        getElementById: (id) => (id === 'viz-picker' ? sel : null),
        querySelector: () => null,
        createElement: () => ({ getContext: () => null }),   // no WebGL2 here
    };
    global.window = global;
    window.feedBack = { on() {}, off() {}, emit() {} };
    window.highway = {
        getSongInfo: () => songInfo,
        setRenderer: (r) => installed.push(r ? r.id : null),
    };
    viz = await import(pathToFileURL(path.join(__dirname, '..', '..', 'static', 'js', 'viz.js')).href);
});

beforeEach(() => {
    installed.length = 0;
    sel.options = ['auto', 'default', 'drum_highway_3d', 'drums', 'highway_3d', 'keys_highway_3d', 'piano']
        .map((value) => ({ value, text: value }));
    window.feedBackViz_drums = factory('drums', (si) => /drums/i.test(si.arrangement || ''));
    window.feedBackViz_drum_highway_3d = factory('drum_highway_3d', () => false, { contextType: 'webgl2' });
    window.feedBackViz_keys_highway_3d = factory('keys_highway_3d', (si) => !!si.has_notation, { contextType: 'webgl2' });
    window.feedBackViz_piano = factory('piano', (si) => /keys|piano/i.test(si.arrangement || ''));
    window.feedBackViz_highway_3d = factory('highway_3d', () => true);
});

test('the override is exposed for the drums plugin to stand down', () => {
    assert.equal(typeof window._instrumentVizOverride, 'function');
});

test('a Drums part on the 3D Highway is taken over by the drum view', () => {
    songInfo = { arrangement: 'Drums', arrangement_index: 2 };
    assert.equal(viz._instrumentVizOverride('highway_3d'), 'drums');
    assert.deepEqual(installed, ['drums']);
    // same song again (e.g. a second song:ready): no reinstall
    assert.equal(viz._instrumentVizOverride('highway_3d'), 'drums');
    assert.deepEqual(installed, ['drums']);
});

test('a keys part goes to a keys view (WebGL2-only views are skipped without WebGL2)', () => {
    songInfo = { arrangement: 'Keys', has_notation: true };
    assert.equal(viz._instrumentVizOverride('default'), 'piano');
});

test('the next guitar song gets the picked view back', () => {
    songInfo = { arrangement: 'Drums' };
    viz._instrumentVizOverride('default');
    assert.equal(installed[installed.length - 1], 'drums');
    songInfo = { arrangement: 'Lead', has_drum_tab: true };
    sel.value = 'default';
    assert.equal(viz._instrumentVizOverride('default'), null);
    assert.equal(installed[installed.length - 1], null);     // Classic 2D restored
});

test('guitar parts are never taken over, even with notation or a drum tab', () => {
    songInfo = { arrangement: 'Lead', has_notation: true, has_drum_tab: true };
    assert.equal(viz._instrumentVizOverride('highway_3d'), null);
    songInfo = { arrangement: 'Bass 2' };
    assert.equal(viz._instrumentVizOverride('venue'), null);
    assert.deepEqual(installed, []);
});

test('an explicit non-guitar pick is left alone', () => {
    songInfo = { arrangement: 'Drums' };
    assert.equal(viz._instrumentVizOverride('piano'), null);
    assert.deepEqual(installed, []);
});

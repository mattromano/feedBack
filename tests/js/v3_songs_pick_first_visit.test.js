'use strict';
// A library pick that is the FIRST Songs visit of a session (the band flow:
// launch, open RockHeroSmith, "Choose a song"). The pick fires
// library:pick-changed while Songs is still unbuilt, which marks the grid
// dirty. That dirty flag must not short-circuit the first render() (blank
// library), nothing may write the default prefs over the saved ones, and a
// saved albums/table/folder view must still yield to the pick's grid.
//
// Runs the REAL songs.js functions (extracted brace-balanced, as in
// v3_songs_score_badge_refresh) against the real library-pick.js session,
// with the DOM-heavy collaborators stubbed. render() is stubbed to its prefs
// step + setView's reload(), which is all this path depends on.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { createLibraryPick } = require('../../static/v3/library-pick.js');

const src = fs.readFileSync(path.join(__dirname, '..', '..', 'static', 'v3', 'songs.js'), 'utf8');

function extractFunctionSource(name) {
    let start = src.indexOf('async function ' + name + '(');
    if (start < 0) start = src.indexOf('function ' + name + '(');
    assert.ok(start >= 0, 'function ' + name + ' not found in songs.js');
    const open = src.indexOf('{', src.indexOf(')', start));
    let depth = 0;
    for (let i = open; i < src.length; i++) {
        const c = src[i];
        if (c === '{') depth++;
        else if (c === '}') { depth--; if (depth === 0) return src.slice(start, i + 1); }
    }
    throw new Error('unbalanced ' + name);
}

const REAL = ['applySavedPrefs', '_restoreViewState', 'saveLibraryPrefs', '_applyPickView',
    'pickOn', 'pickSelectedKey', 'onPickChanged', 'onV3SongsScreenEnter', '_enterSongsScreen'];

function makeStore(init) {
    const data = new Map(Object.entries(init || {}));
    return {
        writes: 0,
        getItem(k) { return data.has(k) ? data.get(k) : null; },
        setItem(k, v) { this.writes++; data.set(k, v); },
        removeItem(k) { data.delete(k); },
    };
}

function boot(saved) {
    const localStorage = makeStore({ 'v3:songs-prefs': JSON.stringify(saved) });
    const sandbox = { localStorage, console };
    vm.createContext(sandbox);
    const pickApiLine = src.match(/const pickApi = [^\r\n]*/)[0];
    vm.runInContext(`
        var PREFS_KEY = 'v3:songs-prefs';
        var SORTS = [['artist', 'Artist'], ['title', 'Title']];
        var FORMATS = [['', 'All'], ['sloppak', 'Sloppak']];
        var ARRANGEMENTS = ['Lead', 'Rhythm', 'Bass', 'Combo'];
        var state = {
            view: 'grid', sort: 'artist', format: '', grouping: true, provider: 'local',
            filters: { arr_has: [], arr_lacks: [], stem_has: [], stem_lacks: [], lyrics: '', tunings: [], mastery: [], match: [], genre: [], tuningMatch: 'exact' },
            pickKey: '', pickPrevView: null, selectMode: false, built: false, artistPage: null,
        };
        var _prefsRestored = false;
        var _libraryDirty = false;
        var active = false;
        var calls = [];
        var sm = null;
        var document = { getElementById: () => null, querySelector: () => null };
        function songsActive() { return active; }
        function localFilename(s) { return (s && s.filename) || ''; }
        function reload() { calls.push('reload:' + state.view); saveLibraryPrefs(); return Promise.resolve(); }
        async function render() { calls.push('render'); _restoreViewState(); state.built = true; await reload(); }
        function setSelectMode() {}
        function requestWindowRender() {}
        function syncChromeFromState() {}
        function renderPickBar() {}
        function refreshArtistPageGates() {}
        async function applyScoreRefresh() {}
        function _readLibraryScrollSnapshot() { return null; }
        function _clearLibraryScrollSnapshot() {}
        function _libraryStateHash() { return ''; }
        function _chromeIntact() { return false; }
        function _gridDomIntact() { return false; }
        function _treeDomIntact() { return false; }
        function _applyMainScrollTop() {}
        function updateLibraryHome() {}
        ${pickApiLine}
        ${REAL.map(extractFunctionSource).join('\n')}
        globalThis.__t = {
            state: () => state, calls: () => calls, setActive: (v) => { active = v; },
            setSm: (v) => { sm = v; }, enter: onV3SongsScreenEnter, onPickChanged,
        };
    `, sandbox);
    const t = sandbox.__t;
    const pick = createLibraryPick({
        emit: (type) => { if (type === 'library:pick-changed') t.onPickChanged(); },
        showScreen: () => {},
    });
    t.setSm({ library: { pickState: pick } });
    return { t, pick, localStorage, stored: () => JSON.parse(localStorage.getItem('v3:songs-prefs')) };
}

const SAVED = {
    sort: 'title', format: 'sloppak', grouping: true,
    filters: { arr_has: ['Bass'], arr_lacks: [], stem_has: ['drums'], stem_lacks: [], lyrics: '', tunings: [] },
};

test('first visit via a pick builds the library and keeps the saved prefs', async () => {
    const saved = Object.assign({ view: 'grid' }, SAVED);
    const { t, pick, localStorage, stored } = boot(saved);
    pick.pick({ title: 'Band', filters: [{ id: 'd', label: 'D', params: { has_drums: '1' }, on: true }] });
    assert.equal(localStorage.writes, 0, 'no prefs write before the saved prefs are restored');
    t.setActive(true);
    await t.enter();
    assert.ok(t.calls().includes('render'), 'the first visit runs the full render (toolbar + grid)');
    assert.equal(t.calls()[0], 'render', 'the dirty flag must not short-circuit into reload() first');
    const st = t.state();
    assert.equal(st.sort, 'title');
    assert.equal(st.view, 'grid');
    assert.deepEqual([...st.filters.arr_has], ['Bass']);   // vm-realm array: copy before comparing
    assert.deepEqual(stored(), saved, 'saved sort, view and filters unchanged in storage');
    pick.back();
    assert.deepEqual(stored(), saved, 'still unchanged after the pick ends');
});

test('a saved albums view yields to grid during a pick and comes back after', async () => {
    const saved = Object.assign({ view: 'albums' }, SAVED);
    const { t, pick, stored } = boot(saved);
    pick.pick({ title: 'Band', filters: [] });
    t.setActive(true);
    await t.enter();
    assert.equal(t.state().view, 'grid', 'albums never shows while picking');
    assert.equal(t.state().pickPrevView, 'albums');
    assert.ok(t.calls().includes('reload:grid'));
    assert.ok(!t.calls().some((c) => c === 'reload:albums'), 'albums never loads while picking');
    assert.equal(stored().view, 'albums', 'the forced grid is never persisted');
    pick.back();
    assert.equal(t.state().view, 'albums', 'the saved view is back after the pick');
    assert.equal(t.state().pickPrevView, null);
    assert.equal(t.calls()[t.calls().length - 1], 'reload:albums');
    assert.equal(stored().view, 'albums');
});

test('a dirty grid on a built library still takes the reload shortcut', async () => {
    const { t, pick } = boot(Object.assign({ view: 'grid' }, SAVED));
    t.setActive(true);
    await t.enter();                       // first build
    t.setActive(false);
    pick.pick({ title: 'Band', filters: [] });   // off-screen: marks dirty
    const before = t.calls().length;
    t.setActive(true);
    await t.enter();
    assert.deepEqual([...t.calls().slice(before)], ['reload:grid']);
});

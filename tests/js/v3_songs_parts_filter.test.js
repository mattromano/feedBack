'use strict';
// The library drawer's "Parts" facet (static/v3/songs.js): which instrument
// parts a song carries, beyond the guitar arrangements.
//   Drums chart  — tri-state: has_drums=1 / has_drums=0 (server: _has_drums_sql)
//   Vocal pitch  — has_lyrics=pitch (shares the Lyrics/Karaoke state)
//   Keys         — tri-state via arrangements_has / arrangements_lacks=Keys
//   Rock Band / Guitar Hero 5-fret parts — moved under the same heading
//
// Runs the REAL songs.js functions (extracted brace-balanced, as in
// v3_songs_pick_first_visit) against a small stub scope.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

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

const REAL = ['fiveFretParam', 'queryParams', 'currentFilterRules', 'activeFilterCount',
    'buildLibraryStateHash', 'applySavedPrefs', 'saveLibraryPrefs',
    'drumsTri', 'nextDrumsFlag', 'triState', 'cycleTri', 'triPill', 'section', 'renderDrawer'];

function emptyFilters() {
    return { arr_has: [], arr_lacks: [], stem_has: [], stem_lacks: [], lyrics: '', drums: '', tunings: [], ff: [], mastery: [], match: [], genre: [], tuningMatch: 'exact' };
}

function boot(savedPrefs) {
    const data = new Map();
    if (savedPrefs) data.set('v3:songs-prefs', JSON.stringify(savedPrefs));
    const localStorage = {
        getItem(k) { return data.has(k) ? data.get(k) : null; },
        setItem(k, v) { data.set(k, v); },
    };
    const drawer = { innerHTML: '', querySelectorAll: () => [], querySelector: () => null };
    const sandbox = { localStorage, URLSearchParams, console, JSON };
    vm.createContext(sandbox);
    vm.runInContext(`
        var PREFS_KEY = 'v3:songs-prefs';
        var SORTS = [['artist', 'Artist']];
        var FORMATS = [['', 'All']];
        var ARRANGEMENTS = ['Lead', 'Rhythm', 'Bass', 'Combo'];
        var STEMS = ['guitar', 'bass', 'drums', 'vocals', 'piano', 'other'];
        var FIVE_FRET = [['guitar', 'Guitar'], ['rhythm', 'Rhythm'], ['bass', 'Bass']];
        var window = {};
        var state = {
            provider: 'local', view: 'grid', sort: 'artist', format: '', q: '', artist: '', album: '', grouping: true,
            filters: ${JSON.stringify(emptyFilters())}, genres: [], tuningNames: [],
        };
        var _prefsRestored = true;
        var drawerEl = null;
        var document = { getElementById: (id) => (id === 'v3-songs-drawer' ? drawerEl : null) };
        function pickApi() { return null; }
        function libInstrument() { return 'guitar-lead'; }
        function libInstrumentLabel() { return 'Guitar'; }
        function playableAvailable() { return false; }
        function applyPlayableParams() {}
        const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
        ${REAL.map(extractFunctionSource).join('\n')}
    `, sandbox);
    sandbox.drawerEl = drawer;
    return { sandbox, localStorage, drawer, run: (code) => vm.runInContext(code, sandbox) };
}

test('Drums chart chip cycles any → has → lacks → any', () => {
    const { run } = boot();
    assert.equal(run("nextDrumsFlag('')"), '1');
    assert.equal(run("nextDrumsFlag('1')"), '0');
    assert.equal(run("nextDrumsFlag('0')"), '');
    assert.equal(run("drumsTri('1')"), 'has');
    assert.equal(run("drumsTri('0')"), 'lacks');
    assert.equal(run("drumsTri('')"), 'any');
});

test('queryParams sends has_drums for both sides, and nothing when unset', () => {
    const { run } = boot();
    assert.equal(run("queryParams().get('has_drums')"), null);
    run("state.filters.drums = '1'");
    assert.equal(run("queryParams().get('has_drums')"), '1');
    run("state.filters.drums = '0'");
    assert.equal(run("queryParams().get('has_drums')"), '0');
});

test('queryParams carries Keys through arrangements_has / arrangements_lacks', () => {
    const { run } = boot();
    run("state.filters.arr_has = ['Keys']");
    assert.equal(run("queryParams().get('arrangements_has')"), 'Keys');
    run("state.filters.arr_has = []; state.filters.arr_lacks = ['Keys']");
    assert.equal(run("queryParams().get('arrangements_lacks')"), 'Keys');
});

test('queryParams sends has_lyrics=pitch for Vocal pitch', () => {
    const { run } = boot();
    run("state.filters.lyrics = 'pitch'");
    assert.equal(run("queryParams().get('has_lyrics')"), 'pitch');
});

test('the table view and collections get the same Parts params', () => {
    const { run } = boot();
    run("state.filters.drums = '0'; state.filters.arr_has = ['Keys']; state.filters.lyrics = 'pitch'");
    const rules = run('currentFilterRules()');
    assert.equal(rules.has_drums, '0');
    assert.equal(rules.arrangements_has, 'Keys');
    assert.equal(rules.has_lyrics, 'pitch');
    // loadTable builds its URL from queryParams (the same builder as the grid).
    const at = src.indexOf('function loadTable(');
    assert.match(src.slice(at, at + 1200), /url: '\/api\/library\/table\?' \+ queryParams\(extra\)\.toString\(\)/);
    // The public filterParams() (plugins) also carries has_drums.
    const fp = src.indexOf('filterParams: () => {');
    assert.match(src.slice(fp, fp + 1200), /p\.set\('has_drums',\s*f\.drums\)/);
});

test('a Drums filter counts toward the active-filter badge and the state hash', () => {
    const { run } = boot();
    assert.equal(run('activeFilterCount()'), 0);
    const before = run('buildLibraryStateHash(state)');
    run("state.filters.drums = '1'");
    assert.equal(run('activeFilterCount()'), 1);
    assert.notEqual(run('buildLibraryStateHash(state)'), before);
    run("state.filters.arr_lacks = ['Keys']");
    assert.equal(run('activeFilterCount()'), 2);
});

test('prefs round-trip keeps Drums + Keys and drops junk', () => {
    const { run, localStorage } = boot();
    run("state.filters.drums = '0'; state.filters.arr_has = ['Lead', 'Keys']; state.filters.lyrics = 'pitch'; state.filters.ff = ['bass']");
    run('saveLibraryPrefs()');
    const saved = JSON.parse(localStorage.getItem('v3:songs-prefs'));
    assert.equal(saved.filters.drums, '0');

    const b = boot(saved);
    b.run('applySavedPrefs()');
    assert.equal(b.run('state.filters.drums'), '0');
    assert.deepEqual(Array.from(b.run('state.filters.arr_has')), ['Lead', 'Keys']);
    assert.equal(b.run('state.filters.lyrics'), 'pitch');
    assert.deepEqual(Array.from(b.run('state.filters.ff')), ['bass']);

    const junk = boot({ filters: { drums: 'yes', arr_has: ['Keys', 'Vocals'], arr_lacks: ['Piano'] } });
    junk.run('applySavedPrefs()');
    assert.equal(junk.run('state.filters.drums'), '');
    assert.deepEqual(Array.from(junk.run('state.filters.arr_has')), ['Keys']);
    assert.deepEqual(Array.from(junk.run('state.filters.arr_lacks')), []);
});

test('no Drums filter saves no drums key (old prefs stay byte-identical)', () => {
    const { run, localStorage } = boot();
    run('saveLibraryPrefs()');
    const saved = JSON.parse(localStorage.getItem('v3:songs-prefs'));
    assert.equal('drums' in saved.filters, false);
});

test('drawer: one Parts section holds Drums, Vocal pitch, Keys and the 5-fret parts', () => {
    const { run, drawer } = boot();
    run("state.filters.drums = '1'; state.filters.arr_lacks = ['Keys']; state.filters.lyrics = 'pitch'; state.filters.ff = ['guitar']");
    run('renderDrawer()');
    const html = drawer.innerHTML;
    const parts = html.indexOf('>Parts<');
    assert.ok(parts >= 0, 'no Parts heading');
    const next = html.indexOf('>Progress<');
    const body = html.slice(parts, next);
    // Active pills: ✓ for has, ✕ for lacks, highlighted for toggles.
    assert.match(body, /data-tri="drums"[^>]*class="[^"]*fb-good[^"]*"[^>]*>✓ Drums chart</);
    assert.match(body, /data-tri="arr" data-val="Keys"[^>]*class="[^"]*fb-low[^"]*"[^>]*>✕ Keys</);
    assert.match(body, /data-vocal-pitch[^>]*bg-fb-primary[^>]*>✓ Vocal pitch</);
    assert.match(body, /Rock Band \/ Guitar Hero/);
    assert.match(body, /data-ff="guitar"/);
    // The old standalone 5-fret section is gone (exactly one heading for it).
    assert.equal(html.split('Rock Band / Guitar Hero').length - 1, 1);
    // Keys is not offered twice (it lives in Parts, not Arrangements).
    assert.equal(html.split('data-val="Keys"').length - 1, 1);
});

test('drawer clear resets the Drums flag', () => {
    const at = src.indexOf("d.querySelector('[data-drawer-clear]')");
    assert.ok(at >= 0);
    assert.match(src.slice(at, at + 400), /drums: ''/);
});

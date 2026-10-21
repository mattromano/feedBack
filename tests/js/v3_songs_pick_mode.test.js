'use strict';
// Pins the Songs screen's pick mode in static/v3/songs.js (feedBack.library.pick,
// session in static/v3/library-pick.js): chip params in the grid query, a card
// click that selects instead of playing, the "Selected" badge, the pick bar.
// Source checks plus mirrors (the v3_songs_karaoke_filter / _window_recycle
// patterns) — keep the mirrors in sync with songs.js.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const src = fs.readFileSync(path.join(__dirname, '..', '..', 'static', 'v3', 'songs.js'), 'utf8');

function fnBody(name) {
    const at = src.indexOf('function ' + name + '(');
    assert.ok(at >= 0, name + ' not found');
    const ends = ['\n    function ', '\n    async function ']
        .map((m) => src.indexOf(m, at + 10)).filter((i) => i >= 0);
    return src.slice(at, ends.length ? Math.min(...ends) : undefined);
}

// ── Source checks ────────────────────────────────────────────────────────────

test('queryParams applies the pick params before extra', () => {
    const body = fnBody('queryParams');
    const m = body.match(/pk\.params\(\)\)\.forEach\(\(\[k, v\]\) => p\.set\(k, v\)\)/);
    assert.ok(m, 'pick params not applied in queryParams');
    const extraAt = body.indexOf('Object.entries(extra || {})');
    assert.ok(extraAt >= 0);
    assert.ok(m.index < extraAt, 'pick params must come before extra');
});

test('libHomeVisible hides the home while picking', () => {
    assert.match(fnBody('libHomeVisible'), /!pickOn\(\)/);
});

test('bindPickGuard is bound before bindSelectGuard on each host', () => {
    ['v3-songs-grid', 'v3-songs-tree', 'v3-songs-artistpage'].forEach((id) => {
        const pick = src.indexOf("bindPickGuard(byId('" + id + "'))");
        const sel = src.indexOf("bindSelectGuard(byId('" + id + "'))");
        assert.ok(pick >= 0, 'no pick guard on ' + id);
        assert.ok(sel >= 0);
        assert.ok(pick < sel, 'pick guard must come first on ' + id);
    });
});

test('bindPickGuard handles click and contextmenu with stopImmediatePropagation', () => {
    const body = fnBody('bindPickGuard');
    assert.match(body, /'click'/);
    assert.match(body, /'contextmenu'/);
    assert.equal((body.match(/stopImmediatePropagation\(\)/g) || []).length, 2);
});

test('saveLibraryPrefs never persists the pick-forced grid', () => {
    assert.match(fnBody('saveLibraryPrefs'), /view: state\.pickPrevView \|\| state\.view/);
});

test('the state hash carries the pick key', () => {
    assert.match(fnBody('buildLibraryStateHash'), /pick: st\.pickKey \|\| ''/);
});

test('songs.js listens for library:pick-changed', () => {
    assert.match(src, /sm\.on\('library:pick-changed'/);
});

test('the Selected badge and the bar copy', () => {
    assert.match(src, /data-pick-badge[^>]*>Selected<\/span>/);
    assert.match(src, /Only songs on this machine can be picked/);
    assert.match(src, /Nothing selected yet/);
});

test('the pick bar is torn down when leaving Songs', () => {
    const at = src.indexOf("sm.on('screen:changed'");
    assert.ok(at >= 0);
    assert.match(src.slice(at, at + 1200), /v3-songs-pickbar/);
});

test('onV3SongsScreenEnter ends with renderPickBar on every path', () => {
    const body = fnBody('onV3SongsScreenEnter');
    assert.match(body, /await _enterSongsScreen\(\);\s*\} finally \{[^}]*renderPickBar\(\);\s*\}/);
});

test('renderPickBar restores focus to the toggled chip by its id', () => {
    const body = fnBody('renderPickBar');
    const save = body.indexOf("document.activeElement.getAttribute('data-pick-filter')");
    const rebuild = body.indexOf('bar.innerHTML =');
    const restore = body.indexOf('again.focus()');
    assert.ok(save >= 0 && rebuild >= 0 && restore >= 0);
    assert.ok(save < rebuild && rebuild < restore, 'read the focused chip id, rebuild, then refocus it');
    assert.match(body, /b\.getAttribute\('data-pick-filter'\) === focusedChip/);
});

test('window.v3Songs exposes pickActive', () => {
    assert.match(src, /pickActive: pickOn/);
});

test('CRLF kept', () => {
    const lf = (src.match(/\n/g) || []).length;
    const crlf = (src.match(/\r\n/g) || []).length;
    assert.ok(lf > 0);
    assert.equal(crlf, lf, 'every \\n in songs.js must be preceded by \\r');
});

// ── Mirror: pickCard + the pick guard's decision ─────────────────────────────

function makePickApi(selectedFilename) {
    const calls = [];
    let selected = selectedFilename ? { filename: selectedFilename } : null;
    return {
        calls,
        session: () => ({ selected }),
        params: () => ({}),
        select: (item) => { calls.push(['select', item]); selected = item; return true; },
        confirm: () => { calls.push(['confirm']); return true; },
    };
}

function pickHarness(api, songsById, localFilename) {
    const state = { songsById };
    let note = null;
    let played = 0;
    const pickApi = () => api;
    function pickOn() { const p = pickApi(); return !!(p && p.session()); }
    function pickSelectedKey() { const p = pickApi(); const s = p && p.session(); return (s && s.selected && s.selected.filename) || ''; }
    function renderPickBar(n) { note = n; }
    // mirror of songs.js pickCard
    function pickCard(cardEl) {
        const fn = cardEl.getAttribute('data-fn');
        const song = state.songsById[fn];
        if (!song) return;
        const playTarget = song.display_chart ? Object.assign({}, song, song.display_chart) : song;
        const lf = localFilename(song);
        if (!lf) { renderPickBar('Only songs on this machine can be picked'); return; }
        if (pickSelectedKey() === lf) { pickApi().confirm(); return; }
        pickApi().select(Object.assign({}, playTarget, { filename: lf }));
    }
    // mirror of the bindPickGuard click handler (capture phase on the host)
    function guardClick(hostEl, e) {
        if (!pickOn()) return;
        const card = e.target.closest('[data-fn]');
        if (!card || !hostEl.contains(card)) return;
        e.preventDefault();
        e.stopImmediatePropagation();
        pickCard(card);
    }
    return { guardClick, getNote: () => note, played: () => played };
}

function fakeCard(fn) {
    return { getAttribute: (k) => (k === 'data-fn' ? fn : null) };
}
function fakeEvent(card) {
    const ev = { prevented: false, stopped: false };
    ev.target = { closest: () => card };
    ev.preventDefault = () => { ev.prevented = true; };
    ev.stopImmediatePropagation = () => { ev.stopped = true; };
    return ev;
}
const host = { contains: () => true };

test('pickCard: an unselected local card selects with display_chart merged', () => {
    const api = makePickApi(null);
    const songs = { 'a.sloppak': { filename: 'a.sloppak', title: 'A', display_chart: { arrangements: ['x'] } } };
    const h = pickHarness(api, songs, (s) => s.filename || '');
    const ev = fakeEvent(fakeCard('a.sloppak'));
    h.guardClick(host, ev);
    assert.ok(ev.prevented && ev.stopped, 'the guard stops the click before any play handler');
    assert.equal(api.calls.length, 1);
    assert.equal(api.calls[0][0], 'select');
    assert.equal(api.calls[0][1].filename, 'a.sloppak');
    assert.deepEqual(api.calls[0][1].arrangements, ['x']);
    assert.equal(h.getNote(), null);
});

test('pickCard: the selected card confirms', () => {
    const api = makePickApi('a.sloppak');
    const songs = { 'a.sloppak': { filename: 'a.sloppak', title: 'A' } };
    const h = pickHarness(api, songs, (s) => s.filename || '');
    h.guardClick(host, fakeEvent(fakeCard('a.sloppak')));
    assert.deepEqual(api.calls, [['confirm']]);
});

test('pickCard: a remote card with no local file is refused with a note', () => {
    const api = makePickApi(null);
    const songs = { 'r:1': { id: 'r:1', title: 'Remote' } };
    const h = pickHarness(api, songs, () => '');
    const ev = fakeEvent(fakeCard('r:1'));
    h.guardClick(host, ev);
    assert.ok(ev.prevented && ev.stopped);
    assert.deepEqual(api.calls, []);
    assert.equal(h.getNote(), 'Only songs on this machine can be picked');
});

test('the guard passes clicks through without a pick session', () => {
    const api = { session: () => null };
    const h = pickHarness(api, {}, (s) => s.filename || '');
    const ev = fakeEvent(fakeCard('a.sloppak'));
    h.guardClick(host, ev);
    assert.equal(ev.prevented, false);
    assert.equal(ev.stopped, false);
});

test('pickCard mirror matches the source', () => {
    const body = fnBody('pickCard');
    assert.match(body, /if \(!lf\) \{ renderPickBar\('Only songs on this machine can be picked'\); return; \}/);
    assert.match(body, /if \(pickSelectedKey\(\) === lf\) \{ pickApi\(\)\.confirm\(\); return; \}/);
    assert.match(body, /pickApi\(\)\.select\(Object\.assign\(\{\}, playTarget, \{ filename: lf \}\)\)/);
});

// ── Mirror: _cardSig ─────────────────────────────────────────────────────────

const SIG_PICK = "(pickOn() ? (pickIsSelected(state.songs[i]) ? 'P' : 'p') : '')";
const IS_SELECTED = "function pickIsSelected(song) { const k = song ? pickSelectedKey() : ''; return !!k && k === localFilename(song); }";

test('pickIsSelected mirror matches the source, and songCard uses it', () => {
    assert.ok(src.includes(IS_SELECTED), 'pickIsSelected drifted from the mirror');
    assert.match(fnBody('songCard'), /const pickSel = pickIsSelected\(song\);/);
});

test('_cardSig mirror matches the source', () => {
    assert.ok(fnBody('_cardSig').includes(SIG_PICK), '_cardSig pick marker drifted from the mirror');
});

function sigHarness() {
    const state = { songs: [], selectMode: false };
    for (let i = 0; i < 6; i++) state.songs[i] = { filename: 'song' + i };
    state.songs[6] = undefined;   // a skeleton hole
    state.songs[7] = { id: 'remote-1' };   // an unsynced remote row: no local file
    let session = null;
    const pickOn = () => !!session;
    const pickSelectedKey = () => (session && session.selected && session.selected.filename) || '';
    const localFilename = (s) => s.filename || '';
    // eslint-disable-next-line no-eval
    const pickIsSelected = eval('(' + IS_SELECTED + ')');
    // eslint-disable-next-line no-unused-vars
    function _cardSig(i) {
        return (state.songs[i] ? 'r' : 's') + (state.selectMode ? '1' : '0') + eval(SIG_PICK);
    }
    const all = () => Array.from({ length: 8 }, (_, i) => _cardSig(i));
    return { all, setSession: (s) => { session = s; } };
}

test('_cardSig: no session keeps today\'s signature', () => {
    const h = sigHarness();
    assert.deepEqual(h.all(), ['r0', 'r0', 'r0', 'r0', 'r0', 'r0', 's0', 'r0']);
});

test('_cardSig: with nothing selected no card (not even a remote one) is marked selected', () => {
    const h = sigHarness();
    h.setSession({ selected: null });
    assert.deepEqual(h.all(), ['r0p', 'r0p', 'r0p', 'r0p', 'r0p', 'r0p', 's0p', 'r0p']);
    h.setSession({ selected: { filename: 'song2' } });
    const changed = h.all().map((s, i) => (s === 'r0P' ? i : -1)).filter((i) => i >= 0);
    assert.deepEqual(changed, [2], 'the first selection rebuilds only the selected card');
});

test('_cardSig: moving the selection changes only the two cards involved', () => {
    const h = sigHarness();
    h.setSession({ selected: { filename: 'song1' } });
    const before = h.all();
    assert.equal(before[1], 'r0P');
    assert.equal(before[6], 's0p');
    h.setSession({ selected: { filename: 'song4' } });
    const after = h.all();
    const changed = before.map((s, i) => (s !== after[i] ? i : -1)).filter((i) => i >= 0);
    assert.deepEqual(changed, [1, 4]);
});

// ── First-visit prefs fix pins (Task 2 M8 / M9) ──────────────────────────────

test('render restores the saved view state before loading providers', () => {
    // the module-level async render() (nested views have their own render())
    const at = src.indexOf('\n    async function render() {');
    assert.ok(at >= 0, 'async render not found');
    const body = src.slice(at, src.indexOf('\n    function ', at + 10));
    const restore = body.indexOf('_restoreViewState();');
    const providers = body.indexOf('await loadProviders()');
    assert.ok(restore >= 0, 'render must call _restoreViewState()');
    assert.ok(providers >= 0);
    assert.ok(restore < providers, '_restoreViewState() must run before loadProviders()');
});

test('saveLibraryPrefs never writes before the saved prefs were restored', () => {
    const body = fnBody('saveLibraryPrefs');
    const first = body.slice(body.indexOf('{') + 1).trim();
    assert.ok(first.startsWith('if (!_prefsRestored) return;'), 'the first statement must be the _prefsRestored guard');
});

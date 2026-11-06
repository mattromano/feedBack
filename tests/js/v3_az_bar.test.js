// Pins the A–Z jump bar across the top of the v3 library (static/v3/songs.js):
// one row of letter links that works on the card grid (alphabetical sorts),
// Albums and the artist list, replacing the side rail as the visible jump
// (the rail keeps the letter data + jump math, hidden). Source-level, like
// tests/js/v3_az_rail.test.js. Also pins the tuner-badge update calming in
// static/v3/badges.js that stopped native <select> popups (library
// dropdowns) from snapping shut while a live guitar was connected.

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const songs = fs.readFileSync(path.join(__dirname, '..', '..', 'static', 'v3', 'songs.js'), 'utf8');
const badges = fs.readFileSync(path.join(__dirname, '..', '..', 'static', 'v3', 'badges.js'), 'utf8');

test('the bar is rendered in the library toolbar', () => {
    assert.match(songs, /<nav id="v3-songs-azbar" class="hidden[^"]*" aria-label="Jump to letter"><\/nav>/);
});

test('renderAzBar covers grid, Albums and the artist list, hides elsewhere', () => {
    assert.match(songs, /function renderAzBar\(\)/);
    assert.match(songs, /state\.view === 'albums'[\s\S]*?\[data-az-artist\]/);
    assert.match(songs, /state\.view === 'tree'[\s\S]*?details\[data-artist\]/);
    assert.match(songs, /state\.view === 'grid' && railSortColumn\(\) && state\.railLetters/);
    assert.match(songs, /if \(!present \|\| !present\.size\) \{ bar\.classList\.add\('hidden'\)/);
});

test('albums are tagged with their artist for the jump', () => {
    assert.match(songs, /data-album="' \+ i \+ '" data-az-artist="' \+ esc\(a\.artist \|\| ''\) \+ '"/);
});

test('every view refresh repaints the bar', () => {
    // Albums + artist list after they render; grid after the rail letters land
    // (and on every hidden path, so a stale view's letters never linger).
    const loadAlbums = songs.slice(songs.indexOf('async function loadAlbums('), songs.indexOf('async function openAlbum('));
    assert.match(loadAlbums, /renderAzBar\(\);/);
    const loadTree = songs.slice(songs.indexOf('async function loadTree('), songs.indexOf('// ── Filter drawer'));
    assert.match(loadTree, /renderAzBar\(\);/);
    const refreshRail = songs.slice(songs.indexOf('async function refreshRail('), songs.indexOf('function _setRailActive('));
    assert.equal((refreshRail.match(/renderAzBar\(\)/g) || []).length, 4);
});

test('letters only for entries that exist; grid jumps reuse jumpToLetter', () => {
    assert.match(songs, /\(on \? '' : ' disabled'\)/);
    assert.match(songs, /else if \(state\.view === 'grid'\) \{\s*_azGridJump\(letter\);/);
    // The grid jump seeks with the count math, then corrects against rendered cards.
    const gj = songs.slice(songs.indexOf('async function _azGridJump('), songs.indexOf('function renderAzBar('));
    assert.match(gj, /await jumpToLetter\(letter, false\);/);
    // Verify-and-nudge: keeps correcting until the letter's first card sits
    // under the stuck toolbar (the grid can re-lay out after a scroll).
    assert.match(gj, /const off = cards\[i\]\.getBoundingClientRect\(\)\.top - landingLine\(\);/);
    assert.match(gj, /if \(Math\.abs\(off\) <= 4\) return;/);
    // Albums/list jumps land below the STUCK toolbar (its CSS top + height).
    assert.match(songs, /const pad = stickTop \+ \(toolbar \? toolbar\.offsetHeight : 0\) \+ 8;/);
    // The side rail no longer shows alongside the bar (no duplicate control).
    assert.match(songs, /rail\.classList\.add\('hidden'\);\s*bindRailOnce\(\);\s*renderAzBar\(\);/);
});

test('tuner badge: holds while a <select> is focused, throttles, writes only changes', () => {
    assert.match(badges, /function _selectHasFocus\(\)[\s\S]*?a\.tagName === 'SELECT'/);
    assert.match(badges, /function _applyFrame\(frame\) \{\s*if \(_selectHasFocus\(\)\) return;/);
    assert.match(badges, /const _MIN_FRAME_MS = 100;/);
    assert.match(badges, /function _setText\(el, text\) \{ if \(el && el\.textContent !== text\)/);
    assert.match(badges, /function _setClass\(el, cls\) \{ if \(el && el\.className !== cls\)/);
    // No direct per-frame writes left in _applyFrame.
    const body = badges.slice(badges.indexOf('function _applyFrame(frame)'), badges.indexOf('// ── Tuner card'));
    assert.doesNotMatch(body, /\.textContent = |s\.className = /);
});

test('Albums view pages through every album, not just the first 120', () => {
    const loadAlbums = songs.slice(songs.indexOf('async function loadAlbums('), songs.indexOf('async function openAlbum('));
    assert.match(loadAlbums, /for \(let page = 0, total = Infinity; albums\.length < total; page\+\+\)/);
    assert.match(loadAlbums, /queryParams\(\{ size: 500, page \}\)/);
    assert.match(loadAlbums, /if \(!batch\.length\) break;/);
});

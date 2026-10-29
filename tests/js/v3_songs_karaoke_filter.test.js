// Pins the library's vocals filtering in static/v3/songs.js.
// The Arrangements → "Vocals" pill was dead UI: the server only accepts
// Lead/Rhythm/Bass/Combo (lib/metadata_db.py _ALLOWED_ARRANGEMENT_NAMES) and the
// scanner never stores a vocals arrangement, so the pill silently filtered
// nothing. Karaoke-ready means the lyrics plugin's singing mode actually runs:
// per-syllable vocal pitch (has_lyrics=pitch) plus a separated vocals stem.
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const src = fs.readFileSync(path.join(__dirname, '..', '..', 'static', 'v3', 'songs.js'), 'utf8');

test('Arrangements facet no longer offers the dead Vocals pill', () => {
    const m = src.match(/const ARRANGEMENTS = \[([^\]]*)\];/);
    assert.ok(m, 'ARRANGEMENTS constant not found');
    assert.doesNotMatch(m[1], /Vocals/);
});

test('drawer renders a Karaoke quick filter button', () => {
    assert.match(src, /data-karaoke/);
    assert.match(src, /Karaoke-ready/);
});

test('Karaoke filter toggles lyrics-with-pitch plus the vocals stem', () => {
    const at = src.indexOf("d.querySelectorAll('[data-karaoke]')");
    assert.ok(at >= 0, 'no [data-karaoke] click handler');
    const handler = src.slice(at, at + 900);
    assert.match(handler, /f\.lyrics = 'pitch'/);
    assert.match(handler, /f\.stem_has\.push\('vocals'\)/);
    assert.match(handler, /f\.stem_lacks = f\.stem_lacks\.filter\(\(s\) => s !== 'vocals'\)/);
    assert.match(handler, /renderDrawer\(\)/);
});

test('restored filters drop arrangement names the facet no longer offers', () => {
    assert.match(src,
        // ('Keys' is kept too: it is the Parts facet's keys part.)
        /arr_has: arr\(f\.arr_has\)\.filter\(\(a\) => ARRANGEMENTS\.includes\(a\) \|\| a === 'Keys'\), arr_lacks: arr\(f\.arr_lacks\)\.filter\(\(a\) => ARRANGEMENTS\.includes\(a\) \|\| a === 'Keys'\),/);
});

// Songs table view (static/v3/songs-table.js): the pure helpers behind the
// sortable, virtualised table — payload decoding, column sorting (with the
// artist → title tiebreak and blanks-last rule), part letters and the
// "last played" formatting — plus the songs.js wiring of the 'table' view.

'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const ROOT = path.join(__dirname, '..', '..', 'static', 'v3');
const TABLE_SRC = fs.readFileSync(path.join(ROOT, 'songs-table.js'), 'utf8');
const SONGS_SRC = fs.readFileSync(path.join(ROOT, 'songs.js'), 'utf8');

function load() {
    const sandbox = { window: {}, localStorage: { getItem: () => null, setItem() {} } };
    vm.createContext(sandbox);
    vm.runInContext(TABLE_SRC, sandbox);
    return sandbox.window.v3SongsTable;
}

const COLS = ['filename', 'play_filename', 'artist', 'title', 'album', 'year', 'duration', 'tuning',
    'arrangements', 'format', 'plays', 'best_accuracy', 'full_combos', 'last_played', 'seconds'];
const row = (o) => COLS.map((c) => (c in o ? o[c] : (c === 'arrangements' ? [] : null)));

function rows(t) {
    return t._internals.toRows({
        cols: COLS,
        rows: [
            row({ filename: 'a', artist: 'The Zebras', title: 'Alpha', year: '2001', plays: 3, best_accuracy: 0.95, last_played: '2026-01-02 10:00:00.000' }),
            row({ filename: 'b', artist: 'Aardvark', title: 'Bravo', year: '1990', plays: 3, best_accuracy: null }),
            row({ filename: 'c', artist: '', title: 'Charlie', year: '', plays: 0, best_accuracy: 0.4 }),
            row({ filename: 'd', artist: 'Aardvark', title: 'Able', plays: 9, full_combos: 2, arrangements: ['Rhythm', 'Lead', 'Bass 2'] }),
        ],
    });
}
const order = (list) => list.map((r) => r.filename);

test('toRows decodes the columnar payload and precomputes sort keys', () => {
    const t = load();
    const r = rows(t);
    assert.equal(r.length, 4);
    assert.equal(r[0].playFilename, 'a');            // falls back to filename
    assert.equal(r[0].artistSort, 'zebras');         // leading "The" ignored
    assert.equal(r[0].yearNum, 2001);
    assert.equal(r[3].parts, 'LRB');                 // fixed instrument order
    assert.ok(r[0].lastMs > 0 && r[1].lastMs === 0);
});

test('sorting: numeric columns, artist→title tiebreak, blanks last', () => {
    const t = load();
    const r = rows(t);
    // Most played first; the 3-play tie breaks by artist (Aardvark before Zebras).
    assert.deepEqual(order(t._internals.sortRows(r, { key: 'plays', dir: -1 })), ['d', 'b', 'a', 'c']);
    // Best: unscored (null) sinks below a real 40% when sorting best-first.
    assert.deepEqual(order(t._internals.sortRows(r, { key: 'best', dir: -1 })), ['a', 'c', 'd', 'b']);
    // Artist A→Z: blank artist last; Aardvark's titles in title order.
    assert.deepEqual(order(t._internals.sortRows(r, { key: 'artist', dir: 1 })), ['d', 'b', 'a', 'c']);
    // Year newest first; no year sinks.
    assert.deepEqual(order(t._internals.sortRows(r, { key: 'year', dir: -1 })), ['a', 'b', 'd', 'c']);
    // Full combos first.
    assert.equal(t._internals.sortRows(r, { key: 'fc', dir: -1 })[0].filename, 'd');
});

test('part letters cover keys/drums/vocals and ignore unknown parts', () => {
    const { parts } = load()._internals;
    assert.equal(parts(['Vocals', 'Lead', 'Piano', 'Drums']), 'LKDV');
    assert.equal(parts(['Bass', 'Bass 2']), 'B');
    assert.equal(parts(['Something else']), '');
    assert.equal(parts(undefined), '');
});

test('last played reads SQLite UTC stamps and formats relative to now', () => {
    const { parseUtc, fmtLast } = load()._internals;
    const ms = parseUtc('2026-03-01 12:00:00.000');
    assert.equal(ms, Date.UTC(2026, 2, 1, 12));
    assert.equal(parseUtc(null), 0);
    assert.equal(parseUtc('garbage'), 0);
    const day = 86400000;
    assert.equal(fmtLast(ms, ms + 3600000), 'today');
    assert.equal(fmtLast(ms, ms + 1.5 * day), 'yesterday');
    assert.equal(fmtLast(ms, ms + 10 * day), '10d ago');
    assert.equal(fmtLast(ms, ms + 90 * day), '3mo ago');
    assert.equal(fmtLast(0, ms), '');
});

test('songs.js wires the table view (button, host, loader, persisted view)', () => {
    assert.match(SONGS_SRC, /id="v3-songs-table-btn"/);
    assert.match(SONGS_SRC, /id="v3-songs-table"/);
    assert.match(SONGS_SRC, /setView\('table'\)/);
    assert.match(SONGS_SRC, /'\/api\/library\/table\?'/);
    assert.match(SONGS_SRC, /\['grid', 'tree', 'folder', 'albums', 'table'\]\.includes\(saved\.view\)/);
});

const FCOLS = COLS.concat(['arr_index', 'has_drum_tab', 'has_vocal_pitch', 'five_fret_parts', 'five_fret_only', 'stem_ids']);
const frow = (o) => FCOLS.map((c) => (c in o ? o[c] : (['arrangements', 'arr_index', 'five_fret_parts', 'stem_ids'].includes(c) ? [] : null)));

test('drum chart and vocal pitch add D and V; a 5-fret-only placeholder adds nothing', () => {
    const { parts } = load()._internals;
    assert.equal(parts(['Lead', 'Bass'], { drums: true }), 'LBD');
    assert.equal(parts(['Lead'], { vocals: true }), 'LV');
    assert.equal(parts(['Lead Guitar'], { fiveFretOnly: true }), '');
});

test('5-fret pills: G R B order, chart beats auto, auto only when shown', () => {
    const { fiveFretPills } = load()._internals;
    const tags = ['bass:auto', 'guitar:chart', 'rhythm:auto', 'rhythm:chart', 'keys:chart'];
    const letters = (list) => list.map((p) => p.letter + (p.auto ? '*' : '')).join(' ');
    assert.equal(letters(fiveFretPills(tags, false)), 'G R');
    assert.equal(letters(fiveFretPills(tags, true)), 'G R B*');
    assert.equal(fiveFretPills(undefined, true).length, 0);
});

test('toRows builds the play-as song item and sorts parts with pills', () => {
    const t = load();
    const data = {
        cols: FCOLS,
        rows: [
            frow({ filename: 'x', play_filename: 'x2', title: 'X', arrangements: ['Lead', 'Bass'], arr_index: [0, 1],
                has_drum_tab: true, has_vocal_pitch: true, five_fret_parts: ['guitar:chart', 'bass:auto'], stem_ids: ['drums'] }),
            frow({ filename: 'y', title: 'Y', arrangements: ['Lead', 'Bass'] }),
        ],
    };
    const [x, y] = t._internals.toRows(data, false);
    assert.equal(x.song.filename, 'x2');                      // the displayed chart plays
    assert.deepEqual(JSON.parse(JSON.stringify(x.song.arrangements)), [{ index: 0, name: 'Lead' }, { index: 1, name: 'Bass' }]);
    assert.equal(x.song.has_drum_tab, true);
    assert.equal(x.song.has_vocal_pitch, true);
    assert.equal(x.parts, 'LBDV');
    assert.equal(x.partsKey, 'LBDV G');                       // bass:auto hidden
    assert.equal(y.partsKey, 'LB');
    assert.equal(t._internals.toRows(data, true)[0].partsKey, 'LBDV GB');
    // Old payloads (no new columns) still decode.
    assert.equal(rows(t)[3].song.five_fret_parts.length, 0);
});

test('songs.js opens Play as from a table row and the card menu', () => {
    assert.match(SONGS_SRC, /menu: window\.v3PlayAs \?/);
    assert.match(SONGS_SRC, /id: '__playas', label: 'Play as…'/);
});

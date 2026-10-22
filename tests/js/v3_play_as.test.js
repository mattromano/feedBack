// "Play as" (static/v3/play-as.js): which real-instrument parts a song offers,
// how registry actions become menu rows, and the per-play viz rule.

'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const pa = require('../../static/v3/play-as.js');
const reg = require('../../static/capabilities/library-card-actions.js');

test('realArrangements: fretted and keys parts only, none for a 5-fret-only pack', () => {
    const song = { arrangements: [{ index: 0, name: 'Lead' }, { index: 1, name: 'Rhythm 2' }, { index: 2, name: 'Bass' },
        { index: 3, name: 'Combo' }, { index: 4, name: 'Keys' }, { index: 5, name: 'Drums' }, { index: 6, name: 'Vocals' }] };
    assert.deepEqual(pa.realArrangements(song).map((a) => a.index), [0, 1, 2, 3, 4]);
    assert.deepEqual(pa.realArrangements({ arrangements: [{ index: 0, name: 'Lead Guitar' }], five_fret_only: true }), []);
    assert.deepEqual(pa.realArrangements({}), []);
    assert.deepEqual(pa.realArrangements({ arrangements: ['Lead', 'Bass'] }), [{ index: 0, name: 'Lead' }, { index: 1, name: 'Bass' }]);
});

test('menuRows: play-as actions only, one row per variant, default flagged', () => {
    reg.snapshot().actions.forEach((a) => reg.unregister(a.id));
    reg.register({ id: 'core.play-as.play', label: 'Play', placement: 'play-as', order: 0, run() {} });
    reg.register({ id: 'other', label: 'Details', placement: 'menu', run() {} });
    reg.register({ id: 'ff', label: '5-Fret', group: 'Controller', hint: 'controller', placement: 'play-as', order: 40,
        variants: () => [{ key: 'guitar', label: '5-Fret Guitar' }, { key: 'bass', label: '5-Fret Bass', enabled: false }], run() {} });
    const rows = pa.menuRows(reg.list({ filename: 'a' }));
    assert.deepEqual(rows.map((r) => [r.id, r.variant, r.label, r.group, r.hint, r.enabled, r.isDefault]), [
        ['core.play-as.play', null, 'Play', '', '', true, true],
        ['ff', 'guitar', '5-Fret Guitar', 'Controller', 'controller', true, false],
        ['ff', 'bass', '5-Fret Bass', 'Controller', 'controller', false, false],
    ]);
});

function keeper(initial) {
    const st = { viz: initial, saved: null, calls: [] };
    const k = pa.createVizKeeper({
        current: () => st.viz,
        setViz: (id) => { st.calls.push(id); st.viz = id; },
        load: () => st.saved,
        save: (v) => { st.saved = v; },
    });
    return { st, k };
}

test('a forced view lasts for its own play and comes back on the next other play', () => {
    const { st, k } = keeper('highway_3d');
    k.beforePlay('song%20a', 'five_fret');
    assert.equal(st.viz, 'five_fret');
    k.onSongLoading('song%20a');                 // our own play: kept
    assert.equal(st.viz, 'five_fret');
    k.onSongLoading('other');                    // a grid-card play: the viewer's pick comes back
    assert.equal(st.viz, 'highway_3d');
    assert.equal(st.saved, null);
});

test('chained forced views keep the first saved pick; a plain Play-as play restores it', () => {
    const { st, k } = keeper('auto');
    k.beforePlay('a', 'five_fret');
    k.onSongLoading('a');
    k.beforePlay('b', 'drums');
    assert.deepEqual(st.saved, { forced: 'drums', prev: 'auto' });
    k.onSongLoading('b');
    k.beforePlay('c', null);
    assert.equal(st.viz, 'auto');
    k.onSongLoading('c');
    assert.equal(st.viz, 'auto');
});

test('a view the viewer picked is never undone', () => {
    const own = keeper('five_fret');
    own.k.beforePlay('a', 'five_fret');          // already theirs: nothing to restore later
    own.k.onSongLoading('a');
    own.k.onSongLoading('b');
    assert.equal(own.st.viz, 'five_fret');

    const changed = keeper('default');
    changed.k.beforePlay('a', 'five_fret');
    changed.k.onSongLoading('a');
    changed.st.viz = 'highway_3d';               // picked from the viz picker mid-song
    changed.k.onSongLoading('b');
    assert.equal(changed.st.viz, 'highway_3d');
});

test('a plain play puts a non-guitar view back to Auto and leaves guitar views alone', () => {
    const piano = keeper('piano');
    piano.k.beforePlay('a', null);
    assert.equal(piano.st.viz, 'auto');
    const hw = keeper('highway_3d');
    hw.k.beforePlay('a', null);
    assert.deepEqual(hw.st.calls, []);
});

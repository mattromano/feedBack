// Verify static/highway.js folds the `five_fret*` WS frames into
// hwState.fiveFretParts (keyed by part_id) and exposes it as
// bundle.fiveFretParts / highway.getFiveFretParts(), without disturbing
// bundle.drumTab. Same isolation strategy as drum_tab_metadata.test.js.

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const HIGHWAY_JS = path.join(__dirname, '..', '..', 'static', 'highway.js');
const SRC = fs.readFileSync(HIGHWAY_JS, 'utf8');

function getCaseBlock(src, label) {
    const start = src.indexOf(`case '${label}'`);
    assert.ok(start !== -1, `case '${label}' not found in highway.js`);
    const tail = src.slice(start);
    const nextCase = tail.slice(1).search(/\n\s*case\s+['"]/);
    const nextDefault = tail.search(/\n\s*default\s*:/);
    let end = tail.length;
    if (nextCase > 0) end = Math.min(end, nextCase + 1);
    if (nextDefault > 0) end = Math.min(end, nextDefault);
    return tail.slice(0, end);
}

function makeDispatcher() {
    const labels = ['drum_tab', 'drum_hits', 'drum_level_hits',
        'five_fret', 'five_fret_notes', 'five_fret_level_notes'];
    const body = labels.map(l => getCaseBlock(SRC, l)).join('\n');
    const ctx = {
        hwState: { drumTab: null, fiveFretParts: null },
        document: { getElementById: () => null },
    };
    vm.createContext(ctx);
    // fiveFretParts starts as highway.js's own init expression
    const init = SRC.match(/hwState\.fiveFretParts = ([^;]+);/);
    assert.ok(init, 'fiveFretParts init not found in highway.js');
    ctx.hwState.fiveFretParts = vm.runInContext(`(${init[1]})`, ctx);
    vm.runInContext(`function feed(msg) { switch (msg.type) {\n${body}\n} }`, ctx);
    return ctx;
}

const plain = (v) => JSON.parse(JSON.stringify(v));

test('five_fret frames fold into bundle.fiveFretParts', () => {
    const ctx = makeDispatcher();
    ctx.feed({ type: 'five_fret', part_id: 'lead5', name: 'Lead 5', instrument: 'guitar',
        source_kind: 'chart', total: 3,
        star_power: [{ start_time: 1, end_time: 2 }], solos: [{ start_time: 3, end_time: 4 }],
        levels: { hard: { total: 2, generated: true }, bogus: { total: 1 } } });
    ctx.feed({ type: 'five_fret', part_id: 'bass5', name: 'Bass 5', instrument: 'bass',
        source_kind: 'midi', total: 1 });
    ctx.feed({ type: 'five_fret_notes', part_id: 'lead5', notes: [{ t: 0.5, f: 1 }, { t: 0.6, f: 2 }] });
    ctx.feed({ type: 'five_fret_notes', part_id: 'bass5', notes: [{ t: 0.7, f: 4 }] });
    ctx.feed({ type: 'five_fret_notes', part_id: 'lead5', notes: [{ t: 0.8, f: 8, l: 0.5 }] });
    ctx.feed({ type: 'five_fret_level_notes', part_id: 'lead5', level: 'hard', notes: [{ t: 0.5, f: 1 }] });
    ctx.feed({ type: 'five_fret_level_notes', part_id: 'lead5', level: 'hard', notes: [{ t: 0.6, f: 2 }] });
    // chunks for an unannounced level or part are dropped
    ctx.feed({ type: 'five_fret_level_notes', part_id: 'lead5', level: 'easy', notes: [{ t: 1, f: 1 }] });
    ctx.feed({ type: 'five_fret_level_notes', part_id: 'lead5', level: '__proto__', notes: [{ t: 1, f: 1 }] });
    ctx.feed({ type: 'five_fret_notes', part_id: 'ghost', notes: [{ t: 1, f: 1 }] });

    const parts = ctx.hwState.fiveFretParts;
    assert.deepEqual(Object.keys(parts), ['lead5', 'bass5']);
    const lead = parts.lead5;
    assert.equal(lead.partId, 'lead5');
    assert.equal(lead.name, 'Lead 5');
    assert.equal(lead.instrument, 'guitar');
    assert.equal(lead.sourceKind, 'chart');
    assert.equal(lead.total, 3);
    assert.equal(lead.notes.length, 3);
    assert.deepEqual(plain(lead.notes[2]), { t: 0.8, f: 8, l: 0.5 });
    assert.deepEqual(plain(lead.starPower), [{ start_time: 1, end_time: 2 }]);
    assert.deepEqual(plain(lead.solos), [{ start_time: 3, end_time: 4 }]);
    assert.deepEqual(Object.keys(lead.levels), ['hard']);
    assert.equal(lead.levels.hard.generated, true);
    assert.equal(lead.levels.hard.notes.length, 2);
    const bass = parts.bass5;
    assert.equal(bass.sourceKind, 'midi');
    assert.equal(bass.notes.length, 1);
    assert.deepEqual(plain(bass.levels), {});
    assert.deepEqual(plain(bass.starPower), []);
    assert.deepEqual(plain(bass.solos), []);
});

test('fiveFretParts is empty before any frame and after a new song', () => {
    const ctx = makeDispatcher();
    assert.deepEqual(plain(ctx.hwState.fiveFretParts), {});
    // a null-prototype object at init and at every hwState reset site that clears drumTab
    assert.equal(SRC.split('hwState.fiveFretParts = Object.create(null);').length - 1, 3);
    assert.doesNotMatch(SRC, /hwState\.fiveFretParts = \{\}/);
    const resets = SRC.split('\n').filter(l => l.includes('hwState.toneBase = ""; hwState.drumTab = null;'));
    assert.equal(resets.length, 2, 'expected the two reset sites');
    for (const l of resets) assert.ok(l.includes('hwState.fiveFretParts = Object.create(null)'), l.trim());
    // drumTab's doc comment sits directly above its init line
    assert.match(SRC, /the fields are never synthesized\.\n\s*hwState\.drumTab = null;/);
    // the bundle exposes the live reference, defaulting to {}
    assert.match(SRC, /b\.fiveFretParts\s*=\s*hwState\.fiveFretParts\s*;/);
    assert.match(SRC, /getFiveFretParts\(\)\s*\{\s*return hwState\.fiveFretParts;\s*\}/);
});

test("a '__proto__' part id is stored as a plain key, never as the prototype", () => {
    const ctx = makeDispatcher();
    ctx.feed({ type: 'five_fret', part_id: '__proto__', name: 'Odd', instrument: 'guitar', total: 1 });
    ctx.feed({ type: 'five_fret_notes', part_id: '__proto__', notes: [{ t: 0.5, f: 1 }] });
    ctx.feed({ type: 'five_fret_notes', part_id: 'constructor', notes: [{ t: 0.5, f: 1 }] });
    ctx.feed({ type: 'five_fret_notes', part_id: 'toString', notes: [{ t: 0.5, f: 1 }] });
    const parts = ctx.hwState.fiveFretParts;
    assert.equal(Object.getPrototypeOf(parts), null);
    assert.deepEqual(Object.keys(parts), ['__proto__']);
    assert.ok(Object.prototype.hasOwnProperty.call(parts, '__proto__'));
    assert.equal(parts.__proto__.name, 'Odd');
    assert.equal(parts.__proto__.notes.length, 1);
    assert.equal(({}).name, undefined);   // Object.prototype untouched
});

test('drumTab still folds unchanged', () => {
    const ctx = makeDispatcher();
    ctx.feed({ type: 'drum_tab', version: 1, name: 'Drums', kit: [], total: 1 });
    ctx.feed({ type: 'drum_hits', data: [{ t: 0.5, p: 'kick' }], total: 1 });
    assert.equal(ctx.hwState.drumTab.hits.length, 1);
    assert.deepEqual(plain(ctx.hwState.fiveFretParts), {});
});

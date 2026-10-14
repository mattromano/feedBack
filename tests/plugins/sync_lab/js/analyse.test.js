'use strict';
// sync_lab: the calibration maths (ear vs eye -> A/V, eye -> input offset)
// and A/V auto-follow, from plugins/sync_lab/screen.js's pure helpers.

const { test } = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');

const sl = require(path.join(__dirname, '..', '..', '..', '..', 'plugins', 'sync_lab', 'screen.js'));

const PLAN = { beat: 0.6, sections: { listen: [4.4, 23.6], watch: [26, 45.2], play: [47.6, 62] } };
const fill = (n, v, jitter = 0) => Array.from({ length: n }, (_, i) => v + (i % 2 ? jitter : -jitter));

test('sectionOf: half a beat of slack, count-ins belong to no section', () => {
    assert.equal(sl.sectionOf(PLAN, 4.4), 'listen');
    assert.equal(sl.sectionOf(PLAN, 4.2), 'listen');
    assert.equal(sl.sectionOf(PLAN, 23.4), null);
    assert.equal(sl.sectionOf(PLAN, 26), 'watch');
    assert.equal(sl.sectionOf(PLAN, 70), null);
});

test('analyse: simulated drummer + guitarist recover A/V and each input offset', () => {
    // Model: input latency drums 20 / guitar 60, display 25, render-ahead R 48, current A/V 0.
    //   eye = display + input; ear = input + R + A/V.
    const players = {
        'p0-g': { key: 'p0-g', kind: 'guitar', label: 'P1 guitar', listen: fill(30, 60 + 48, 4), watch: fill(30, 25 + 60, 4), play: fill(20, 85) },
        'p1-d': { key: 'p1-d', kind: 'drums', label: 'P2 drums', listen: fill(30, 20 + 48, 3), watch: fill(30, 25 + 20, 3), play: fill(20, 45) },
    };
    const r = sl.analyse(players, { av: 0, guitarLat: 80, drumOffset: 0 });
    assert.equal(r.avNew, -23);                 // eye - ear = 25 - 48
    const g = r.rows.find((x) => x.kind === 'guitar'), d = r.rows.find((x) => x.kind === 'drums');
    assert.equal(g.input, 85); assert.equal(g.current, 80);
    assert.equal(d.input, 45); assert.equal(d.current, 0);
});

test('analyse: too few notes gives no suggestion; limits are applied', () => {
    const few = { g: { key: 'g', kind: 'guitar', label: 'Lead', listen: fill(3, 10), watch: fill(3, 10), play: [] } };
    const r = sl.analyse(few, { av: 12, guitarLat: 80, drumOffset: null });
    assert.ok(Number.isNaN(r.avNew));
    assert.ok(Number.isNaN(r.rows[0].input));
    const late = { g: { key: 'g', kind: 'guitar', label: 'Lead', listen: fill(10, 400), watch: fill(10, 300), play: [] } };
    const r2 = sl.analyse(late, { av: 0, guitarLat: 0, drumOffset: null });
    assert.equal(r2.rows[0].input, 250);
    assert.equal(r2.rows[0].wanted, 300);
});

test('followAv: shifts A/V by the output-latency change, ignores small changes', () => {
    const ref = { av: 24, aheadMs: 48 };
    assert.equal(sl.followAv(ref, 88, 24), -16);
    assert.equal(sl.followAv(ref, 52, 24), null);
    assert.equal(sl.followAv(ref, 88, -16), null, 'already there');
    assert.equal(sl.followAv(null, 88, 24), null);
});

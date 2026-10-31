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

test('targetFor / clampTarget: each input goes to its own setting, in its range and step', () => {
    assert.equal(sl.targetFor({ kind: 'guitar' }), 'guitarLat');
    assert.equal(sl.targetFor({ kind: 'drums' }), 'drumsKit');
    assert.equal(sl.targetFor({ kind: 'drums', controllerKey: 'pad#1' }), 'drumsPad:pad#1');
    assert.equal(sl.targetFor({ kind: 'five_fret', controllerKey: 'gh#1' }), 'fiveFret:gh#1');
    assert.equal(sl.targetFor({ kind: 'five_fret' }), null);
    assert.equal(sl.clampTarget('fiveFret:gh#1', 47), 45);
    assert.equal(sl.clampTarget('fiveFret:gh#1', 290), 200);
    assert.equal(sl.clampTarget('drumsPad:x', -300), -250);
    assert.equal(sl.clampTarget('guitarLat', -12), 0);
    assert.ok(Number.isNaN(sl.clampTarget('nope', 3)));
});

test('analyse: guitar + 5-Fret controller + drum controller recover A/V and each controller offset', () => {
    // display 25, R 48, A/V 0; inputs guitar 60, 5-Fret 33, drum pad 70.
    const players = {
        g: { key: 'g', kind: 'guitar', label: 'Lead', listen: fill(30, 60 + 48, 4), watch: fill(30, 25 + 60, 4), play: [] },
        f: { key: 'f', kind: 'five_fret', controllerKey: 'gh#1', label: 'Guitar Hero', listen: fill(30, 33 + 48, 3), watch: fill(30, 25 + 33, 3), play: [] },
        d: { key: 'd', kind: 'drums', controllerKey: 'rb#1', label: 'RB drums', listen: fill(30, 70 + 48, 3), watch: fill(30, 25 + 70, 3), play: [] },
    };
    const r = sl.analyse(players, { av: 0, offsets: { guitarLat: 80, 'fiveFret:gh#1': 0, 'drumsPad:rb#1': 20 } });
    assert.equal(r.avNew, -23);
    const by = Object.fromEntries(r.rows.map((x) => [x.target, x]));
    assert.equal(by.guitarLat.suggested, 85);
    assert.equal(by['fiveFret:gh#1'].suggested, 60);          // 58 -> step 5
    assert.equal(by['fiveFret:gh#1'].delta, 60);
    assert.equal(by['drumsPad:rb#1'].suggested, 95);
    assert.equal(by['drumsPad:rb#1'].current, 20);
    assert.equal(by['drumsPad:rb#1'].delta, 75);
    assert.equal(by.guitarLat.confidence.rating, 'great');
    assert.equal(r.avConfidence.rating, 'great');
});

test('confidence: plain-words ratings from hit count and spread', () => {
    assert.equal(sl.confidence({ n: 4, spread: 2 }).rating, 'unreliable');
    const great = sl.confidence({ n: 30, spread: 10 });
    assert.equal(great.rating, 'great');
    assert.equal(great.plusMinus, 3);
    assert.match(great.words, /±3 ms/);
    assert.equal(sl.confidence({ n: 10, spread: 40 }).rating, 'good');
    assert.equal(sl.confidence({ n: 30, spread: 60 }).rating, 'rough');
    assert.equal(sl.confidence({ n: 30, spread: 90 }).rating, 'unreliable');
});

test('coachState: step, instruction and beat count through the song', () => {
    assert.equal(sl.coachState(PLAN, 1).step, 'get-ready');
    const c1 = sl.coachState(PLAN, 4.4);
    assert.deepEqual([c1.step, c1.title, c1.beat, c1.beats], ['listen', 'LISTEN', 1, 32]);
    assert.equal(sl.coachState(PLAN, 4.4 + 0.6 * 9).beat, 10);
    assert.equal(sl.coachState(PLAN, 24.5).step, 'next-watch');
    assert.equal(sl.coachState(PLAN, 30).step, 'watch');
    assert.match(sl.coachState(PLAN, 30).hint, /No clicks/);
    assert.equal(sl.coachState(PLAN, 46).step, 'next-play');
    assert.equal(sl.coachState(PLAN, 50).step, 'play');
    assert.equal(sl.coachState(PLAN, 63).step, 'done');
});

test('warnings: a picked input with no hits 6 beats in, or mostly misses', () => {
    const expect = [{ kind: 'five_fret', controllerKey: 'gh#1', label: 'Guitar Hero' }, { kind: 'kit', label: 'Drum kit' }];
    const players = { d: { key: 'd', kind: 'drums', controllerKey: null, listen: fill(8, 10), watch: [], play: [], misses: 0 } };
    assert.deepEqual(sl.warnings(players, expect, PLAN, 4.4 + 0.6 * 3), [], 'too early to tell');
    const w = sl.warnings(players, expect, PLAN, 4.4 + 0.6 * 7);
    assert.equal(w.length, 1);
    assert.match(w[0].text, /No hits from Guitar Hero/);
    const missing = { f: { key: 'f', kind: 'five_fret', controllerKey: 'gh#1', listen: [1], watch: [], play: [], misses: 12 } };
    assert.match(sl.warnings(missing, expect.slice(0, 1), PLAN, 4.4 + 0.6 * 7)[0].text, /missing the notes/);
    assert.deepEqual(sl.warnings(players, null, PLAN, 10), [], 'no picks (song opened from the library): no warnings');
});

test('matchesExpect: the kit and drum controllers are told apart', () => {
    assert.ok(sl.matchesExpect({ kind: 'drums', controllerKey: null }, { kind: 'kit' }));
    assert.ok(!sl.matchesExpect({ kind: 'drums', controllerKey: 'rb#1' }, { kind: 'kit' }));
    assert.ok(sl.matchesExpect({ kind: 'drums', controllerKey: 'rb#1' }, { kind: 'drumpad', controllerKey: 'rb#1' }));
    assert.ok(!sl.matchesExpect({ kind: 'drums', controllerKey: 'rb#2' }, { kind: 'drumpad', controllerKey: 'rb#1' }));
    assert.ok(!sl.matchesExpect({ kind: 'guitar' }, { kind: 'five_fret' }));
});

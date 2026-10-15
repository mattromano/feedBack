'use strict';
// highway_tweaks: the pure pieces of screen.js (precise stems clock, frame
// time, judgment log rows) and its 3D highway source patch run against the
// in-tree plugins/highway_3d/screen.js, so an upstream change to the 3D
// highway that breaks an anchor fails here instead of silently in the app.

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const ROOT = path.join(__dirname, '..', '..', '..', '..');
const hwt = require(path.join(ROOT, 'plugins', 'highway_tweaks', 'screen.js'));
const H3D = fs.readFileSync(path.join(ROOT, 'plugins', 'highway_3d', 'screen.js'), 'utf8');

test('stems clock: extrapolates between audio callbacks and keeps the render-ahead offset', () => {
    const c = hwt.createStemsClock();
    // Context renders 40 ms ahead of the speakers; currentTime only stepped
    // at the last callback (ct = 10.000) but 6 ms have passed since.
    const ts = { contextTime: 9.960, performanceTime: 1000 };
    const r = c.read(10.000, ts, 1006, 5.000, 1);
    // est = 9.966, off = 0.034 -> precise = 5 + (9.966 + 0.034 - 10.000) = 5.000 on the first read
    assert.ok(Math.abs(r.t - 5.000) < 1e-9);
    assert.ok(Math.abs(r.renderAheadMs - 34) < 1e-6);
    // Next frame, 10 ms later, currentTime has NOT stepped: the clock still moves 10 ms.
    const r2 = c.read(10.000, ts, 1016, 5.000, 1);
    assert.ok(Math.abs(r2.t - 5.010) < 0.001, 'advanced ' + (r2.t - 5) * 1000 + ' ms');
});

test('stems clock: scales the correction by the playback rate and rejects bad timestamps', () => {
    const c = hwt.createStemsClock();
    c.read(10, { contextTime: 9.96, performanceTime: 1000 }, 1000, 5, 0.5);
    const r = c.read(10, { contextTime: 9.96, performanceTime: 1000 }, 1010, 5, 0.5);
    assert.ok(Math.abs(r.t - 5.005) < 0.001);
    assert.equal(c.read(10, null, 1000, 5, 1), null);
    assert.equal(c.read(10, { contextTime: 0, performanceTime: 0 }, 1000, 5, 1), null);
});

test('frameTimeFor: adds the highway render offset, refuses implausible jumps', () => {
    assert.ok(Math.abs(hwt.frameTimeFor(12.030, 0.030, 12.004) - 12.034) < 1e-9);
    assert.ok(Number.isNaN(hwt.frameTimeFor(12.030, 0.030, 13)));
    assert.ok(Number.isNaN(hwt.frameTimeFor(12.030, NaN, 12)));
});

test('renderOffsetOf: prefers the core bundle renderOffset over currentTime - last setTime', () => {
    // Core's frame clock (#974) moved currentTime 9 ms past the last 60 Hz
    // sample; the real offset is still 30 ms.
    assert.equal(hwt.renderOffsetOf({ currentTime: 12.039, renderOffset: 0.030 }, 12.000), 0.030);
    // Older core: no renderOffset, fall back to the setTime stamp.
    assert.ok(Math.abs(hwt.renderOffsetOf({ currentTime: 12.030 }, 12.000) - 0.030) < 1e-9);
    assert.ok(Number.isNaN(hwt.renderOffsetOf({ currentTime: 12.030 }, undefined)));
    assert.ok(Number.isNaN(hwt.renderOffsetOf(null, 12)));
});

test('composeClockSource: stems clock first, core source second, a null hold always wins', () => {
    let precise = NaN;
    let base = 5;
    const fn = hwt.composeClockSource(() => precise, () => base);
    assert.equal(fn(), 5);               // no stems clock: core's source
    precise = 7.25;
    assert.equal(fn(), 7.25);            // stems clock running: it wins
    base = null;
    assert.equal(fn(), null);            // count-in: host drives time
    base = NaN;
    assert.ok(Number.isNaN(hwt.composeClockSource(() => NaN, null)()));
    assert.equal(fn.__hwtClock, true);
});

test('frameStats: counts frames over 1.5x the median as dropped', () => {
    const s = hwt.frameStats([16, 16, 17, 16, 40, 16], 121);
    assert.equal(s.p50, 16);
    assert.equal(s.droppedPct, 16.7);
});

test('judgmentRow: compact row with per-string verdicts', () => {
    const row = hwt.judgmentRow({ noteTime: 1.23456, chord: true, hit: false, timingState: 'LATE', timingError: 61,
        hitStrings: 2, totalStrings: 3, score: 0.666, pitchError: 12.3, section: 'verse',
        strings: [{ s: 0, f: 3, hit: true, centsError: -4, snr: 12.34 }, { s: 1, f: 5, hit: false, centsError: null }] });
    assert.deepEqual(row, { t: 1.235, c: 1, h: 0, ts: 'LATE', te: 61, hs: 2, tt: 3, sc: 0.67, pe: 12, sec: 'verse',
        v: [{ s: 0, f: 3, d: 1, ce: -4, snr: 12.3 }, { s: 1, f: 5, d: 0, ce: null, snr: null }] });
});

test('3D highway patch: every anchor matches the in-tree highway_3d and the result parses', () => {
    const hits = [];
    const out = hwt.patchHighway3d(H3D, hits, {});
    assert.deepEqual(hits, ['frame-clock', 'rb-verdicts']);
    assert.doesNotThrow(() => new vm.Script(out, { filename: 'highway_3d.patched.js' }));
    assert.match(out, /window\.__hwtFrameTime\(bundle\)/);
    assert.match(out, /__rbV\.set\(__rbK, __rbE\)/);
});

test('3D highway patch: kill switches leave the source alone', () => {
    const hits = [];
    assert.equal(hwt.patchHighway3d(H3D, hits, { noRb: true, noFrameClock: true }), H3D);
    assert.deepEqual(hits, []);
});

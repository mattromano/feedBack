'use strict';
// guitar_volume: the setGain rewriter (applied to Audio Engine / Rig Builder
// as they load) and the master-level state.

const { test } = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const vm = require('node:vm');

const gv = require(path.join(__dirname, '..', '..', '..', '..', 'plugins', 'guitar_volume', 'screen.js'));

function run(code, gain) {
    const calls = [];
    const api = { setGain(k, v) { calls.push([k, v]); return Promise.resolve(); } };
    const window = gain ? { __gvGain: gain } : {};
    vm.runInNewContext(code, { api, window, dbToLinearGain: (db) => Math.pow(10, db / 20) });
    return calls;
}

test('rewrites every setGain call, keeps arguments with nested calls and strings intact', () => {
    const src = "api.setGain('chain', dbToLinearGain(6)); api.setGain('input', 0.5); const k = 'chain'; api.setGain(k, Math.min(2, (1)));";
    const r = gv.rewriteSetGain(src);
    assert.equal(r.count, 3);
    const doubled = run(r.code, (k, v) => (k === 'chain' ? v * 2 : v));
    assert.equal(doubled.length, 3);
    assert.ok(Math.abs(doubled[0][1] - 2 * Math.pow(10, 6 / 20)) < 1e-9);
    assert.deepEqual(doubled.slice(1), [['input', 0.5], ['chain', 2]]);
    // Without the plugin's global the rewritten code behaves exactly like the original.
    assert.deepEqual(run(r.code, null), run(src, null));
});

test('leaves calls it cannot read alone', () => {
    for (const src of ['api.setGain();', 'api.setGain(a, b, c);', "const s = 'x.setGain(';"]) {
        assert.equal(gv.rewriteSetGain(src).count, 0, src);
    }
});

test('master: chain levels scale by the master, a change re-applies the last preset level', () => {
    const applied = [];
    const m = gv.createMaster(-6, (v) => applied.push(v));
    assert.equal(m.gain('input', 0.7), 0.7);
    const out = m.gain('chain', 0.8);
    assert.ok(Math.abs(out - 0.8 * Math.pow(10, -6 / 20)) < 1e-9);
    m.setDb(0);
    assert.deepEqual(applied, [0.8]);
    assert.equal(m.setDb(99), gv.MAX);
    assert.equal(gv.clampDb('junk'), 0);
});

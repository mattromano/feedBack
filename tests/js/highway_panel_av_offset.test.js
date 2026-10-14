// The A/V offset is one per-machine calibration (picture vs sound), so EVERY
// highway must draw with it — not only the main `window.highway`.
//
// Bug: setAvOffsetMs() only called window.highway.setAvOffset(). Highways a
// plugin builds with window.createHighway() (splitscreen panels, one per
// player) stayed at 0 ms, so in split view the gems — and Note Detection,
// which judges on the highway's render clock — ran on a different clock than
// in single player (measured: drum panel judged −29.6 ms at A/V +30).
//
// Fix: static/js/highway-av-offset.js keeps the shared value and every live
// highway; createHighway() registers each instance (seeded with the current
// value) and setAvOffsetMs() sets them all.

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const ROOT = path.join(__dirname, '..', '..');
const AV_JS = path.join(ROOT, 'static', 'js', 'highway-av-offset.js');

// Evaluate the (dependency-free) ES module in a vm sandbox: strip `export`
// and hand the exported functions back through a global.
function loadModule() {
    const src = fs.readFileSync(AV_JS, 'utf8').replace(/^export /gm, '');
    const sandbox = { WeakRef, Set, Number, out: null };
    vm.runInNewContext(src + '\nout = { registerHighwayAvOffset, setAllHighwaysAvOffset, sharedAvOffsetMs };', sandbox);
    return sandbox.out;
}

function fakeHighway() {
    let ms = 0;
    return { setAvOffset(v) { ms = Number(v) || 0; }, getAvOffset() { return ms; } };
}

test('a highway created after the offset was set starts with it', () => {
    const m = loadModule();
    m.setAllHighwaysAvOffset(30);
    const panel = fakeHighway();
    m.registerHighwayAvOffset(panel);
    assert.equal(panel.getAvOffset(), 30);
});

test('changing the offset reaches the main highway and every panel', () => {
    const m = loadModule();
    const main = fakeHighway(), p1 = fakeHighway(), p2 = fakeHighway();
    for (const h of [main, p1, p2]) m.registerHighwayAvOffset(h);
    m.setAllHighwaysAvOffset(-13);
    assert.deepEqual([main, p1, p2].map((h) => h.getAvOffset()), [-13, -13, -13]);
    assert.equal(m.sharedAvOffsetMs(), -13);
});

test('a highway whose setAvOffset throws does not stop the others', () => {
    const m = loadModule();
    const bad = { setAvOffset() { throw new Error('torn down'); } };
    const ok = fakeHighway();
    m.registerHighwayAvOffset(bad);
    m.registerHighwayAvOffset(ok);
    m.setAllHighwaysAvOffset(24);
    assert.equal(ok.getAvOffset(), 24);
});

test('createHighway registers every instance and setAvOffsetMs sets them all', () => {
    const highway = fs.readFileSync(path.join(ROOT, 'static', 'highway.js'), 'utf8');
    assert.match(highway, /import\s*\{[^}]*registerHighwayAvOffset[^}]*\}\s*from\s*'\.\/js\/highway-av-offset\.js'/);
    assert.match(highway, /registerHighwayAvOffset\(api\)/, 'createHighway must register the instance it returns');
    const settings = fs.readFileSync(path.join(ROOT, 'static', 'js', 'settings.js'), 'utf8');
    assert.match(settings, /setAllHighwaysAvOffset\(_avOffsetMs\)/, 'setAvOffsetMs must set every highway, not only window.highway');
});

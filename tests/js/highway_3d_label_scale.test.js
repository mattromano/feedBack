// The 3D highway honours the highway's fret-number label scale
// (highway.setLabelScale, 0.75–2.5) carried on bundle.labelScale.
//
// Design: fret-number sprites are scaled in world space (sprite.scale), NOT by
// re-rasterising their text textures. txtMat()'s cache key therefore stays
// (style|wide|text|color) — stepping the scale through 0.75…2.5 adds no
// texture entries, so there is nothing to leak or rebuild, and a change shows
// up on the very next frame. At scale 1 every multiplier is ×1 → unchanged.

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { loadAudioSession, runBrowserScript } = require('./audio_session_test_harness');

const SCREEN_JS = path.join(__dirname, '..', '..', 'plugins', 'highway_3d', 'screen.js');
const src = fs.readFileSync(SCREEN_JS, 'utf8');

function loadScreen() {
    const window = loadAudioSession();
    window.document.getElementById = () => null;
    window.document.createElement = () => ({ getContext: () => null, style: {}, addEventListener() {}, setAttribute() {} });
    window.document.addEventListener = () => {};
    window.document.removeEventListener = () => {};
    window.Image = class Image {};
    runBrowserScript(window, 'plugins/highway_3d/screen.js');
    return window.feedBackViz_highway_3d.__test;
}

test('resolveLabelScale: defaults to 1, passes valid values, clamps junk', () => {
    const { resolveLabelScale } = loadScreen();
    assert.equal(typeof resolveLabelScale, 'function');
    assert.equal(resolveLabelScale(undefined), 1);
    assert.equal(resolveLabelScale({}), 1, 'older core without bundle.labelScale');
    assert.equal(resolveLabelScale({ labelScale: NaN }), 1);
    assert.equal(resolveLabelScale({ labelScale: '2' }), 1, 'non-number ignored');
    assert.equal(resolveLabelScale({ labelScale: 1 }), 1);
    assert.equal(resolveLabelScale({ labelScale: 1.75 }), 1.75);
    assert.equal(resolveLabelScale({ labelScale: 9 }), 2.5);
    assert.equal(resolveLabelScale({ labelScale: 0.1 }), 0.75);
});

test('update() refreshes the per-instance multiplier from the bundle every frame', () => {
    assert.match(src, /let\s+_labelScaleMul\s*=\s*1(\.0)?\s*;/);
    assert.match(src, /_labelScaleMul\s*=\s*resolveLabelScale\(\s*bundle\s*\)\s*;/);
});

// Each fret-number sprite site multiplies its scale by _labelScaleMul.
const SCALED_SITES = [
    ['fret row', /const\s+scale\s*=\s*5\.95\s*\*\s*_textSizeMul\s*\*\s*_labelScaleMul\s*\*\s*fretLabelScaleForFret\(f\)/],
    ['chord fret numbers', /const\s+_flS\s*=\s*7\.0\s*\*\s*K\s*\*\s*\(1 \+ 0\.4 \* chDt \/ AHEAD\)\s*\*\s*_textSizeMul\s*\*\s*_labelScaleMul\s*\*/],
    ['fret column markers', /const\s+sz\s*=\s*7\.0\s*\*\s*K\s*\*\s*\(1 \+ 0\.4 \* dt \/ AHEAD\)\s*\*\s*_textSizeMul\s*\*\s*_labelScaleMul\s*\*/],
    ['note fret label', /const\s+flS\s*=\s*7\.0\s*\*\s*K\s*\*[^;]*_textSizeMul\s*\*\s*_labelScaleMul\s*\*\s*fretLabelScaleForFret\(n\.f\)/],
    ['synthetic chord-note fret label', /const\s+_flS2\s*=\s*7\.0\s*\*\s*K\s*\*[^;]*_textSizeMul\s*\*\s*_labelScaleMul\s*\*/],
    ['board-ghost fret digit', /const\s+ghostLblS\s*=\s*0\.7\s*\*\s*ghostOuterL\s*\*\s*_textSizeMul\s*\*\s*_labelScaleMul\s*\*/],
    ['inlay labels (live rescale)', /const\s+s\s*=\s*5\.5\s*\*\s*_inlayMul\s*\*\s*K\s*\*\s*fretLabelScaleForFret\(f\)/],
    ['inlay labels (build)', /const\s+scale\s*=\s*5\.5\s*\*\s*\(0\.5 \+ textSize\)\s*\*\s*_labelScaleMul\s*\*/],
];
for (const [name, re] of SCALED_SITES) {
    test(`fret-number site scaled: ${name}`, () => assert.match(src, re));
}

test('inlay rescale guard keys on text size × label scale so a scale change re-applies', () => {
    assert.match(src, /const\s+_inlayMul\s*=\s*_textSizeMul\s*\*\s*_labelScaleMul\s*;/);
    assert.match(src, /if\s*\(\s*_inlayMul\s*!==\s*_textSizeMulApplied\s*\)/);
});

test('teaching marks keep their size but move out with the bigger fret label', () => {
    assert.match(src, /const\s+_tmS\s*=\s*5\.0\s*\*\s*K\s*\*\s*_textSizeMul\s*\*\s*fretLabelScaleForFret\(n\.f\)\s*;/);
    assert.match(src, /NW\s*\*\s*0\.95\s*\*\s*_labelScaleMul/);
});

test('non-fret text is untouched: section banners and chord names do not use the label scale', () => {
    assert.match(src, /sp\.scale\.set\(20 \* K \* _textSizeMul, 5 \* K \* _textSizeMul, 1\);/);
    assert.match(src, /const\s+lblWS\s*=\s*lblW\s*\*\s*_textSizeMul\s*;/);
    assert.match(src, /const\s+scalePx\s*=\s*2\.42\s*\*\s*_textSizeMul\s*\*\s*K\s*;/, 'nut tuning labels');
});

test('txtMat cache key is scale-free (no texture per scale step, nothing to leak)', () => {
    assert.match(src, /const\s+k\s*=\s*sName\s*\+\s*'\|'\s*\+\s*\(wide \? 'W' : ''\)\s*\+\s*text\s*\+\s*'\|'\s*\+\s*col\s*;/);
    const body = src.slice(src.indexOf('function txtMat('), src.indexOf('function pinchHarmonicMat('));
    assert.doesNotMatch(body, /_labelScaleMul|labelScale/);
});

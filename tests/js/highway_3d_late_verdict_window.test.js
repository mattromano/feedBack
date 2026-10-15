// Pins the note-detect verdict windows in plugins/highway_3d/screen.js to the
// SUSTAIN END of a note / chord, not its start.
//
// The desktop engine verifier judges a sustained note or chord once its
// sustain is over, so the verdict lands ~0.35-0.5 s after n.t + sus. The
// windows used to count from n.t: drawNote culled the gem at
// n.t + NOTEDETECT_GEM_VERDICT_WINDOW (0.75 s), the chord frame's hold was
// floored at 0.75 s and the 'unmatched' latch fired at 0.55 s past ch.t, so a
// note sustained for more than ~0.4 s was gone before its verdict arrived and
// about one hit in ten counted without a flash.
//
// Source-level, same strategy as the other highway_3d tests, plus a timeline
// check of the drawNote predicate itself.

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const SCREEN_JS = path.join(__dirname, '..', '..', 'plugins', 'highway_3d', 'screen.js');
const src = fs.readFileSync(SCREEN_JS, 'utf8');

test('drawNote keeps a gem for its verdict until the window past its sustain end', () => {
    assert.match(src, /if \(!_ndHasProvider \|\| susEnd - now < -NOTEDETECT_GEM_VERDICT_WINDOW\) return;/);
    assert.doesNotMatch(src, /dt < -NOTEDETECT_GEM_VERDICT_WINDOW\) return;/,
        'the start-relative cull culled sustained gems before their verdict');
    // susEnd is n.t + sus, so a note without a sustain keeps the old window.
    assert.match(src, /const susEnd = n\.t \+ \(n\.sus \|\| 0\);/);
});

test('timeline: a late verdict after a sustain finds its gem still drawable', () => {
    const W = Number(src.match(/const NOTEDETECT_GEM_VERDICT_WINDOW = ([\d.]+);/)[1]);
    const keptAt = (n, now) => !((n.t + (n.sus || 0)) - now < -W);   // the drawNote predicate
    const oldKeptAt = (n, now) => !((n.t - now) < -W);
    const VERDICT_LAG = 0.45;   // engine verifier, measured 0.35-0.5 s after the sustain
    let oldLost = 0;
    for (const sus of [0, 0.2, 0.4, 0.6, 1.0, 2.5]) {
        const n = { t: 10, s: 2, f: 5, sus };
        const verdictAt = n.t + sus + VERDICT_LAG;
        assert.ok(keptAt(n, verdictAt), `sus ${sus}: gem culled before its verdict at ${verdictAt}`);
        if (!oldKeptAt(n, verdictAt)) oldLost++;
    }
    assert.ok(oldLost > 0, 'the old window must have lost some (sanity check of the model)');
});

test('chord frame hold and unmatched latch count from the longest member sustain', () => {
    assert.match(src, /if \(_ndHasProvider && chordTailHoldS < maxSus \+ NOTEDETECT_GEM_VERDICT_WINDOW\) \{\s*chordTailHoldS = maxSus \+ NOTEDETECT_GEM_VERDICT_WINDOW;/);
    assert.match(src, /\} else if \(chDt \+ maxSus < -_ND_UNMATCHED_LATCH_AFTER && !anyState\) \{/);
    // The latch must still fire inside the frame's visible life.
    const W = Number(src.match(/const NOTEDETECT_GEM_VERDICT_WINDOW = ([\d.]+);/)[1]);
    const L = Number(src.match(/const _ND_UNMATCHED_LATCH_AFTER = ([\d.]+);/)[1]);
    assert.ok(L < W);
});

test('chord verdict latches outlive a sustained chord (pruner keys on chord start)', () => {
    assert.match(src, /const pruneBefore = ndVerdictT0 - 0\.5 - _chordVerdictsMaxSus;/);
    assert.match(src, /if \(maxSus > _chordVerdictsMaxSus\) _chordVerdictsMaxSus = Math\.min\(maxSus, 30\);/);
    // Outer chord-loop bound already counts the sustain.
    assert.match(src, /if \(ch\.t \+ _chFilterSus < ndVerdictT0\) continue;/);
    // Outer note-loop bound too.
    assert.match(src, /if \(!_inArpPersist && n\.t \+ \(n\.sus \|\| 0\) < ndVerdictT0\) continue;/);
});

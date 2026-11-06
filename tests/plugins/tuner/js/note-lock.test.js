// plugins/tuner/utils/audio.js note lock: a decaying string keeps its reading.
//
// The tuner used one absolute level (-40 dBFS RMS) to start AND end a reading,
// and a single quiet/unclear 30 ms frame blanked it, so on a low-gain USB guitar
// cable the reading died ~0.3-0.8 s into a still-ringing note. These drive the
// frame handler with synthetic worker results (freq / confidence / rms) at the
// 30 ms poll rate.
'use strict';
const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

function makeTuner() {
    const src = fs.readFileSync(path.join(__dirname, '..', '..', '..', '..', 'plugins', 'tuner', 'utils', 'audio.js'), 'utf8');
    const win = {};
    // eslint-disable-next-line no-new-func
    new Function('window', src)(win);
    const t = win._tunerAudio;
    t._reset();
    const out = [];
    const realNow = Date.now;
    let clock = 1000;
    const feed = (freq, confidence, rms) => {
        Date.now = () => clock;
        try { t._feed({ freq, confidence, rms }, (r) => out.push(r)); } finally { Date.now = realNow; }
        clock += 30;
        return out[out.length - 1];
    };
    return { feed, out };
}

const DB = (db) => Math.pow(10, db / 20);

test('a note decaying 30 dB over 2 s stays locked the whole time', () => {
    const { feed } = makeTuner();
    // pluck at -24 dBFS RMS, clean E2, then an exponential decay to -54 dBFS
    let shown = 0, frames = 0;
    for (let i = 0; i < 67; i++) {               // 67 x 30 ms ≈ 2 s
        const rms = DB(-24 - (30 * i) / 66);
        const conf = i < 40 ? 0.9 : 0.55;        // clarity also drops on the tail
        const r = feed(82.4, conf, rms);
        frames++;
        if (r.smoothedFreq) shown++;
    }
    // only the 2 warm-up frames may be blank
    assert.ok(shown >= frames - 2, `reading dropped out: shown ${shown}/${frames}`);
});

test('old behaviour regression: dropping below -40 dBFS no longer blanks', () => {
    const { feed } = makeTuner();
    for (let i = 0; i < 5; i++) feed(110, 0.9, DB(-30));
    const r = feed(110, 0.6, DB(-45));
    assert.ok(r.smoothedFreq > 100 && r.hasSignal, 'still showing A2 at -45 dBFS');
});

test('a few bad frames are bridged, then the last reading is held, then cleared', () => {
    const { feed } = makeTuner();
    for (let i = 0; i < 5; i++) feed(196, 0.9, DB(-28));
    // the string is muted: silence frames
    const during = [];
    for (let i = 0; i < 25; i++) during.push(feed(0, 0, DB(-80)));   // 750 ms
    assert.ok(during[0].smoothedFreq > 190, 'first silent frame still shows the note');
    assert.ok(during[10].smoothedFreq > 190, 'held ~300 ms in');
    assert.strictEqual(during[24].smoothedFreq, null, 'cleared after miss frames + 600 ms hold');
    assert.strictEqual(during[24].hasSignal, false);
});

test('quiet noise never acquires a reading', () => {
    const { feed } = makeTuner();
    for (let i = 0; i < 20; i++) {
        const r = feed(60 + i, 0.3, DB(-50));
        assert.strictEqual(r.smoothedFreq, null);
    }
});

test('a new string re-locks straight to the new note without a blank', () => {
    const { feed } = makeTuner();
    for (let i = 0; i < 6; i++) feed(82.4, 0.9, DB(-26));
    const r = feed(110, 0.9, DB(-26));          // next string plucked
    assert.ok(Math.abs(r.smoothedFreq - 110) < 1, 'jumps to A2');
    const r2 = feed(110, 0.9, DB(-27));
    assert.ok(Math.abs(r2.smoothedFreq - 110) < 1);
});

test('an octave error on a decaying tail is folded back, not treated as a new note', () => {
    const { feed } = makeTuner();
    for (let i = 0; i < 6; i++) feed(82.4, 0.9, DB(-30));
    const r = feed(164.8, 0.4, DB(-48));         // YIN octave slip at low level
    assert.ok(Math.abs(r.smoothedFreq - 82.4) < 1, 'stays on E2');
});

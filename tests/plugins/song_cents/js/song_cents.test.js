// song_cents (mattromano build): the estimator finds a recording's offset, and screen.js keeps the
// interface Live Retune's "Match song" reads: window.__songCents.current + 'songcents:change'.
'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const DIR = path.join(__dirname, '..', '..', '..', '..', 'plugins', 'song_cents');
const { estimateCents } = require(path.join(DIR, 'assets', 'estimate.js'));

function tone(centsOff, sr = 44100, secs = 6) {
    // A few notes of a guitar-ish harmonic stack, all `centsOff` sharp of 12-TET.
    const out = new Float32Array(sr * secs);
    const notes = [82.41, 110, 146.83, 196, 246.94];
    for (let i = 0; i < out.length; i++) {
        const n = notes[Math.floor(i / sr) % notes.length] * Math.pow(2, centsOff / 1200);
        const t = i / sr;
        out[i] = 0.5 * Math.sin(2 * Math.PI * n * t) + 0.25 * Math.sin(4 * Math.PI * n * t)
            + 0.12 * Math.sin(6 * Math.PI * n * t);
    }
    return out;
}

test('estimator recovers a sharp recording', () => {
    for (const c of [0, 25, 45, -30]) {
        const r = estimateCents(tone(c), 44100);
        assert.ok(Math.abs(r.cents - c) < 3, `${c}c measured ${r.cents}`);
        assert.ok(r.confidence > 0.5);
    }
});

test('screen.js publishes window.__songCents.current and songcents:change', async () => {
    const handlers = {};
    const events = [];
    const window = {
        feedBack: { on: (ev, fn) => { handlers[ev] = fn; } },
        dispatchEvent: (e) => events.push(e),
    };
    const ctx = {
        window,
        document: { getElementById: () => null, createElement: () => ({}), head: { append() {} } },
        CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init && init.detail; } },
        fetch: async (url) => ({
            ok: true,
            json: async () => (String(url).includes('/known') ? {} : { file: 'a.sloppak', cents: 45, source: 'chart' }),
        }),
        setTimeout: (fn) => 0,
        console,
        encodeURIComponent,
    };
    vm.runInNewContext(fs.readFileSync(path.join(DIR, 'screen.js'), 'utf8'), ctx);
    assert.equal(typeof window.__songCents.scanLibrary, 'function');
    assert.equal(typeof handlers['song:loaded'], 'function');
    await handlers['song:loaded']({ detail: { filename: 'a.sloppak' } });
    assert.deepEqual({ ...window.__songCents.current }, { file: 'a.sloppak', cents: 45, source: 'chart', confidence: undefined });
    assert.deepEqual(events.map((e) => [e.type, e.detail && e.detail.source]),
        [['songcents:change', 'pending'], ['songcents:change', 'chart']]);
});

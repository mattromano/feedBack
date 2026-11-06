'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const { _yinDetect } = require('../../../../plugins/tuner/workers/yin.js');

const SAMPLE_RATE = 44100;
const FRAME = 4096;

function sine(freq, nSamples, amplitude = 0.8) {
    const buf = new Float32Array(nSamples);
    for (let i = 0; i < nSamples; i++) {
        buf[i] = amplitude * Math.sin(2 * Math.PI * freq * i / SAMPLE_RATE);
    }
    return buf;
}

function assertFreq(result, expected, tolerancePct = 1.5, label = '') {
    assert.ok(result.freq > 0, `${label}: expected a detected frequency, got 0`);
    const errorPct = Math.abs(result.freq - expected) / expected * 100;
    assert.ok(
        errorPct < tolerancePct,
        `${label}: expected ${expected} Hz ±${tolerancePct}%, got ${result.freq.toFixed(2)} Hz (error ${errorPct.toFixed(2)}%)`
    );
}

// ── Silence / no-signal ──────────────────────────────────────────────────────

test('silence (all zeros) returns freq 0', () => {
    const result = _yinDetect(new Float32Array(FRAME), SAMPLE_RATE);
    assert.equal(result.freq, 0);
    assert.equal(result.confidence, 0);
});

test('sub-threshold amplitude returns freq 0', () => {
    // rms below the worker noise floor (0.0008, -62 dBFS) is rejected before any pitch estimation
    const buf = sine(440, FRAME, 0.001);
    const result = _yinDetect(buf, SAMPLE_RATE);
    assert.equal(result.freq, 0);
});

// ── Clean sine pitches — guitar range ────────────────────────────────────────

test('detects E2 (82.41 Hz) — low E string', () => {
    assertFreq(_yinDetect(sine(82.41, FRAME), SAMPLE_RATE), 82.41, 1.5, 'E2');
});

test('detects A2 (110 Hz) — A string', () => {
    assertFreq(_yinDetect(sine(110, FRAME), SAMPLE_RATE), 110, 1.5, 'A2');
});

test('detects D3 (146.83 Hz) — D string', () => {
    assertFreq(_yinDetect(sine(146.83, FRAME), SAMPLE_RATE), 146.83, 1.5, 'D3');
});

test('detects G3 (196 Hz) — G string', () => {
    assertFreq(_yinDetect(sine(196, FRAME), SAMPLE_RATE), 196, 1.5, 'G3');
});

test('detects B3 (246.94 Hz) — B string', () => {
    assertFreq(_yinDetect(sine(246.94, FRAME), SAMPLE_RATE), 246.94, 1.5, 'B3');
});

test('detects E4 (329.63 Hz) — high E string', () => {
    assertFreq(_yinDetect(sine(329.63, FRAME), SAMPLE_RATE), 329.63, 1.5, 'E4');
});

test('detects A4 (440 Hz) — concert A', () => {
    assertFreq(_yinDetect(sine(440, FRAME), SAMPLE_RATE), 440, 1.5, 'A4');
});

// ── Confidence ────────────────────────────────────────────────────────────────

test('high confidence for clean sine at A4', () => {
    const result = _yinDetect(sine(440, FRAME), SAMPLE_RATE);
    assert.ok(result.confidence > 0.8, `Expected confidence > 0.8, got ${result.confidence.toFixed(3)}`);
});

test('rms reflects signal level', () => {
    const quiet = _yinDetect(sine(440, FRAME, 0.1), SAMPLE_RATE);
    const loud  = _yinDetect(sine(440, FRAME, 0.9), SAMPLE_RATE);
    assert.ok(loud.rms > quiet.rms, 'louder signal should have higher rms');
});

// ── Edge cases ────────────────────────────────────────────────────────────────

test('minimum buffer size (4096 samples) does not throw', () => {
    assert.doesNotThrow(() => _yinDetect(sine(440, 4096), SAMPLE_RATE));
});

test('returns object with freq, confidence, rms keys', () => {
    const result = _yinDetect(sine(440, FRAME), SAMPLE_RATE);
    assert.ok('freq' in result, 'missing freq');
    assert.ok('confidence' in result, 'missing confidence');
    assert.ok('rms' in result, 'missing rms');
});

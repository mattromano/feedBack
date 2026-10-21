'use strict';

const { test, before, beforeEach } = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const { pathToFileURL } = require('node:url');

let dt;

before(async () => {
    dt = await import(pathToFileURL(path.join(__dirname, '..', '..', 'static', 'js', 'display-time.js')).href);
});

beforeEach(() => dt.clearSweepTime());

test('returns audio time when no sweep is set', () => {
    assert.equal(dt.getDisplayTime(() => 42.5), 42.5);
});

test('returns the sweep time while sweeping', () => {
    dt.setSweepTime(10.25);
    assert.equal(dt.getDisplayTime(() => 99), 10.25);
});

test('clearSweepTime returns to audio time', () => {
    dt.setSweepTime(10);
    dt.clearSweepTime();
    assert.equal(dt.getDisplayTime(() => 7), 7);
});

test('ignores non-finite sweep values', () => {
    dt.setSweepTime(NaN);
    assert.equal(dt.getDisplayTime(() => 5), 5);
    dt.setSweepTime(Infinity);
    assert.equal(dt.getDisplayTime(() => 5), 5);
});

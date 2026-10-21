// mattromano build (0.2.9 play_queue's count-in extras): the song-start count-in
// honours "only when needed" (first note within 3 s) and never runs when a queue
// moves on to its next song. static/js/player-controls.js _wantSongCountIn.
'use strict';

const { test, before, beforeEach } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { pathToFileURL } = require('node:url');

const store = new Map();
let pc;
let notes = [];
let chords = [];
let continuation = false;

before(async () => {
    global.window = global;
    global.document = { getElementById: () => null };
    global.localStorage = {
        getItem: (k) => (store.has(k) ? store.get(k) : null),
        setItem: (k, v) => store.set(k, String(v)),
        removeItem: (k) => store.delete(k),
    };
    window.highway = { getNotes: () => notes, getChords: () => chords };
    window.feedBack = { playQueue: { isContinuation: () => continuation } };
    pc = await import(pathToFileURL(path.join(__dirname, '..', '..', 'static', 'js', 'player-controls.js')).href);
});

beforeEach(() => {
    store.clear();
    pc.setSongCountInOverride(null);
    store.set('countdownBeforeSong', '1');
    notes = [];
    chords = [];
    continuation = false;
});

test('off when the Countdown setting is off', () => {
    store.set('countdownBeforeSong', '0');
    notes = [{ t: 0.5 }];
    assert.equal(pc._wantSongCountIn(), false);
});

test('Auto (the default): counts in only when the first note comes within 3 s', () => {
    notes = [{ t: 0.8 }];
    assert.equal(pc._wantSongCountIn(), true);
    notes = [{ t: 12.0 }];
    chords = [{ t: 9.0 }];
    assert.equal(pc._wantSongCountIn(), false);
    chords = [{ t: 2.5 }];
    assert.equal(pc._wantSongCountIn(), true);
});

test('Auto off: always counts in', () => {
    store.set('countdownBeforeSongAuto', '0');
    notes = [{ t: 30 }];
    assert.equal(pc._wantSongCountIn(), true);
});

test('a part with no fretted notes (drums, keys) still counts in', () => {
    assert.equal(pc._firstChartNoteTime(), Infinity);
    assert.equal(pc._wantSongCountIn(), true);
});

test('never when the queue moves on to its next song', () => {
    notes = [{ t: 0.2 }];
    continuation = true;
    assert.equal(pc._wantSongCountIn(), false);
    store.set('countdownBeforeSongAuto', '0');
    assert.equal(pc._wantSongCountIn(), false);
});

test('the song-start autoplay path asks _wantSongCountIn', () => {
    const src = fs.readFileSync(path.join(__dirname, '..', '..', 'static', 'js', 'session.js'), 'utf8');
    assert.match(src, /if \(_wantSongCountIn\(\)\) \{\s*Promise\.resolve\(startSongCountIn\(\)\)/);
    assert.doesNotMatch(src, /_countdownBeforeSongEnabled\(\)/);
});

test('override true counts in even with the setting off', () => {
    store.set('countdownBeforeSong', '0');
    assert.equal(pc.setSongCountInOverride(true), true);
    assert.equal(pc._wantSongCountIn(), true);
});

test('override false never counts in', () => {
    store.set('countdownBeforeSong', '1');
    notes = [{ t: 0.5 }];
    assert.equal(pc.setSongCountInOverride(false), true);
    assert.equal(pc._wantSongCountIn(), false);
});

test('null clears the override; junk is refused', () => {
    store.set('countdownBeforeSong', '0');
    pc.setSongCountInOverride(true);
    assert.equal(pc.setSongCountInOverride('yes'), false);
    assert.equal(pc._wantSongCountIn(), true, 'junk changed nothing');
    assert.equal(pc.setSongCountInOverride(null), true);
    assert.equal(pc._wantSongCountIn(), false, 'plain setting rules again');
});

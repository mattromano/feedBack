// Build-a-Band: a secondary highway (the drum lane) streams its own song
// over its own WebSocket but must not rebroadcast global song state —
// window.feedBack.currentSong, song:loaded, beats:loaded, song:ready all
// belong to the main player. connect(url, { secondary: true }) opts out.
// Drives the real highway.js message handler with a fake WebSocket.
'use strict';

const { test, before } = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const { pathToFileURL } = require('node:url');

let hwMod;
let sockets = [];
let events = [];

before(async () => {
    global.window = global;
    global.addEventListener = () => {};
    global.removeEventListener = () => {};
    const el = () => ({ style: {}, textContent: '', classList: { add() {}, remove() {}, toggle() {} }, appendChild() {}, remove() {} });
    global.document = { getElementById: () => null, createElement: el, body: { appendChild() {} }, documentElement: { clientWidth: 1000, clientHeight: 700 } };
    global.localStorage = { getItem: () => null, setItem() {}, removeItem() {} };
    global.requestAnimationFrame = () => 1;
    global.cancelAnimationFrame = () => {};
    global.WebSocket = class {
        constructor(url) { this.url = url; sockets.push(this); }
        close() {}
    };
    const bus = new EventTarget();
    bus.emit = (name, detail) => { events.push(name); bus.dispatchEvent(new CustomEvent(name, { detail })); };
    bus.on = (name, fn) => bus.addEventListener(name, fn);
    global.window.feedBack = bus;
    const root = path.join(__dirname, '..', '..', 'static');
    hwMod = await import(pathToFileURL(path.join(root, 'highway.js')).href).catch((e) => ({ error: e }));
});

const SONG_INFO = { type: 'song_info', title: 'Lane Song', artist: 'Band', arrangement: 'Drums', arrangement_index: 2, arrangements: [], tuning: [0, 0, 0, 0, 0, 0], duration: 100, stems: [] };

async function stream(hw, opts) {
    sockets = [];
    events = [];
    hw.init({ style: {}, width: 0, height: 0, getContext: () => null });
    hw.connect('ws://host/ws/highway/lane.sloppak?arrangement=2', opts);
    const ws = sockets[0];
    for (const msg of [SONG_INFO, { type: 'beats', data: [{ time: 0, measure: 1 }, { time: 0.5, measure: -1 }] }, { type: 'ready' }]) {
        ws.onmessage({ data: JSON.stringify(msg) });
    }
    for (let i = 0; i < 10; i++) await new Promise((r) => setImmediate(r));
}

test('a secondary highway keeps its data but does not rebroadcast song state', async (t) => {
    if (hwMod.error || typeof window.createHighway !== 'function') return t.skip('highway.js not importable under node');
    window.feedBack.currentSong = { title: 'Main Song' };
    const hw = window.createHighway();
    let info = null;
    await stream(hw, { secondary: true, onSongInfo: (m) => { info = m; } });
    assert.equal(window.feedBack.currentSong.title, 'Main Song', 'currentSong untouched');
    assert.deepEqual(events.filter((n) => ['song:loaded', 'beats:loaded', 'song:ready'].includes(n)), []);
    // The lane still parses and uses the stream.
    assert.equal(info && info.title, 'Lane Song');
    assert.equal(hw.getBeats().length, 2);
    assert.equal(hw.getSongInfo().title, 'Lane Song');
    hw.stop();
});

test('a primary highway still broadcasts (control)', async (t) => {
    if (hwMod.error || typeof window.createHighway !== 'function') return t.skip('highway.js not importable under node');
    window.feedBack.currentSong = { title: 'Main Song' };
    const hw = window.createHighway();
    await stream(hw, { onSongInfo: () => {} });
    assert.equal(window.feedBack.currentSong.title, 'Lane Song');
    for (const n of ['song:loaded', 'beats:loaded', 'song:ready']) {
        assert.ok(events.includes(n), `${n} emitted`);
    }
    hw.stop();
});

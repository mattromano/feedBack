// static/js/miss-audio.js: Rock Band-style miss audio (off by default).
// Drives create() with a fake AudioContext, a fake clock, a fake storage and a
// fake stems capability host; no browser needed.

const { test } = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');

const M = require(path.join(__dirname, '..', '..', 'static', 'js', 'miss-audio.js'));

function fakeAudio() {
    const plays = [];
    const node = () => ({ connect() {}, disconnect() {}, gain: { value: 1, setValueAtTime() {}, exponentialRampToValueAtTime() {} },
        frequency: { value: 0, setValueAtTime() {}, exponentialRampToValueAtTime() {} }, Q: { value: 0 },
        start() { plays.push(1); }, stop() {} });
    const ctx = {
        currentTime: 0, sampleRate: 8000, state: 'running', destination: {},
        createGain: node, createBiquadFilter: node, createOscillator: node, createBufferSource: node,
        createBuffer: (ch, n) => ({ getChannelData: () => new Float32Array(n) }),
    };
    return { factory: () => ctx, plays };
}

function fakeCaps() {
    const log = [];
    return {
        log,
        claim: (c) => log.push(['claim', c.claimId]),
        dispatch: (d) => { log.push([d.command, d.args.id || d.args.claimId]); return Promise.resolve({ status: 'handled' }); },
        release: (c) => log.push(['release', c.claimId]),
    };
}

function harness(stored, arrangement) {
    let t = 0;
    const store = new Map(stored ? [[M.STORAGE_KEY, stored]] : []);
    const audio = fakeAudio();
    const caps = fakeCaps();
    const win = { feedBack: { currentSong: { arrangement: arrangement || 'Lead' } } };
    const ctl = M.create({ win, caps, audioFactory: audio.factory, now: () => t,
        storage: { getItem: (k) => store.get(k) ?? null, setItem: (k, v) => store.set(k, v) } });
    return { ctl, audio, caps, store, tick: (ms) => { t += ms; } };
}

const settle = () => new Promise((r) => setImmediate(r));

test('off by default: misses make no sound and mute nothing', () => {
    const h = harness();
    assert.equal(h.ctl.mode(), 'off');
    assert.equal(h.ctl.handle('drums:miss', {}), null);
    assert.equal(h.audio.plays.length, 0);
    assert.equal(h.caps.log.length, 0);
});

test('clunk: a miss or overhit plays the clunk, rate-limited, and never mutes a stem', () => {
    const h = harness('clunk');
    assert.equal(h.ctl.handle('drums:miss', {}), 'clunk');
    h.tick(30);
    assert.equal(h.ctl.handle('drums:overhit', {}), null, 'too soon after the last one');
    h.tick(100);
    assert.equal(h.ctl.handle('note:miss', {}), 'clunk');
    assert.equal(h.ctl.handle('drums:miss', { auto: true }), null, 'auto notes never clunk');
    assert.equal(h.caps.log.length, 0);
});

test('dropout: the instrument stem drops on a miss and comes back on the next hit', async () => {
    const h = harness('dropout');
    h.ctl.handle('drums:miss', {});
    assert.deepEqual(h.ctl.muted(), ['drums']);
    assert.deepEqual(h.caps.log.slice(0, 2), [['claim', M.CLAIM_PREFIX + 'drums'], ['mute', 'drums']]);
    h.tick(200);
    h.ctl.handle('drums:overhit', {});
    assert.equal(h.caps.log.filter(x => x[0] === 'mute').length, 1, 'muted once');
    h.ctl.handle('drums:hit', {});
    await settle();
    assert.deepEqual(h.ctl.muted(), []);
    assert.deepEqual(h.caps.log.slice(-2), [['restore', M.CLAIM_PREFIX + 'drums'], ['release', M.CLAIM_PREFIX + 'drums']]);
});

test('dropout: guitar misses drop the guitar stem, or bass on a bass arrangement; a seek restores', async () => {
    const g = harness('dropout', 'Lead');
    g.ctl.handle('note:miss', {});
    assert.deepEqual(g.ctl.muted(), ['guitar']);
    g.ctl.handle('song:seek', {});
    await settle();
    assert.deepEqual(g.ctl.muted(), []);
    const b = harness('dropout', 'Bass');
    b.ctl.handle('five_fret:overstrum', {});
    assert.deepEqual(b.ctl.muted(), ['bass']);
});

test('a band session (play:judgment) owns miss audio: this stands down for a while', () => {
    const h = harness('dropout');
    h.ctl.handle('play:judgment', {});
    assert.equal(h.ctl.handle('drums:miss', {}), null);
    assert.deepEqual(h.ctl.muted(), []);
    h.tick(6000);
    assert.equal(h.ctl.handle('drums:miss', {}), 'clunk');
});

test('setMode saves the choice, rejects unknown modes and turning dropout off restores', async () => {
    const h = harness('dropout');
    h.ctl.handle('drums:miss', {});
    assert.equal(h.ctl.setMode('loud'), 'off');
    assert.equal(h.store.get(M.STORAGE_KEY), 'off');
    await settle();
    assert.deepEqual(h.ctl.muted(), []);
    assert.equal(h.ctl.setMode('clunk'), 'clunk');
});

test('install wires the bus events and the settings select', () => {
    const handlers = {};
    const bus = { on: (type, fn) => { handlers[type] = fn; } };
    const sel = { value: '', listeners: [], addEventListener(ev, fn) { this.listeners.push(fn); } };
    const win = { document: { getElementById: (id) => (id === 'setting-miss-audio' ? sel : null) }, feedBack: { currentSong: null } };
    const ctl = M.install({ win, bus, caps: fakeCaps(), audioFactory: fakeAudio().factory, storage: null });
    for (const type of ['drums:miss', 'drums:hit', 'note:miss', 'five_fret:miss', 'song:seek', 'play:judgment']) {
        assert.equal(typeof handlers[type], 'function', type);
    }
    assert.equal(sel.value, 'off');
    sel.value = 'clunk';
    sel.listeners[0]();
    assert.equal(ctl.mode(), 'clunk');
});

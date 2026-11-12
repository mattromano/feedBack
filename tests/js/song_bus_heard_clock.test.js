// Song bus + heard clock in static/js/juce-audio.js (_installRendererBusFeeder).
//
// Under an exclusive output (ASIO / WASAPI exclusive) the song itself rides
// its own engine bus: the stems master is tapped directly, every push carries
// the context frame of its first sample, and the stems context goes to the
// null sink so the app loopback never carries it too. The engine then reports
// the context frame being heard, and the feeder publishes
// window.__fbSongOutputDelay = { ctx, sec, source } for the stems transport.
// On the default device the delay comes from the context's own output
// timestamp (or its reported latencies when that is stale).
//
// Same harness as renderer_bus_feeder.test.js: the IIFE runs in a vm sandbox
// against fakes, and the estimator's interval is driven by hand.

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const SRC = fs.readFileSync(path.join(__dirname, '..', '..', 'static', 'js', 'juce-audio.js'), 'utf8');

function extractFeederIIFE(src) {
    const start = src.indexOf('(function _installRendererBusFeeder() {');
    assert.ok(start !== -1);
    let i = src.indexOf('{', start) + 1, depth = 1;
    while (i < src.length && depth > 0) { if (src[i] === '{') depth++; else if (src[i] === '}') depth--; i++; }
    return src.slice(start, i) + ')();';
}

function makeCtx() {
    return {
        sampleRate: 48000, state: 'running', currentTime: 10, baseLatency: 0.01, outputLatency: 0.04,
        sinkIdCalls: [], outputTs: null,
        setSinkId(v) { this.sinkIdCalls.push(v); return Promise.resolve(); },
        resume() { return Promise.resolve(); },
        getOutputTimestamp() { return this.outputTs || { contextTime: 0, performanceTime: 0 }; },
        audioWorklet: { addModule: () => Promise.resolve() },
        createMediaStreamSource() { return { connect() {}, disconnect() {} }; },
        close() { return Promise.resolve(); },
    };
}

function makeSandbox({ exclusive = true, clock = () => ({ valid: false }), songBus = () => Promise.resolve() } = {}) {
    const calls = { setSongBus: [], pushSongAudio: [], setRendererBus: [] };
    const nodes = [];
    let now = 1000;
    const intervals = [];
    const api = {
        isAudioRunning: () => Promise.resolve(true),
        setRendererBus: (en, g) => { calls.setRendererBus.push([en, g]); return Promise.resolve(); },
        pushRendererAudio() {},
        setPageMuted: (m) => Promise.resolve(m),
        setSongBus: (en, g) => { calls.setSongBus.push([en, g]); return songBus(en, g); },
        pushSongAudio: (buf, rate, start) => { calls.pushSongAudio.push({ frames: buf.length / 2, rate, start }); },
        getSongBusClock: () => Promise.resolve(clock()),
    };
    class FakeWorkletNode {
        constructor(ctx, name) { this.name = name; this.port = { onmessage: null }; nodes.push(this); }
        connect() {} disconnect() {}
    }
    const sandbox = {
        console: { log() {}, warn() {}, error() {} },
        URL: { createObjectURL: () => 'blob:x' }, Blob: class {},
        AudioWorkletNode: FakeWorkletNode, AudioContext: function () { return makeCtx(); },
        WeakSet, WeakMap, Promise, Float32Array, Math, Number, JSON,
        performance: { now: () => now },
        setInterval: (fn) => { intervals.push(fn); return intervals.length; },
        document: { hidden: false, addEventListener() {}, getElementById: () => ({ id: 'audio' }) },
        navigator: { mediaDevices: {} },          // no loopback: app audio falls back
        window: null,
    };
    sandbox.window = {
        feedBackDesktop: { audio: api },
        _juceOutputIsExclusive: () => Promise.resolve(typeof exclusive === 'function' ? exclusive() : exclusive),
        _juceMode: false, _currentSongAudio: null, feedBack: { stems: {} },
    };
    sandbox.globalThis = sandbox;
    sandbox.host = { jucePlayer: () => null, playSong() {}, _audioSeek: () => Promise.resolve({}),
        setPlayButtonState() {}, _songEventPayload: () => ({}), showScreen() {} };
    vm.createContext(sandbox);
    vm.runInContext(extractFeederIIFE(SRC), sandbox);
    return {
        sb: sandbox, calls, nodes,
        tick: () => sandbox.window._reevaluateRendererBus(),
        // The estimator is the first interval the feeder registers (100 ms);
        // the second is its own 500 ms re-evaluate, driven here via tick().
        sample: async () => { intervals[0](); await new Promise((r) => setImmediate(r)); },
        advance: (ms) => { now += ms; },
    };
}

const graphOf = () => ({ context: makeCtx(), masterNode: { connected: [], connect(n) { this.connected.push(n); },
    disconnect(n) { this.connected = this.connected.filter((x) => x !== n); } } });
const songNode = (h) => h.nodes.find((n) => n.name === 'feedback-song-tap');

test('exclusive + stems: the song rides the song bus, its context on the null sink', async () => {
    const h = makeSandbox();
    const g = graphOf();
    h.sb.window.feedBack.stems.audioGraph = g;
    await h.tick();
    assert.deepEqual(h.calls.setSongBus.at(-1), [true, 1.0]);
    assert.equal(g.context.sinkIdCalls.at(-1)?.type, 'none', 'stems ctx off the page output → not in the loopback');
    assert.ok(songNode(h) && g.masterNode.connected.includes(songNode(h)), 'master tapped directly');
    // The old surface 'stems' fallback must not ALSO carry it on the app bus.
    await h.tick();
    assert.ok(!h.calls.setRendererBus.some(([en]) => en), 'app bus not engaged for the song');
});

test('pushes carry the context frame of their first sample; a gap starts a new push', async () => {
    const h = makeSandbox();
    h.sb.window.feedBack.stems.audioGraph = graphOf();
    await h.tick();
    const port = songNode(h).port;
    const q = (f) => port.onmessage({ data: { f, d: new Float32Array(256) } });   // 128 frames
    for (let k = 0; k < 4; k++) q(4800 + k * 128);       // 512 frames from 4800 → one 10 ms push
    assert.deepEqual(h.calls.pushSongAudio[0], { frames: 512, rate: 48000, start: 4800 });
    q(5312); q(5440 + 128);                               // second quantum skips a frame block
    assert.deepEqual(h.calls.pushSongAudio[1], { frames: 128, rate: 48000, start: 5312 }, 'flushed at the gap');
    for (let k = 1; k < 4; k++) q(5568 + k * 128);
    assert.equal(h.calls.pushSongAudio[2].start, 5568, 'new batch starts at the jumped frame');
});

test('shared output: no song bus, sink stays default', async () => {
    const h = makeSandbox({ exclusive: false });
    const g = graphOf();
    h.sb.window.feedBack.stems.audioGraph = g;
    await h.tick();
    assert.equal(h.calls.setSongBus.length, 0);
    assert.equal(g.context.sinkIdCalls.length, 0);
});

test('heard delay from the engine: context clock minus the heard frame, median-filtered', async () => {
    const heardMs = [];
    let ctxRef = null;
    const h = makeSandbox({ clock: () => ({ valid: true, heardSourceFrame: (ctxRef.currentTime - heardMs.shift() / 1000) * 48000 }) });
    const g = graphOf();
    ctxRef = g.context;
    h.sb.window.feedBack.stems.audioGraph = g;
    await h.tick();
    for (const d of [58, 57, 59, 58, 300 /* outlier */, 58]) { heardMs.push(d); await h.sample(); h.advance(100); g.context.currentTime += 0.1; }
    const pub = h.sb.window.__fbSongOutputDelay;
    assert.equal(pub.source, 'song-bus');
    assert.equal(pub.ctx, g.context);
    assert.ok(Math.abs(pub.sec - 0.058) < 1e-9, 'median ignores the outlier: ' + pub.sec);
});

test('engine clock invalid (bus priming / underflow): no new sample, nothing invented', async () => {
    const h = makeSandbox({ clock: () => ({ valid: false }) });
    h.sb.window.feedBack.stems.audioGraph = graphOf();
    await h.tick();
    for (let i = 0; i < 5; i++) await h.sample();
    assert.equal(h.sb.window.__fbSongOutputDelay, null);
});

test('back to shared mode: the song-bus figure is dropped, the default-device one takes over', async () => {
    let excl = true;
    let ctxRef = null;
    const h = makeSandbox({ exclusive: () => excl, clock: () => ({ valid: true, heardSourceFrame: (ctxRef.currentTime - 0.058) * 48000 }) });
    const g = graphOf();
    ctxRef = g.context;
    h.sb.window.feedBack.stems.audioGraph = g;
    await h.tick();
    for (let i = 0; i < 4; i++) await h.sample();
    assert.equal(h.sb.window.__fbSongOutputDelay.source, 'song-bus');
    excl = false;
    await h.tick();
    assert.deepEqual(h.calls.setSongBus.at(-1), [false, 0]);
    assert.equal(g.context.sinkIdCalls.at(-1), '', 'default sink restored');
    assert.equal(h.sb.window.__fbSongOutputDelay, null, 'stale song-bus figure cleared at once');
    // A frozen output timestamp (seen after a null-sink round trip) is not
    // trusted: the context's reported latencies (10 + 40 ms) are used.
    g.context.outputTs = { contextTime: 1, performanceTime: 1 };
    for (let i = 0; i < 4; i++) await h.sample();
    const pub = h.sb.window.__fbSongOutputDelay;
    assert.equal(pub.source, 'output-timestamp');
    assert.ok(Math.abs(pub.sec - 0.05) < 1e-9, String(pub.sec));
    // A live timestamp is used directly.
    g.context.currentTime = 20;
    g.context.outputTs = { contextTime: 19.955, performanceTime: h.sb.performance.now() };
    for (let i = 0; i < 15; i++) await h.sample();
    assert.ok(Math.abs(h.sb.window.__fbSongOutputDelay.sec - 0.045) < 1e-9);
});

test('a failed engage restores the sink and drops its tap (no silent song, no orphan taps)', async () => {
    let fail = true, excl = true;
    const h = makeSandbox({ exclusive: () => excl, songBus: (en) => (en && fail ? Promise.reject(new Error('engine busy')) : Promise.resolve()) });
    const g = graphOf();
    h.sb.window.feedBack.stems.audioGraph = g;
    await h.tick();
    assert.equal(g.context.sinkIdCalls.at(-1), '', 'stems ctx back on the default sink after the failure');
    assert.equal(g.masterNode.connected.length, 0, 'tap disconnected from the master');
    assert.equal(h.sb.window._songBusState().engaged, false);
    // Retries don't pile up taps.
    await h.tick(); await h.tick();
    assert.equal(g.masterNode.connected.length, 0);
    assert.equal(g.context.sinkIdCalls.at(-1), '');
    // A switch back to shared mode leaves the song audible on the default sink.
    excl = false;
    await h.tick();
    assert.equal(g.context.sinkIdCalls.at(-1), '');
    // And once the engine accepts, it engages normally with exactly one tap.
    excl = true; fail = false;
    await h.tick();
    assert.equal(h.sb.window._songBusState().engaged, true);
    assert.equal(g.masterNode.connected.length, 1);
    assert.equal(g.context.sinkIdCalls.at(-1)?.type, 'none');
});

test('exclusive output without the song bus: the context\'s own timestamp is never published', async () => {
    // Song bus failing (engine busy) under ASIO: the stems ctx's timestamp
    // describes the null sink / unused default device, not what is heard.
    const h = makeSandbox({ songBus: (en) => (en ? Promise.reject(new Error('engine busy')) : Promise.resolve()) });
    const g = graphOf();
    g.context.outputTs = { contextTime: 9.95, performanceTime: 1000 };
    h.sb.window.feedBack.stems.audioGraph = g;
    // Before the first re-evaluate the output kind is unknown: nothing either.
    for (let i = 0; i < 4; i++) await h.sample();
    assert.ok(!h.sb.window.__fbSongOutputDelay, 'unknown output: nothing published');
    await h.tick();
    for (let i = 0; i < 4; i++) await h.sample();
    assert.equal(h.sb.window.__fbSongOutputDelay, null, 'exclusive, bus down: nothing published');
});

test('exclusive → shared → exclusive: the default-device figure is dropped the moment ASIO is back', async () => {
    let excl = false;
    const h = makeSandbox({ exclusive: () => excl, songBus: (en) => (en ? Promise.reject(new Error('engine busy')) : Promise.resolve()) });
    const g = graphOf();
    h.sb.window.feedBack.stems.audioGraph = g;
    await h.tick();
    for (let i = 0; i < 4; i++) await h.sample();
    assert.equal(h.sb.window.__fbSongOutputDelay.source, 'output-timestamp', 'shared: published');
    excl = true;
    await h.tick();                       // bus engage fails → retrying
    await h.sample();
    assert.equal(h.sb.window.__fbSongOutputDelay, null, 'stale default-device figure cleared under ASIO');
});

test('a suspend keeps the last figure for the same context (no jump on resume)', async () => {
    const h = makeSandbox({ exclusive: false });
    const g = graphOf();
    h.sb.window.feedBack.stems.audioGraph = g;
    await h.tick();
    for (let i = 0; i < 4; i++) await h.sample();
    const before = h.sb.window.__fbSongOutputDelay;
    assert.ok(Math.abs(before.sec - 0.05) < 1e-9);
    g.context.state = 'suspended';
    for (let i = 0; i < 30; i++) { h.advance(100); await h.sample(); }   // a 3 s pause
    const held = h.sb.window.__fbSongOutputDelay;
    assert.ok(held && held.ctx === g.context && Math.abs(held.sec - 0.05) < 1e-9, 'figure kept while suspended');
    assert.ok(h.sb.performance.now() - held.at < 2000, 'kept fresh for the stems transport\'s 2 s rule');
    g.context.state = 'running';
    g.context.baseLatency = 0.02;                      // 70 ms now
    await h.sample();
    assert.ok(Math.abs(h.sb.window.__fbSongOutputDelay.sec - 0.05) < 1e-9, 'first sample after resume: median still the held figure');
    // A closed context drops it.
    g.context.state = 'closed';
    await h.sample();
    assert.equal(h.sb.window.__fbSongOutputDelay, null);
});

test('a suspend never keeps a figure whose path is gone', async () => {
    let excl = false;
    const h = makeSandbox({ exclusive: () => excl, songBus: (en) => (en ? Promise.reject(new Error('engine busy')) : Promise.resolve()) });
    const g = graphOf();
    h.sb.window.feedBack.stems.audioGraph = g;
    await h.tick();
    for (let i = 0; i < 4; i++) await h.sample();
    assert.ok(h.sb.window.__fbSongOutputDelay);
    g.context.state = 'suspended';
    excl = true;
    await h.tick();
    await h.sample();
    assert.equal(h.sb.window.__fbSongOutputDelay, null, 'default-device figure not kept alive under ASIO');
    // Another context's suspended graph never inherits it either.
    excl = false; await h.tick();
    g.context.state = 'running';
    for (let i = 0; i < 4; i++) await h.sample();
    const g2 = graphOf(); g2.context.state = 'suspended';
    h.sb.window.feedBack.stems.audioGraph = g2;
    await h.sample();
    assert.equal(h.sb.window.__fbSongOutputDelay, null);
});

test('__fbHeardDelaySec: the stems transport\'s rule (same context, < 2 s old, 0 < sec < 1)', async () => {
    const h = makeSandbox({ exclusive: false });
    const g = graphOf();
    h.sb.window.feedBack.stems.audioGraph = g;
    await h.tick();
    for (let i = 0; i < 4; i++) await h.sample();
    const f = h.sb.window.__fbHeardDelaySec;
    assert.ok(Math.abs(f(g.context) - 0.05) < 1e-9, 'live figure for this context');
    assert.equal(f(makeCtx()), 0, 'another context: 0');
    assert.equal(f(null), 0);
    h.advance(1999);
    assert.ok(f(g.context) > 0, 'still fresh just under 2 s');
    h.advance(2);
    assert.equal(f(g.context), 0, 'older than 2 s: 0');
    h.sb.window.__fbSongOutputDelay = { ctx: g.context, sec: 1.5, at: h.sb.performance.now() };
    assert.equal(f(g.context), 0, 'out-of-range figure: 0');
});

test('__fbHeardDelaySec exists on a browser host (no desktop bridge) and answers 0', () => {
    const sandbox = { window: { feedBack: {} }, performance: { now: () => 0 }, console: { log() {} } };
    sandbox.globalThis = sandbox;
    vm.createContext(sandbox);
    vm.runInContext(extractFeederIIFE(SRC), sandbox);
    assert.equal(typeof sandbox.window.__fbHeardDelaySec, 'function');
    assert.equal(sandbox.window.__fbHeardDelaySec({}), 0);
});

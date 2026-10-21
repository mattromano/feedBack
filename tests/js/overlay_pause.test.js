// pauseForOverlay / resumeAfterOverlay: a plugin overlay (Build-a-Band's
// Settings over the song) pauses the song, cancelling any count-in, and later
// resumes it through the song count-in, the way the exit-confirm Stay does.
//
// Same isolation strategy as song_close.test.js: extract the functions from
// app.js by brace-matching and run them in a vm sandbox with stubbed deps.

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const { extractFunction } = require('./test_utils');

const APP_JS = path.join(__dirname, '..', '..', 'static', 'app.js');
const SRC = fs.readFileSync(APP_JS, 'utf8');

function load({ isPlaying = false, countingIn = false, gen = 7, wantCountIn = false, activeId = 'player', juce = false } = {}) {
    const pauseSrc = extractFunction(SRC, 'function pauseForOverlay(');
    const resumeSrc = extractFunction(SRC, 'function resumeAfterOverlay(');
    const sb = {
        calls: { togglePlay: 0, cancel: 0, songCountIn: 0 },
        state: { countingIn, gen, wantCountIn, activeId },
        emits: [],
        // JUCE mode: togglePlay's pause awaits this gate (the async IPC).
        gate: null,
    };
    vm.createContext(sb);
    vm.runInContext(`
        var S = { isPlaying: ${isPlaying} };
        var window = {
            _juceMode: ${juce},
            feedBack: { isPlaying: ${isPlaying}, emit(name, payload) { emits.push({ name, payload }); } },
        };
        let _overlayPauseInFlight = null;
        function _songEventPayload() { return { audioT: 1 }; }
        function togglePlay() {
            calls.togglePlay++;
            if (window._juceMode && S.isPlaying) {
                let release;
                globalThis.gate = { promise: new Promise((r) => { release = r; }), release: () => release() };
                return globalThis.gate.promise.then(() => { S.isPlaying = false; window.feedBack.isPlaying = false; });
            }
            S.isPlaying = !S.isPlaying;
        }
        function _cancelCountIn() { calls.cancel++; }
        function isCountingIn() { return state.countingIn; }
        function audioSeekGen() { return state.gen; }
        function _wantSongCountIn() { return state.wantCountIn; }
        function startSongCountIn() { calls.songCountIn++; }
        var document = { querySelector(sel) { return sel === '.screen.active' ? { id: state.activeId } : null; } };
        ${pauseSrc}
        ${resumeSrc}
        globalThis.pause = pauseForOverlay;
        globalThis.resume = resumeAfterOverlay;
        globalThis.getS = () => S;
        globalThis.getFb = () => window.feedBack;
    `, sb);
    return sb;
}

function resetCalls(sb) { sb.calls.togglePlay = 0; sb.calls.cancel = 0; sb.calls.songCountIn = 0; }

test('pausing a playing song cancels the count-in and pauses once', () => {
    const sb = load({ isPlaying: true, gen: 7 });
    const token = sb.pause();
    assert.deepEqual({ ...token }, { wasPlaying: true, gen: 7 });
    assert.equal(sb.calls.cancel, 1);
    assert.equal(sb.calls.togglePlay, 1);
    assert.equal(sb.getS().isPlaying, false);
});

test('pausing during a count-in stops it without starting playback', () => {
    const sb = load({ isPlaying: false, countingIn: true });
    const token = sb.pause();
    assert.equal(token.wasPlaying, true);
    assert.equal(sb.calls.cancel, 1);
    assert.equal(sb.calls.togglePlay, 0);
});

test('an already paused song stays paused', () => {
    const sb = load({ isPlaying: false });
    const token = sb.pause();
    assert.equal(token.wasPlaying, false);
    resetCalls(sb);
    assert.equal(sb.resume(token), false);
    assert.deepEqual({ ...sb.calls }, { togglePlay: 0, cancel: 0, songCountIn: 0 });
});

test('resume counts in when the song wants it', () => {
    const sb = load({ isPlaying: true, wantCountIn: true });
    const token = sb.pause();
    resetCalls(sb);
    assert.equal(sb.resume(token), true);
    assert.equal(sb.calls.songCountIn, 1);
    assert.equal(sb.calls.togglePlay, 0);
});

test('resume without a count-in plays', () => {
    const sb = load({ isPlaying: true, wantCountIn: false });
    const token = sb.pause();
    resetCalls(sb);
    assert.equal(sb.resume(token), true);
    assert.equal(sb.calls.togglePlay, 1);
    assert.equal(sb.calls.songCountIn, 0);
});

test('resume is refused after a song change, while playing, while counting in, off the player, or with no token', () => {
    const cases = [
        ['song change', (sb) => { sb.state.gen = 8; }],
        ['playing', (sb) => { sb.getS().isPlaying = true; }],
        ['counting in', (sb) => { sb.state.countingIn = true; }],
        ['off the player', (sb) => { sb.state.activeId = 'home'; }],
    ];
    for (const [name, mutate] of cases) {
        const sb = load({ isPlaying: true, gen: 7, wantCountIn: true });
        const token = sb.pause();
        mutate(sb);
        resetCalls(sb);
        assert.equal(sb.resume(token), false, name);
        assert.deepEqual({ ...sb.calls }, { togglePlay: 0, cancel: 0, songCountIn: 0 }, name);
    }
    const sb = load({ isPlaying: false });
    assert.equal(sb.resume(undefined), false, 'no token');
    assert.equal(sb.resume(null), false, 'null token');
    assert.deepEqual({ ...sb.calls }, { togglePlay: 0, cancel: 0, songCountIn: 0 }, 'no token');
});

test('both are published on window.feedBack', () => {
    assert.match(SRC, /window\.feedBack\.pauseForOverlay\s*=\s*pauseForOverlay/);
    assert.match(SRC, /window\.feedBack\.resumeAfterOverlay\s*=\s*resumeAfterOverlay/);
});

test('a browser-audio pause clears feedBack.isPlaying and emits song:pause', () => {
    const sb = load({ isPlaying: true, juce: false });
    sb.pause();
    assert.equal(sb.getFb().isPlaying, false);
    assert.deepEqual(sb.emits.map((e) => e.name), ['song:pause']);
    assert.deepEqual({ ...sb.emits[0].payload }, { audioT: 1 });
});

test('a JUCE pause leaves the isPlaying and song:pause emit to togglePlay', () => {
    const sb = load({ isPlaying: true, juce: true });
    sb.pause();
    assert.deepEqual(sb.emits, []);
});

test('a resume during an in-flight JUCE pause waits for it, then plays', async () => {
    const sb = load({ isPlaying: true, juce: true, wantCountIn: false });
    const token = sb.pause();
    assert.equal(sb.getS().isPlaying, true, 'pause still in flight');
    assert.equal(sb.resume(token), true);
    assert.equal(sb.calls.togglePlay, 1, 'nothing resumes before the pause settles');
    sb.gate.release();
    for (let i = 0; i < 5; i++) await null;
    assert.equal(sb.calls.togglePlay, 2);
    assert.equal(sb.getS().isPlaying, true, 'the song ends up playing');
});

test('a resume queued behind an in-flight pause still honours the song check', async () => {
    const sb = load({ isPlaying: true, juce: true });
    const token = sb.pause();
    sb.state.gen = 8;   // song changed behind the overlay
    assert.equal(sb.resume(token), false);
    sb.gate.release();
    for (let i = 0; i < 5; i++) await null;
    assert.equal(sb.calls.togglePlay, 1);
    assert.equal(sb.getS().isPlaying, false);
});

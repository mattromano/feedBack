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

function load({ isPlaying = false, countingIn = false, gen = 7, wantCountIn = false, activeId = 'player' } = {}) {
    const pauseSrc = extractFunction(SRC, 'function pauseForOverlay(');
    const resumeSrc = extractFunction(SRC, 'function resumeAfterOverlay(');
    const sb = {
        calls: { togglePlay: 0, cancel: 0, songCountIn: 0 },
        state: { countingIn, gen, wantCountIn, activeId },
    };
    vm.createContext(sb);
    vm.runInContext(`
        var S = { isPlaying: ${isPlaying} };
        function togglePlay() { calls.togglePlay++; S.isPlaying = !S.isPlaying; }
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

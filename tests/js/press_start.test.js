'use strict';
// Press Start (static/v3/press-start.js): the standard Start button on a free
// controller, outside a song, emits gamepad:start-pressed.
const { test } = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const {
    START_BUTTON, COOLDOWN_MS, isStartEligible, songLoaded, anyVisibleModal, createPressStart,
} = require('../../static/v3/press-start.js');

const OK = { activeId: 'v3-home', songLoaded: false, modalOpen: false, claimCount: 0, picking: false };

test('constants', () => {
    assert.strictEqual(START_BUTTON, 9);
    assert.strictEqual(COOLDOWN_MS, 1000);
});

test('isStartEligible table', () => {
    for (const activeId of ['v3-home', 'v3-songs', 'settings']) {
        assert.strictEqual(isStartEligible({ ...OK, activeId }), true, activeId);
    }
    const no = [
        { songLoaded: true }, { modalOpen: true }, { picking: true }, { claimCount: 1 },
        { activeId: 'player' }, { activeId: 'v3-feedbarcade' }, { activeId: 'plugin-minigames' },
        { activeId: 'plugin-rockherosmith' }, { activeId: '' },
    ];
    for (const over of no) {
        assert.strictEqual(isStartEligible({ ...OK, ...over }), false, JSON.stringify(over));
    }
    assert.strictEqual(isStartEligible({ ...OK, activeId: null }), false);
    assert.strictEqual(isStartEligible(null), false);
});

function audioDoc(src) {
    return {
        getElementById: (id) => (id === 'audio' && src !== undefined
            ? { getAttribute: (n) => (n === 'src' ? src : null) } : null),
    };
}

test('songLoaded', () => {
    const win = { feedBack: {} };
    assert.strictEqual(songLoaded(audioDoc(), win, 'player'), true);
    assert.strictEqual(songLoaded(audioDoc(), { feedBack: { isPlaying: true } }, 'v3-home'), true);
    assert.strictEqual(songLoaded(audioDoc('/a.ogg'), win, 'v3-home'), true);
    assert.strictEqual(songLoaded(audioDoc(), { feedBack: {}, _juceAudioUrl: 'blob:x' }, 'v3-home'), true);
    assert.strictEqual(songLoaded(audioDoc(''), win, 'v3-home'), false);
    assert.strictEqual(songLoaded(audioDoc(null), win, 'v3-home'), false);
    assert.strictEqual(songLoaded(audioDoc(), win, 'v3-home'), false);
    assert.strictEqual(songLoaded(audioDoc(), {}, 'v3-home'), false);
});

function node(o) {
    return {
        hidden: !!o.hidden,
        closest: (sel) => (sel === '[hidden]' && o.inHidden ? {} : null),
        getClientRects: () => ({ length: o.rects === undefined ? 1 : o.rects }),
        _style: { display: o.display || 'block', visibility: o.visibility || 'visible' },
    };
}
function modalEnv(nodes) {
    const doc = { querySelectorAll: (sel) => (sel === '[aria-modal="true"], .feedBack-modal' ? nodes : []) };
    const win = { getComputedStyle: (n) => n._style };
    return { doc, win };
}

test('anyVisibleModal', () => {
    const cases = [
        [node({ hidden: true }), false],
        [node({ inHidden: true }), false],
        [node({ display: 'none' }), false],
        [node({ visibility: 'hidden' }), false],
        [node({ rects: 0 }), false],
        [node({}), true],
    ];
    for (const [n, want] of cases) {
        const { doc, win } = modalEnv([n]);
        assert.strictEqual(anyVisibleModal(doc, win), want);
    }
    const { doc, win } = modalEnv([node({ hidden: true }), node({ rects: 0 })]);
    assert.strictEqual(anyVisibleModal(doc, win), false);
    assert.strictEqual(anyVisibleModal(modalEnv([]).doc, win), false);
    const throwing = { querySelectorAll: () => { throw new Error('x'); } };
    assert.strictEqual(anyVisibleModal(throwing, win), false);
});

function setup(o = {}) {
    const pads = o.pads || [
        { index: 0, id: 'X', mapping: 'standard', connected: true, claimedBy: null, controllerKey: 'X#1' },
        { index: 1, id: 'X', mapping: 'standard', connected: true, claimedBy: null, controllerKey: 'X#2' },
    ];
    let watchers = 0;
    let watchCalls = 0;
    const gamepadInput = {
        list: () => pads,
        watch: () => { watchers += 1; watchCalls += 1; return () => { watchers -= 1; }; },
    };
    const emits = [];
    let t = 5000;
    let env = o.env || OK;
    const inst = createPressStart({
        gamepadInput,
        emit: (type, detail) => emits.push({ type, detail }),
        env: () => env,
        now: () => t,
    });
    return {
        inst, emits, pads,
        watchers: () => watchers,
        watchCalls: () => watchCalls,
        advance: (ms) => { t += ms; },
        setEnv: (e) => { env = e; },
    };
}
const press = (pad, extra) => ({ pad, type: 'button', index: 9, value: 1, pressed: true, t: 0, ...extra });

test('sync watches once and unwatches', () => {
    const s = setup();
    assert.strictEqual(s.inst.watching(), false);
    s.inst.sync(true);
    s.inst.sync(true);
    assert.strictEqual(s.watchCalls(), 1);
    assert.strictEqual(s.watchers(), 1);
    assert.strictEqual(s.inst.watching(), true);
    s.inst.sync(false);
    s.inst.sync(false);
    assert.strictEqual(s.watchers(), 0);
    assert.strictEqual(s.inst.watching(), false);
    s.inst.sync(true);
    assert.strictEqual(s.watchCalls(), 2);
    assert.strictEqual(s.watchers(), 1);
});

test('sync without a watch function does nothing', () => {
    const inst = createPressStart({ gamepadInput: { list: () => [] }, emit: () => {}, env: () => OK, now: () => 0 });
    inst.sync(true);
    assert.strictEqual(inst.watching(), false);
});

test('Start on a free standard pad emits once with the pad', () => {
    const s = setup();
    s.inst.onWatch(press(0));
    assert.deepStrictEqual(s.emits, [
        { type: 'gamepad:start-pressed', detail: { pad: 0, id: 'X', controllerKey: 'X#1' } },
    ]);
});

test('ignored', () => {
    const s = setup();
    s.inst.onWatch(press(0, { pressed: false, value: 0 }));
    s.inst.onWatch(press(0, { index: 0 }));
    s.inst.onWatch(press(0, { type: 'axis' }));
    s.inst.onWatch(press(5));
    s.pads[0].mapping = '';
    s.inst.onWatch(press(0));
    s.pads[0].mapping = 'standard';
    s.pads[0].claimedBy = 'five_fret';
    s.inst.onWatch(press(0));
    s.pads[0].claimedBy = null;
    s.pads[0].connected = false;
    s.inst.onWatch(press(0));
    s.pads[0].connected = true;
    s.setEnv({ ...OK, activeId: 'player', songLoaded: true });
    s.inst.onWatch(press(0));
    s.setEnv({ ...OK, modalOpen: true });
    s.inst.onWatch(press(0));
    assert.deepStrictEqual(s.emits, []);
    // An ignored press doesn't start the cooldown.
    s.setEnv(OK);
    s.inst.onWatch(press(0));
    assert.strictEqual(s.emits.length, 1);
});

test('cooldown dedupes a mirror slot', () => {
    const s = setup();
    s.inst.onWatch(press(0));
    s.advance(10);
    s.inst.onWatch(press(1));
    assert.strictEqual(s.emits.length, 1);
    s.advance(1001);
    s.inst.onWatch(press(1));
    assert.strictEqual(s.emits.length, 2);
    assert.deepStrictEqual(s.emits[1].detail, { pad: 1, id: 'X', controllerKey: 'X#2' });
});

test('index.html loads press-start.js after play-modes.js and after gamepad-input.js', () => {
    const html = fs.readFileSync(path.join(__dirname, '../../static/v3/index.html'), 'utf8');
    const at = (p) => html.indexOf(p);
    const ps = at('<script defer src="/static/v3/press-start.js"></script>');
    assert.ok(ps > 0);
    assert.ok(ps > at('/static/v3/play-modes.js'));
    assert.ok(ps > at('/static/capabilities/gamepad-input.js'));
    assert.ok(at('/static/capabilities/gamepad-input.js') > 0);
});

// ── install: the browser wiring, dependency-injected ────────────────────────
const { install } = require('../../static/v3/press-start.js');

function page() {
    const handlers = {};
    const emits = [];
    let watchers = 0;
    const st = { screen: 'player', src: '/a.ogg', wanted: true };
    const fb = {
        isPlaying: true,
        on: (t, fn) => { (handlers[t] = handlers[t] || []).push(fn); },
        emit: (t, d) => emits.push({ type: t, detail: d }),
    };
    const gi = {
        list: () => [{ index: 0, id: 'X', mapping: 'standard', connected: true, claimedBy: null, controllerKey: 'X#1' }],
        watch: () => { watchers += 1; return () => { watchers -= 1; }; },
        claimCount: () => 0,
    };
    const pms = { startPressWanted: () => st.wanted };
    const doc = {
        querySelector: (sel) => (sel === '.screen.active' ? { id: st.screen } : null),
        getElementById: (id) => (id === 'audio' ? { getAttribute: (n) => (n === 'src' ? st.src : null) } : null),
        querySelectorAll: () => [],
    };
    const root = { feedBack: fb, getComputedStyle: () => ({ display: 'block', visibility: 'visible' }) };
    const fire = (t, detail) => (handlers[t] || []).forEach((fn) => fn({ detail }));
    return { fb, gi, pms, doc, root, handlers, emits, st, fire, watchers: () => watchers };
}

test('install wires the resync events and song:loading', () => {
    const p = page();
    const inst = install({ fb: p.fb, gi: p.gi, pms: p.pms, doc: p.doc, root: p.root, now: () => 0 });
    for (const t of ['ui:play-modes-changed', 'screen:changed', 'song:stop', 'song:ended', 'song:loading']) {
        assert.strictEqual((p.handlers[t] || []).length, 1, t);
    }
    // On the player with a song: not watching.
    assert.strictEqual(inst.watching(), false);
    // Off the player, nothing loaded: each resync event arms the watch.
    p.st.screen = 'v3-home'; p.st.src = ''; p.fb.isPlaying = false;
    for (const t of ['ui:play-modes-changed', 'screen:changed', 'song:stop', 'song:ended']) {
        p.fire('song:loading');
        assert.strictEqual(inst.watching(), false, 'song:loading unwatches');
        p.fire(t);
        assert.strictEqual(inst.watching(), true, t);
    }
    assert.strictEqual(p.watchers(), 1);
    // No mode wants Press Start: a resync unwatches.
    p.st.wanted = false;
    p.fire('ui:play-modes-changed');
    assert.strictEqual(inst.watching(), false);
    assert.strictEqual(p.watchers(), 0);
});

test('install re-arms after leaving a song: song:stop then screen:changed', () => {
    const p = page();
    const inst = install({ fb: p.fb, gi: p.gi, pms: p.pms, doc: p.doc, root: p.root, now: () => 5000 });
    assert.strictEqual(inst.watching(), false);
    // session.js emits song:stop before clearing the audio src: still loaded.
    p.st.screen = 'v3-home';
    p.fire('song:stop');
    assert.strictEqual(inst.watching(), false);
    // Then the teardown clears src and isPlaying, and screen:changed follows.
    p.st.src = ''; p.fb.isPlaying = false;
    p.fire('screen:changed', { id: 'v3-home' });
    assert.strictEqual(inst.watching(), true);
    inst.onWatch({ pad: 0, type: 'button', index: 9, value: 1, pressed: true, t: 0 });
    assert.deepStrictEqual(p.emits, [
        { type: 'gamepad:start-pressed', detail: { pad: 0, id: 'X', controllerKey: 'X#1' } },
    ]);
});

test('session.js leaving the player clears feedBack.isPlaying beside S.isPlaying', () => {
    const src = fs.readFileSync(path.join(__dirname, '../../static/js/session.js'), 'utf8');
    const start = src.indexOf("if (id !== 'player') {");
    assert.ok(start > 0);
    const block = src.slice(start, src.indexOf('window.scrollTo(0, 0);', start));
    const s = block.lastIndexOf('S.isPlaying = false;');
    assert.ok(s > 0);
    // After the JUCE branch, i.e. in the teardown every mode runs.
    assert.ok(s > block.indexOf("window._juceAudioUrl = null;"));
    assert.match(block.slice(s), /^S\.isPlaying = false;[\s\S]*?if \(window\.feedBack\) window\.feedBack\.isPlaying = false;\s*setPlayButtonState\(false\);/);
});

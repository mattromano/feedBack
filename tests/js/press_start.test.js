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

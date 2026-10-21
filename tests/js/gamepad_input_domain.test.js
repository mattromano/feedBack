// Behavioral tests for static/capabilities/gamepad-input.js — the core-owned
// gamepad-input domain (claim a controller, receive raw button/axis changes).
// Loaded into a vm on the capabilities harness window with a fake
// navigator.getGamepads and a manual requestAnimationFrame queue, as
// tests/js/gamepad.test.js does for the menu poller.

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { createWindow, ROOT } = require('./capabilities_test_harness');

const CAPABILITIES_JS = path.join(ROOT, 'static', 'capabilities.js');
const GAMEPAD_INPUT_JS = path.join(ROOT, 'static', 'capabilities', 'gamepad-input.js');

function pad(index, opts = {}) {
    return {
        index,
        id: opts.id || 'Xbox 360 Controller (STANDARD GAMEPAD Vendor: 045e Product: 028e)',
        connected: opts.connected !== false,
        mapping: opts.mapping == null ? 'standard' : opts.mapping,
        buttons: (opts.buttons || [0, 0, 0, 0]).map(v => ({ pressed: v >= 0.5, value: v })),
        axes: opts.axes || [0, 0],
    };
}

function load(options = {}) {
    const window = createWindow();
    let pads = [];
    const rafQueue = [];
    let clock = 0;
    window.performance = { now: () => clock };
    window.requestAnimationFrame = (fn) => { rafQueue.push(fn); return rafQueue.length; };
    if (!options.noGamepadApi) window.navigator = { getGamepads: () => pads };
    else window.navigator = {};
    const context = vm.createContext(window);
    vm.runInContext(fs.readFileSync(CAPABILITIES_JS, 'utf8'), context, { filename: CAPABILITIES_JS });
    vm.runInContext(fs.readFileSync(GAMEPAD_INPUT_JS, 'utf8'), context, { filename: GAMEPAD_INPUT_JS });
    const emit = (type, gamepad) => window.dispatchEvent({ type, gamepad });
    return {
        window,
        api: window.feedBack.gamepadInput,
        setPads: (arr) => { pads = arr; },
        connect: (gp) => emit('gamepadconnected', gp),
        disconnect: (gp) => emit('gamepaddisconnected', gp),
        tick: () => { const fn = rafQueue.shift(); if (fn) fn(clock); },
        polling: () => rafQueue.length > 0,
        setClock: (ms) => { clock = ms; },
    };
}

test('list parses vendor/product', () => {
    const g = load();
    g.setPads([
        pad(0, { id: 'Xbox 360 Controller (STANDARD GAMEPAD Vendor: 045e Product: 02ea)' }),
        null,
        pad(2, { id: 'Guitar Hero Guitar (Vendor: 1430 Product: 4748)', mapping: '' }),
        pad(3, { id: 'Some Generic Pad' }),
    ]);
    const list = g.api.list();
    assert.equal(g.api.version, 1);
    assert.equal(list.length, 3, 'null slots are skipped');
    assert.deepEqual({ ...list[0] }, {
        index: 0, id: 'Xbox 360 Controller (STANDARD GAMEPAD Vendor: 045e Product: 02ea)',
        mapping: 'standard', vendor: '045e', product: '02ea', connected: true, claimedBy: null,
    });
    assert.equal(list[1].index, 2);
    assert.equal(list[1].vendor, '1430');
    assert.equal(list[1].product, '4748');
    assert.equal(list[1].mapping, '');
    assert.equal(list[2].vendor, null);
    assert.equal(list[2].product, null);
});

test('list and claim are safe without navigator.getGamepads', () => {
    const g = load({ noGamepadApi: true });
    assert.deepEqual(Array.from(g.api.list()), []);
    assert.equal(g.api.claim(0, 'five_fret'), null);
    assert.equal(g.api.isClaimed(0), false);
    assert.equal(g.api.claimCount(), 0);
    assert.equal(g.polling(), false);
});

test('claim/release and isClaimed', () => {
    const g = load();
    g.setPads([pad(0)]);
    const changes = [];
    const off = g.api.on('change', (ev) => changes.push(ev.type));
    assert.equal(g.api.isClaimed(0), false);
    const h = g.api.claim(0, 'five_fret');
    assert.ok(h, 'handle returned');
    assert.equal(h.index, 0);
    assert.equal(h.id, pad(0).id);
    assert.equal(h.controllerKey, `${pad(0).id}#1`);
    assert.equal(typeof h.addListener, 'function');
    assert.equal(typeof h.removeListener, 'function');
    assert.equal(g.api.isClaimed(0), true);
    assert.equal(g.api.claimCount(), 1);
    assert.equal(g.api.list()[0].claimedBy, 'five_fret');
    assert.equal(g.api.claim(0, 'five_fret'), h, 'same owner gets the same handle');
    assert.equal(g.api.claim(5, 'five_fret'), null, 'no controller at that index');
    h.release();
    assert.equal(g.api.isClaimed(0), false);
    assert.equal(g.api.claimCount(), 0);
    assert.equal(g.api.list()[0].claimedBy, null);
    assert.deepEqual(changes, ['claim', 'release']);
    off();
    g.api.claim(0, 'five_fret').release();
    assert.deepEqual(changes, ['claim', 'release'], 'unsubscribed listener no longer fires');
});

test('identical controllers get distinct controllerKeys', () => {
    const g = load();
    g.setPads([pad(0), pad(1)]);
    const a = g.api.claim(0, 'p1');
    const b = g.api.claim(1, 'p2');
    assert.equal(a.controllerKey, `${pad(0).id}#1`);
    assert.equal(b.controllerKey, `${pad(0).id}#2`);
});

test('second owner cannot claim', () => {
    const g = load();
    g.setPads([pad(0)]);
    const h = g.api.claim(0, 'five_fret');
    assert.equal(g.api.claim(0, 'drums'), null);
    assert.equal(g.api.list()[0].claimedBy, 'five_fret');
    h.release();
    assert.ok(g.api.claim(0, 'drums'), 'claimable once released');
});

test('button and axis change events with deadzone', () => {
    const g = load();
    g.setPads([pad(0, { buttons: [0, 0, 0, 0], axes: [0, 0] })]);
    const h = g.api.claim(0, 'five_fret');
    const events = [];
    const fn = (ev) => events.push({ ...ev });
    h.addListener(fn);

    g.tick();
    assert.deepEqual(events, [], 'no events without change');

    g.setClock(1500);
    g.setPads([pad(0, { buttons: [1, 0, 0, 0], axes: [0.01, 0] })]);
    g.tick();
    assert.deepEqual(events, [{ type: 'button', index: 0, value: 1, pressed: true, t: 1.5 }]);

    events.length = 0;
    g.setPads([pad(0, { buttons: [1, 0, 0, 0], axes: [0.015, -0.5] })]);
    g.tick();
    assert.deepEqual(events, [{ type: 'axis', index: 1, value: -0.5, t: 1.5 }], 'sub-0.02 axis change ignored');
    assert.ok(!('pressed' in events[0]), 'axis events carry no pressed field');

    events.length = 0;
    g.setPads([pad(0, { buttons: [0, 0, 0, 0], axes: [0.03, -0.5] })]);
    g.tick();
    assert.deepEqual(events, [
        { type: 'button', index: 0, value: 0, pressed: false, t: 1.5 },
        { type: 'axis', index: 0, value: 0.03, t: 1.5 },
    ], 'axis drift accumulates against the last reported value');

    events.length = 0;
    h.removeListener(fn);
    g.setPads([pad(0, { buttons: [1, 0, 0, 0] })]);
    g.tick();
    assert.deepEqual(events, [], 'removed listener gets nothing');
});

test('polling stops when no claims', () => {
    const g = load();
    g.setPads([pad(0)]);
    assert.equal(g.polling(), false, 'no rAF before any claim');
    const h = g.api.claim(0, 'five_fret');
    assert.equal(g.polling(), true, 'claim starts polling');
    g.tick();
    assert.equal(g.polling(), true, 'keeps polling while claimed');
    h.release();
    g.tick();
    assert.equal(g.polling(), false, 'no further frames after the last release');
    g.api.claim(0, 'five_fret');
    assert.equal(g.polling(), true, 'a new claim restarts polling');
});

test('disconnect then reconnect resumes the same handle', () => {
    const g = load();
    const id = 'Guitar Hero Guitar (Vendor: 1430 Product: 4748)';
    g.setPads([pad(0, { id, mapping: '' })]);
    const h = g.api.claim(0, 'five_fret');
    const events = [];
    h.addListener((ev) => events.push({ ...ev }));
    const changes = [];
    g.api.on('change', (ev) => changes.push(ev.type));

    // Unplug: the slot empties. The handle stays valid but silent.
    g.setPads([null]);
    g.disconnect(pad(0, { id, connected: false }));
    assert.doesNotThrow(() => g.tick());
    assert.deepEqual(events, []);
    assert.equal(g.api.claimCount(), 1, 'claim survives the disconnect');
    assert.equal(g.polling(), true);

    // A different controller arriving in that slot is not routed to the handle.
    g.setPads([pad(0, { id: 'Other Pad', buttons: [1, 0, 0, 0] })]);
    g.tick();
    assert.deepEqual(events, []);
    assert.equal(g.api.isClaimed(0), false, 'the other controller is free to claim');

    // The same controller comes back in slot 1; events resume on the same handle.
    g.setPads([pad(0, { id: 'Other Pad' }), pad(1, { id, mapping: '', buttons: [0, 1, 0, 0] })]);
    g.connect(pad(1, { id, mapping: '' }));
    g.tick();
    assert.equal(h.index, 1, 'handle follows the controller to its new slot');
    assert.equal(g.api.isClaimed(1), true);
    assert.deepEqual(events, [{ type: 'button', index: 1, value: 1, pressed: true, t: 0 }]);
    assert.deepEqual(changes, ['disconnect', 'connect']);
    assert.equal(g.api.claim(1, 'five_fret'), h, 'same handle after reconnect');
});

test('unplugging one of two identical controllers leaves the other claim live', () => {
    const g = load();
    g.setPads([pad(0), pad(1)]);
    const a = g.api.claim(0, 'p1');
    const b = g.api.claim(1, 'p2');
    const got = { a: [], b: [] };
    a.addListener((ev) => got.a.push(ev.index));
    b.addListener((ev) => got.b.push(ev.index));

    g.setPads([null, pad(1, { buttons: [0, 0, 1, 0] })]);
    g.tick();
    assert.deepEqual(got, { a: [], b: [2] }, 'the remaining controller stays with its own claim');
    assert.equal(b.index, 1);
    assert.equal(g.api.isClaimed(0), true, 'the empty slot is still held by the silent claim');

    g.setPads([pad(0, { buttons: [0, 0, 0, 1] }), pad(1, { buttons: [0, 0, 1, 0] })]);
    g.tick();
    assert.deepEqual(got, { a: [3], b: [2] }, 'the replugged controller resumes on its handle');
    assert.equal(a.index, 0);
    assert.equal(g.api.claim(2, 'p3'), null);
    g.setPads([pad(0), pad(1), pad(2)]);
    assert.equal(g.api.claim(2, 'p3').controllerKey, `${pad(0).id}#3`);
});

test('registers the capability owner with inspect and list-sources', async () => {
    const g = load();
    const caps = g.window.feedBack.capabilities;
    const pipeline = caps.inspect('gamepad-input');
    assert.ok(pipeline, 'gamepad-input pipeline exists');
    const owner = (pipeline.participants || []).find(p => p.pluginId === 'core.gamepad-input');
    assert.ok(owner, 'core.gamepad-input owner registered');
    assert.equal(owner.kind, 'provider-coordinator');
    assert.equal(owner.safety, 'sensitive');
    for (const cmd of ['inspect', 'list-sources']) assert.ok(owner.commands.includes(cmd), `owner exposes ${cmd}`);

    g.setPads([pad(0)]);
    g.api.claim(0, 'five_fret');
    const listed = await caps.dispatch({ capability: 'gamepad-input', command: 'list-sources', source: 'tester' });
    assert.equal(listed.outcome, 'handled');
    const r = await caps.dispatch({ capability: 'gamepad-input', command: 'inspect', source: 'tester' });
    assert.equal(r.outcome, 'handled');
    const snap = JSON.stringify(r);
    assert.match(snap, /five_fret/, 'snapshot reports the claim owner');
});

// A/V offset hand-set override (feedBack#1046) in static/js/settings.js.
//
// A user control marks the offset as hand-set; Note Detection's untagged
// auto-calibrate call is then ignored instead of overwriting it. Plugins
// acting for the user (Sync Lab Apply) pass 'manual', Sync Lab's
// output-latency follow passes 'follow', and a 'reset' hands the offset back
// to auto-calibrate.
//
// Same isolation strategy as settings_autosave.test.js: extract the function
// by brace-matching and run it in a vm sandbox.

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const SETTINGS_JS = path.join(__dirname, '..', '..', 'static', 'js', 'settings.js');

function extractFunction(src, signature) {
    const start = src.indexOf(signature);
    if (start === -1) throw new Error(`'${signature}' not found in settings.js`);
    const openBrace = src.indexOf('{', start);
    let depth = 1;
    let i = openBrace + 1;
    while (i < src.length && depth > 0) {
        const ch = src[i];
        if (ch === '{') depth++;
        else if (ch === '}') depth--;
        i++;
    }
    return src.slice(start, i);
}

function build() {
    const src = fs.readFileSync(SETTINGS_JS, 'utf8');
    const setsLine = src.match(/const _AV_SOURCES_PAST_OVERRIDE = new Set\([^)]*\);/);
    assert.ok(setsLine, 'missing _AV_SOURCES_PAST_OVERRIDE');
    const sandbox = {
        persisted: 0,
        highwayMs: null,
        console: { info() {}, log() {}, warn() {} },
        document: { getElementById: () => null, activeElement: null },
        emitted: [],
        window: { feedBack: { emit(name, detail) { sandbox.emitted.push({ name, detail }); } } },
    };
    vm.createContext(sandbox);
    vm.runInContext(`
        var _avOffsetMs = 0;
        var _avUserOverride = false;
        function setAllHighwaysAvOffset(ms) { globalThis.highwayMs = ms; }
        function handleSliderInput() {}
        function _persistAvOffset() { globalThis.persisted++; }
        ${setsLine[0]}
        ${extractFunction(src, 'export function setAvOffsetMs(').replace(/^export /, '')}
        ${extractFunction(src, 'export function isAvOffsetLocked(').replace(/^export /, '')}
        globalThis.set = setAvOffsetMs;
        globalThis.locked = isAvOffsetLocked;
        globalThis.state = () => ({ ms: _avOffsetMs, manual: _avUserOverride });
        globalThis.seedManual = (v) => { _avUserOverride = v; };
    `, sandbox);
    return sandbox;
}

test('auto-calibrate applies while the offset has not been set by hand', () => {
    const sb = build();
    assert.equal(sb.set(37), true);
    assert.equal(sb.state().ms, 37);
    assert.equal(sb.state().manual, false);
    assert.equal(sb.highwayMs, 37);
});

test('a hand-set value is not clobbered by an untagged (auto-calibrate) call', () => {
    const sb = build();
    sb.set(-20, false, 'manual');
    assert.deepEqual({ ...sb.state() }, { ms: -20, manual: true });
    assert.equal(sb.set(55), false);
    assert.equal(sb.state().ms, -20);
    assert.equal(sb.highwayMs, -20);
});

test('user-driven plugin Apply (manual) and latency follow still apply past the override', () => {
    const sb = build();
    sb.set(-20, false, 'manual');
    assert.equal(sb.set(-12, false, 'follow'), true);
    assert.deepEqual({ ...sb.state() }, { ms: -12, manual: true });
    assert.equal(sb.set(8, false, 'manual'), true);
    assert.deepEqual({ ...sb.state() }, { ms: 8, manual: true });
});

test('reset clears the override so auto-calibrate can set it again', () => {
    const sb = build();
    sb.set(-20, false, 'manual');
    sb.set(0, false, 'reset');
    assert.deepEqual({ ...sb.state() }, { ms: 0, manual: false });
    assert.equal(sb.set(30), true);
    assert.equal(sb.state().ms, 30);
});

test('load seeds the saved value without changing the override', () => {
    const sb = build();
    sb.seedManual(true);
    assert.equal(sb.set(12, true, 'load'), true);
    assert.deepEqual({ ...sb.state() }, { ms: 12, manual: true });
    assert.equal(sb.persisted, 0, 'load must not echo the value back');
});

test('values are clamped to the +-1000 ms the server accepts', () => {
    const sb = build();
    sb.set(5000, true, 'manual');
    assert.equal(sb.state().ms, 1000);
    sb.set('nonsense', true, 'manual');
    assert.equal(sb.state().ms, 0);
});

test('Sync Lab tags its A/V writes (Apply = manual, auto-follow = follow)', () => {
    const src = fs.readFileSync(path.join(__dirname, '..', '..', 'plugins', 'sync_lab', 'screen.js'), 'utf8');
    assert.match(src, /window\.setAvOffsetMs\(v, false, source\)/);
    assert.match(src, /function applyAv\(v\) \{ setAv\(v, 'manual'\);/);
    assert.match(src, /setAv\(want, 'follow'\)/);
});

test('isAvOffsetLocked follows manual / reset, and every applied change is announced on the bus', () => {
    const sb = build();
    assert.equal(sb.locked(), false);
    sb.set(-20, false, 'manual');
    assert.equal(sb.locked(), true);
    assert.equal(sb.set(40), false, 'an ignored write');
    sb.set(0, false, 'reset');
    assert.equal(sb.locked(), false);
    assert.deepEqual(sb.emitted.map((e) => [e.name, e.detail.ms, e.detail.source, e.detail.locked]), [
        ['settings:av-offset', -20, 'manual', true],
        ['settings:av-offset', 0, 'reset', false],
    ]);
});

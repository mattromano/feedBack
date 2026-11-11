// The v3 instrument menu's "Current tuning" pickers (static/v3/badges.js): set the
// selected instrument's working tuning by hand between songs, from a named quick-set
// list or one note picker per string. Runs the real badges.js + working-tuning
// capability in a stubbed window (no DOM: the menu host is absent, so rendering is
// exercised through currentTuningSection's markup).

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { createWindow, ROOT } = require('./capabilities_test_harness');

const CAPABILITIES_JS = path.join(ROOT, 'static', 'capabilities.js');
const WORKING_TUNING_JS = path.join(ROOT, 'static', 'capabilities', 'working-tuning.js');
const BADGES_JS = path.join(ROOT, 'static', 'v3', 'badges.js');

const API_TUNINGS = {
    referencePitch: 440,
    tunings: {
        'guitar-6': {
            Standard: [82.41, 110.00, 146.83, 196.00, 246.94, 329.63],
            'Drop D': [73.42, 110.00, 146.83, 196.00, 246.94, 329.63],
            'Eb Standard': [77.78, 103.83, 138.59, 185.00, 233.08, 311.13],
        },
    },
    tuningMidis: {
        'guitar-6': {
            Standard: [40, 45, 50, 55, 59, 64],
            'Drop D': [38, 45, 50, 55, 59, 64],
            'Eb Standard': [39, 44, 49, 54, 58, 63],
        },
    },
};

const nums = (a) => (a == null ? a : Array.from(a));
const flush = async () => { for (let i = 0; i < 6; i++) await new Promise((r) => setImmediate(r)); };

async function load(settings) {
    const window = createWindow();
    const routes = {
        '/api/settings': Object.assign({ instrument: 'guitar', string_count: 6, tuning: 'Standard', reference_pitch: 440 }, settings || {}),
        '/api/tunings': API_TUNINGS,
    };
    window.fetch = (url) => {
        const v = routes[url];
        return Promise.resolve(v === undefined
            ? { ok: false, json: () => Promise.resolve(null) }
            : { ok: true, json: () => Promise.resolve(v) });
    };
    // badges.js boots on DOMContentLoaded; keep it from booting (no DOM here).
    window.document.readyState = 'loading';
    window.document.addEventListener = () => {};
    window.document.removeEventListener = () => {};
    // Minimal stand-in for app.js's displayTuningName (an ES module).
    window.displayTuningName = (value, offsets) => {
        if (Array.isArray(offsets)) {
            const k = offsets.join(',');
            return { '0,0,0,0,0,0': 'E Standard', '-2,0,0,0,0,0': 'Drop D', '-1,-1,-1,-1,-1,-1': 'Eb Standard' }[k] || 'Custom Tuning';
        }
        return value || '';
    };
    const context = vm.createContext(window);
    vm.runInContext(fs.readFileSync(CAPABILITIES_JS, 'utf8'), context, { filename: CAPABILITIES_JS });
    vm.runInContext(fs.readFileSync(WORKING_TUNING_JS, 'utf8'), context, { filename: WORKING_TUNING_JS });
    vm.runInContext(fs.readFileSync(BADGES_JS, 'utf8'), context, { filename: BADGES_JS });
    await window.v3Badges.reload();
    await flush();
    return { window, wt: window.feedBack.workingTuning, b: window.v3Badges._internals };
}

test('a named tuning resolves to per-string offsets from Standard', async () => {
    const { b } = await load();
    assert.deepEqual(nums(b.namedTuningOffsets('guitar-6', 'Drop D')), [-2, 0, 0, 0, 0, 0]);
    assert.deepEqual(nums(b.namedTuningOffsets('guitar-6', 'Eb Standard')), [-1, -1, -1, -1, -1, -1]);
    assert.equal(b.namedTuningOffsets('guitar-6', 'Nope'), null);
    assert.equal(b.namedTuningOffsets('bass-4', 'Standard'), null);
});

test('home "Standard" with working offsets [0..0] is home (no bogus "Now in E Standard")', async () => {
    const { b, wt } = await load();
    wt.set({ offsets: [0, 0, 0, 0, 0, 0] }, { instrument: 'guitar-6' });
    const info = b.workingTuningInfo();
    assert.equal(info.label, 'E Standard');
    assert.equal(info.isHome, true, 'same pitches as the home tuning must read as home');
    assert.doesNotMatch(b.currentTuningSection(info), /data-inst-reset/, 'no Back to default while home');
});

test('a retuned string writes the working tuning and shows Back to default', async () => {
    const { b, wt } = await load();
    const offsets = b.currentOffsets();
    assert.deepEqual(nums(offsets), [0, 0, 0, 0, 0, 0]);
    offsets[0] = -2;
    assert.equal(b.setWorkingOffsets(offsets), true);
    const st = wt.get('guitar-6');
    assert.deepEqual(nums(st.offsets), [-2, 0, 0, 0, 0, 0]);
    assert.equal(st.source, 'manual');
    assert.equal(st.provenance, 'assumed');
    const info = b.workingTuningInfo();
    assert.equal(info.isHome, false);
    assert.equal(info.label, 'Drop D');
    const html = b.currentTuningSection(info);
    assert.match(html, /data-inst-reset/, 'Back to default shows once off home');
    assert.match(html, /<option selected>Drop D<\/option>/, 'the quick-set list reflects a matching named tuning');
});

test('per-string pickers name the notes from Standard, low string first', async () => {
    const { b, wt } = await load();
    wt.set({ offsets: [-2, 0, 0, 0, 0, 0] }, { instrument: 'guitar-6' });
    const html = b.currentTuningSection(b.workingTuningInfo());
    const strings = html.match(/<select data-inst-string="\d+"[\s\S]*?<\/select>/g);
    assert.equal(strings.length, 6, 'one picker per string');
    const selected = strings.map((s) => s.match(/<option value="(-?\d+)" selected>([^<]+)</).slice(1));
    assert.deepEqual(selected, [['-2', 'D'], ['0', 'A'], ['0', 'D'], ['0', 'G'], ['0', 'B'], ['0', 'E']]);
});

test('an out-of-range string offset stays selectable', async () => {
    const { b } = await load();
    const choices = nums(b.stringOffsetChoices(-9));
    assert.ok(choices.includes(-9));
    assert.deepEqual(choices, choices.slice().sort((x, y) => x - y));
    assert.ok(nums(b.stringOffsetChoices(0)).includes(-7) && nums(b.stringOffsetChoices(0)).includes(4));
});

test('the instrument selector registers as a working-tuning writer', () => {
    const src = fs.readFileSync(WORKING_TUNING_JS, 'utf8');
    assert.match(src,
        /registerParticipant\('core\.settings\.instruments',\s*\{\s*'working-tuning':\s*\{\s*roles: \['contributor', 'requester'\],\s*operations: \['get-working-tuning', 'set-working-tuning'\]/);
});

test('registerMenuSection adds a slot and replaces by id', async () => {
    const { window } = await load();
    assert.equal(window.v3Badges.registerMenuSection('x', () => {}), true);
    assert.equal(window.v3Badges.registerMenuSection('x', () => {}), true);
    assert.equal(window.v3Badges.registerMenuSection('', () => {}), false);
    assert.equal(window.v3Badges.registerMenuSection('y', null), false);
});

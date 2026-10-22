// First-run tour: the registered play mode's hero half gets a step right after
// the hero step; with no mode (or a mode without a tour) the steps are unchanged.
// vm-loads the real IIFE with a fake window.feedBackTour that records register().

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const SRC = fs.readFileSync(path.join(__dirname, '..', '..', 'static', 'v3', 'onboarding-tour.js'), 'utf8');

function loadSteps(heroMode) {
    const registered = {};
    const win = {
        feedBackTour: { register: (id, opts) => { registered[id] = opts; } },
    };
    if (heroMode !== undefined) {
        win.feedBack = { ui: { playModeState: { heroMode: () => heroMode } } };
    }
    win.window = win;
    const ctx = vm.createContext({
        window: win,
        document: { addEventListener() {}, removeEventListener() {} },
        setTimeout() {},
        console,
    });
    vm.runInContext(SRC, ctx);
    // JSON round-trip: arrays built inside the vm realm fail deepEqual's prototype check.
    return JSON.parse(JSON.stringify(registered['home-onboarding'].buildSteps()));
}

const TODAY = ['hero', 'continue', 'instrument', 'tuner', 'audio', 'profile', 'nav'];

test('no play mode: today\'s steps', () => {
    assert.deepEqual(loadSteps(undefined).map((s) => s.id), TODAY);
    assert.deepEqual(loadSteps(null).map((s) => s.id), TODAY);
});

test('a hero mode with a tour adds play-mode second', () => {
    const steps = loadSteps({ id: 'rhs', tour: { title: 'Play together', content: 'Band up here.' } });
    assert.deepEqual(steps.map((s) => s.id),
        ['hero', 'play-mode', 'continue', 'instrument', 'tuner', 'audio', 'profile', 'nav']);
    const s = steps[1];
    assert.equal(s.shape, 'spotlight');
    assert.equal(s.position, 'bottom');
    assert.equal(s.selector, '#v3-hero-mode');
    assert.equal(s.waitFor, '#v3-hero-mode');
    assert.equal(s.title, 'Play together');
    assert.equal(s.content, 'Band up here.');
});

test('a hero mode without tour adds nothing', () => {
    assert.deepEqual(loadSteps({ id: 'rhs' }).map((s) => s.id), TODAY);
});

test('the mode is read at build time, not registration time', () => {
    const registered = {};
    let mode = null;
    const win = {
        feedBackTour: { register: (id, o) => { registered[id] = o; } },
        feedBack: { ui: { playModeState: { heroMode: () => mode } } },
    };
    win.window = win;
    vm.runInContext(SRC, vm.createContext({ window: win, document: { addEventListener() {} }, setTimeout() {}, console }));
    const b = registered['home-onboarding'].buildSteps;
    assert.equal(b().length, 7);
    mode = { id: 'rhs', tour: { title: 't', content: 'c' } };
    assert.equal(b().length, 8);
});

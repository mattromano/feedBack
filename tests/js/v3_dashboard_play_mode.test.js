const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const DASH = fs.readFileSync(path.join(__dirname, '..', '..', 'static', 'v3', 'dashboard.js'), 'utf8');
const CSS = fs.readFileSync(path.join(__dirname, '..', '..', 'static', 'v3', 'v3.css'), 'utf8');

test("soloHeroHtml keeps today's hero", () => {
    assert.ok(DASH.includes('function soloHeroHtml(spanTwo)'));
    assert.ok(DASH.includes("(spanTwo ? 'lg:col-span-2 ' : '') + 'relative rounded-xl overflow-hidden min-h-[480px] flex items-center bg-fb-bg\">'"));
    assert.ok(DASH.includes('Turn any song into practice.'));
    assert.ok(DASH.includes('>Start Playing</button>'));
    assert.ok(DASH.includes('>Lessons</button>'));
});

test('no mode renders soloHeroHtml(true)', () => {
    assert.match(DASH, /heroMode \? window\.v3PlayModes\.splitHeroHtml\(soloHeroHtml\(false\), heroMode, esc\) : soloHeroHtml\(true\)/);
});

test('renderHero is guarded and runs before dashboard-rendered', () => {
    const call = DASH.indexOf('renderHero(');
    assert.ok(call !== -1);
    const tryIdx = DASH.lastIndexOf('try {', call);
    assert.ok(tryIdx !== -1 && call - tryIdx < 120, 'renderHero( call is inside a try');
    assert.ok(call < DASH.indexOf("new CustomEvent('v3:dashboard-rendered')"));
    const start = DASH.indexOf('pms.start(heroMode.id)');
    assert.ok(start !== -1 && start < DASH.indexOf("new CustomEvent('v3:dashboard-rendered')"));
    assert.ok(DASH.includes('#v3-play-mode-start'));
});

test('re-renders on ui:play-modes-changed', () => {
    assert.match(DASH, /sm\.on\('ui:play-modes-changed', \(\) => \{\s*const a = document\.querySelector\('\.screen\.active'\);\s*if \(a && a\.id === 'v3-home'\) render\(\);/);
});

test('v3.css stacks below 768px', () => {
    assert.ok(CSS.includes('.v3-hero-split'));
    assert.match(CSS, /@media \(min-width: 768px\) \{\s*\.v3-hero-split \{\s*grid-template-columns: 1fr 1fr;/);
    assert.ok(CSS.includes('.v3-hero-mode-start:hover'));
    assert.ok(CSS.includes('.v3-hero-mode-start:focus-visible'));
});

test('no new Tailwind classes in the added dashboard lines', () => {
    const added = DASH.split('\n').filter((l) => /pms|heroMode|play-modes-changed/.test(l)).join('\n');
    assert.ok(!/class=/.test(added), 'added lines introduce no class attributes');
});

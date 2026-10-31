'use strict';
// RockHeroSmith (formerly Build-a-Band) is the first HOME-adjacent sidebar
// entry, right under Home. Source checks against static/v3/shell.js (house
// style): the NAV entry, the PROMOTED_PLUGINS anchor, the bolt icon, and that
// nothing else competes for the slot under Home.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const src = fs.readFileSync(path.join(__dirname, '..', '..', 'static', 'v3', 'shell.js'), 'utf8');

test('PROMOTED rockherosmith anchors after home', () => {
    assert.match(src, /navKey: 'rockherosmith', pluginId: 'rockherosmith', slotId: 'v3-nav-rockherosmith', anchorAfter: 'home'/);
});

test('NAV entry uses the bolt icon', () => {
    assert.match(src, /key: 'rockherosmith', screen: 'plugin-rockherosmith', label: 'RockHeroSmith', group: null,\s+icon: 'bolt'/);
});

test('ICONS has bolt', () => {
    assert.match(src, /bolt: 'M13 2L4 14h7l-1 8 9-12h-7l1-8z'/);
});

test('no build_a_band in shell.js', () => {
    assert.doesNotMatch(src, /build_a_band|build-a-band|Build-a-Band/i);
});

test('only the rockherosmith promo anchors after home', () => {
    const anchors = src.match(/anchorAfter: 'home'/g) || [];
    assert.equal(anchors.length, 1);
    // promotedSlotHTML(it.key) is emitted right after each nav item, so the
    // slot sits directly under Home and above Progress.
    assert.match(src, /navItemHTML\(it\) \+ promotedSlotHTML\(it\.key\)/);
});

test('a plugin sub-screen (plugin-rockherosmith-solo) takes its sidebar entry title, not "Plugins"', () => {
    const m = src.match(/function titleFor\(screenId\) \{[\s\S]*?\r?\n    \}\r?\n/);
    assert.ok(m, 'titleFor present');
    assert.match(m[0], /screenId\.indexOf\(n\.screen \+ '-'\) === 0/);
    assert.ok(m[0].indexOf("n.screen + '-'") < m[0].indexOf("return 'Plugins'"), 'checked before the generic title');
});

test('a plugin-backed hub hides by a style rule, sidebar entry and data-v3-backed buttons alike', () => {
    assert.match(src, /const PLUGIN_BACKED_NAV = \{ feedbarcade: 'minigames', lessons: 'tutorials' \};/);
    assert.match(src, /v3-plugin-backed-css/);
    assert.match(src, /\[data-v3-backed="' \+ key \+ '"\]/);
    const dash = fs.readFileSync(path.join(__dirname, '..', '..', 'static', 'v3', 'dashboard.js'), 'utf8');
    assert.match(dash, /id="v3-lessons-btn" data-v3-backed="lessons"/, "Home's Lessons button is marked");
});

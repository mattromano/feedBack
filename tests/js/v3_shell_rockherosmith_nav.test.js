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

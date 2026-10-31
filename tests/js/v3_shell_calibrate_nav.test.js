'use strict';
// Calibrate (the Sync Lab plugin) is a main-menu entry right under Settings.
// Source checks against static/v3/shell.js (house style).
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const src = fs.readFileSync(path.join(__dirname, '..', '..', 'static', 'v3', 'shell.js'), 'utf8');

test('NAV entry routes Calibrate to the sync_lab screen with the sync icon', () => {
    assert.match(src, /key: 'calibrate',\s+screen: 'plugin-sync_lab',\s+label: 'Calibrate',\s+group: null,\s+icon: 'sync'/);
});

test('PROMOTED calibrate anchors after settings, before Audio', () => {
    const cal = src.search(/navKey: 'calibrate',\s+pluginId: 'sync_lab',\s+slotId: 'v3-nav-calibrate',\s+anchorAfter: 'settings'/);
    const audio = src.indexOf("navKey: 'audio_engine'");
    assert.ok(cal > 0, 'calibrate promo present');
    assert.ok(cal < audio, 'listed first, so it sits directly under Settings');
});

test('ICONS has sync', () => {
    assert.match(src, /sync: 'M12 8v5l3 2M9 2h6M12 4a9 9 0 100 18 9 9 0 000-18z'/);
});

test('the sync_lab manifest labels its entry Calibrate', () => {
    const m = JSON.parse(fs.readFileSync(path.join(__dirname, '..', '..', 'plugins', 'sync_lab', 'plugin.json'), 'utf8'));
    assert.deepEqual(m.nav, { label: 'Calibrate', screen: 'plugin-sync_lab' });
});

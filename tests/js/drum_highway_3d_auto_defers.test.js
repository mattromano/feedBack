// Our build ships the `drums` plugin (YARG scoring, kits, split view) next to
// the bundled drum_highway_3d. Auto is first-match-wins and drum_highway_3d
// sorts before `drums`, so the bundled view must step aside in Auto when the
// drums plugin is installed (it stays selectable from the picker).
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const SRC = fs.readFileSync(path.join(__dirname, '..', '..', 'plugins', 'drum_highway_3d', 'screen.js'), 'utf8');

function matcher(window) {
    const m = SRC.match(/matchesArrangement = (function \(songInfo\) \{[\s\S]*?\n    \});/);
    assert.ok(m, 'drum_highway_3d matchesArrangement found');
    return vm.runInNewContext(`(${m[1]})`, { window });
}

const DRUM_PART = { has_drum_tab: true, arrangement: 'Drums' };

test('drum_highway_3d claims the Drums part when the drums plugin is absent', () => {
    assert.equal(matcher({})(DRUM_PART), true);
});

test('drum_highway_3d defers to the drums plugin in Auto', () => {
    const drums = function () {};
    drums.matchesArrangement = () => true;
    assert.equal(matcher({ feedBackViz_drums: drums })(DRUM_PART), false);
});

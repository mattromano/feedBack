// Issue #17: app-initiated pauses (count-in rewind, leaving the player) and native-transport
// (JUCE) mode were logged as "Audio paused unexpectedly", and play() rejections logged as
// "[object DOMException]". Source-level pins.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const read = (p) => fs.readFileSync(path.join(__dirname, '..', '..', 'static', p), 'utf8');

test('the pause listener skips expected pauses and native-transport mode', () => {
    const app = read('app.js');
    assert.match(app, /if \(S\.expectPause\) \{ S\.expectPause = false; return; \}/);
    assert.match(app, /if \(S\.isPlaying && !window\._juceRerouteInProgress && !window\._juceMode\) \{\s*console\.log\('Audio paused unexpectedly/);
});

test('count-in and leaving the player mark their own pauses', () => {
    const countIn = read('js/count-in.js');
    assert.equal((countIn.match(/S\.expectPause = true;[^\n]*\n\s*audio\.pause\(\);/g) || []).length, 2);
    assert.match(read('js/session.js'), /if \(S\.isPlaying\) S\.expectPause = true;[^\n]*\n\s*audio\.pause\(\);/);
});

test('play() rejections log the error name and message', () => {
    assert.match(read('js/transport.js'), /audio\.play\(\) rejected:', err && err\.name, err && err\.message \? err\.message : err\)/);
    assert.match(read('js/count-in.js'), /rejected after count-in:', err && err\.name, err && err\.message \? err\.message : err\)/);
});

test('3D highways import three.js and the bloom addons by absolute URL', () => {
    const fs2 = require('node:fs'), path2 = require('node:path');
    for (const p of ['highway_3d', 'drum_highway_3d', 'keys_highway_3d']) {
        const src = fs2.readFileSync(path2.join(__dirname, '..', '..', 'plugins', p, 'screen.js'), 'utf8');
        assert.match(src, /const _absUrl = \(p\) =>/, p);
        assert.match(src, /import\(_absUrl\(THREE_URL\)\)/, p);
        assert.match(src, /const A = _absUrl\('\/static\/vendor\/three\/addons\/'\);/, p);
    }
});

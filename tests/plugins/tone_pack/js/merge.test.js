'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const SCREEN = path.join(__dirname, '..', '..', '..', '..', 'plugins', 'tone_pack', 'screen.js');
const { mergePack } = require(SCREEN);

const pack = {
    presets: {
        'Auto · Clean': { nativePreset: 'A', generatedBy: 'tone_pack', outputGain: 1 },
        'Auto · Crunch': { nativePreset: 'B', generatedBy: 'tone_pack', outputGain: 0.5 },
    },
    targets: { clean: 'Auto · Clean', od: 'Auto · Crunch' },
};

test('adds pack presets, fills empty targets, idle = the user\'s own preset', () => {
    const r = mergePack(pack, { 'Main Lead': { nativePreset: 'X' } }, null, []);
    assert.deepEqual(r.added.sort(), ['Auto · Clean', 'Auto · Crunch']);
    assert.equal(r.presets['Main Lead'].nativePreset, 'X');
    assert.deepEqual(r.ta.targets, { clean: 'Auto · Clean', od: 'Auto · Crunch', idle: 'Main Lead' });
});

test('never overrides user targets or user presets with the same name', () => {
    const r = mergePack(pack, { 'Auto · Clean': { nativePreset: 'mine' } }, { targets: { clean: 'Mine' } }, []);
    assert.equal(r.presets['Auto · Clean'].nativePreset, 'mine');
    assert.equal(r.ta.targets.clean, 'Mine');
});

test('a deleted pack preset stays deleted; a changed one refreshes but keeps level tweaks', () => {
    const r1 = mergePack(pack, {}, null, ['Auto · Clean']);
    assert.equal(r1.presets['Auto · Clean'], undefined);
    // older pack preset (no packLevels): takes the new pack levels
    const old = { 'Auto · Crunch': { nativePreset: 'old', generatedBy: 'tone_pack', outputGain: 0.9, created: 5 } };
    const r2 = mergePack(pack, old, null, ['Auto · Crunch']);
    assert.equal(r2.presets['Auto · Crunch'].nativePreset, 'B');
    assert.equal(r2.presets['Auto · Crunch'].outputGain, 0.5);
    // the user moved the level away from what the pack set: kept
    const tweaked = { 'Auto · Crunch': { nativePreset: 'old', generatedBy: 'tone_pack', outputGain: 1.4,
        packLevels: { outputGain: 1 } } };
    const r3 = mergePack(pack, tweaked, null, ['Auto · Crunch']);
    assert.equal(r3.presets['Auto · Crunch'].outputGain, 1.4);
    assert.deepEqual(r2.updated, ['Auto · Crunch']);
});

test('a target pointing at a removed pack preset is moved to the new one', () => {
    const r = mergePack(pack, {}, { targets: { clean: 'Auto · Old Clean' } }, ['Auto · Old Clean']);
    assert.equal(r.ta.targets.clean, 'Auto · Clean');
});

const { songOverrides } = require(SCREEN);
const targets = { clean: 'C', dist: 'D', od: 'O', bass: 'B', solo: 'S', idle: 'I' };
const byName = (n) => (/clean/i.test(n) ? 'clean' : /dist/i.test(n) ? 'dist' : null);

test('songOverrides: only names the keyword classifier misses, bass parts all Bass, $song key', () => {
    const ov = songOverrides({ 'Tone 1': 'dist', 'x_clean': 'od', 'Default': 'clean', '$song': 'od' },
        byName, targets, { songKey: 'sloppak/a.sloppak' });
    assert.deepEqual(ov, { 'Tone 1': 'D', 'Default': 'C', 'sloppak/a.sloppak': 'O' });
    const bass = songOverrides({ 'b_dist': 'bass', 'Tone 0': 'bass' }, byName, targets, { bass: true });
    assert.deepEqual(bass, { 'b_dist': 'B', 'Tone 0': 'B' });
    assert.deepEqual(songOverrides({ 'Tone 1': 'mod' }, byName, targets, {}), {}, 'no target for the category');
});

const { withTrim, homePresetName } = require(SCREEN);

test('trims fold into the pack output gain and refresh existing pack presets', () => {
    const p = { nativePreset: 'A', generatedBy: 'tone_pack', outputGain: 1, category: 'dist', packLevels: { outputGain: 1 } };
    const t = withTrim(p, { dist: 6 });
    assert.ok(Math.abs(t.outputGain - 1.9953) < 1e-3);
    assert.equal(t.packLevels.outputGain, t.outputGain);
    const pk = { presets: { 'Auto · Dist': p }, targets: {} };
    const r = mergePack(pk, { 'Auto · Dist': Object.assign({}, p) }, null, ['Auto · Dist'], { dist: 6 });
    assert.deepEqual(r.updated, ['Auto · Dist']);
    assert.ok(r.presets['Auto · Dist'].outputGain > 1.9);
});

test('homePresetName: Main Lead, else the Idle target', () => {
    assert.equal(homePresetName({ 'Main Lead': {}, X: {} }, {}), 'Main Lead');
    assert.equal(homePresetName({ X: {} }, { targets: { idle: 'X' } }), 'X');
    assert.equal(homePresetName({}, {}), null);
});

const { isMetalArtist, _norm } = require(SCREEN);

test('isMetalArtist: normalised names and multi-artist credits', () => {
    const set = new Set(['my chemical romance', 'babymetal', 'motorhead'].map(_norm));
    assert.equal(isMetalArtist('My Chemical Romance', set), true);
    assert.equal(isMetalArtist('Motörhead', set), true);
    assert.equal(isMetalArtist('Lil Uzi Vert, BABYMETAL', set), true);
    assert.equal(isMetalArtist('Sum 41', set), false);
});

test('metal song: crunch / distortion tones (by name or gear) go to the metal preset; clean and lead stay', () => {
    const t = { clean: 'C', dist: 'Main Lead', od: 'Main Lead', solo: 'Metal Tone', idle: 'Main Lead' };
    const cls = (n) => (/clean/i.test(n) ? 'clean' : /dist/i.test(n) ? 'dist' : /lead/i.test(n) ? 'solo' : null);
    const ov = songOverrides({ 'x_clean': 'clean', 'x_dist': 'dist', 'Tone 1': 'od', 'x_lead': 'solo', 'Tone 2': null },
        cls, t, { metalPreset: 'Metal Tone' });
    assert.deepEqual(ov, { 'x_dist': 'Metal Tone', 'Tone 1': 'Metal Tone' });
});

test('preferred targets replace the category targets once', () => {
    const presets = { 'Main Lead': {}, 'Metal Tone': {}, 'Auto · Crunch': { generatedBy: 'tone_pack' } };
    const r = mergePack({ presets: {}, targets: {} }, presets, { targets: { od: 'Auto · Crunch', solo: 'Auto · Lead' } }, [], {},
        { preferred: { od: 'Main Lead', dist: 'Main Lead', solo: 'Metal Tone', bogus: 'Nope' } });
    assert.equal(r.ta.targets.od, 'Main Lead');
    assert.equal(r.ta.targets.dist, 'Main Lead');
    assert.equal(r.ta.targets.solo, 'Metal Tone');
    assert.equal(r.ta.targets.bogus, undefined);
});

test('packCats points Clean / Acoustic at the pack presets even when set to a lead (once, migration 3)', () => {
    const pk = { presets: { 'Auto · Clean': { nativePreset: 'c' }, 'Auto · Acoustic': { nativePreset: 'a' } },
        targets: { clean: 'Auto · Clean', acoustic: 'Auto · Acoustic' } };
    const ta = { targets: { clean: 'Main Lead', acoustic: 'Main Lead', od: 'Main Lead', idle: 'Main Lead' } };
    const r = mergePack(pk, { 'Main Lead': { nativePreset: 'L' } }, ta, [], {}, { packCats: ['clean', 'acoustic'] });
    assert.equal(r.ta.targets.clean, 'Auto · Clean');
    assert.equal(r.ta.targets.acoustic, 'Auto · Acoustic');
    assert.equal(r.ta.targets.od, 'Main Lead', 'other categories untouched');
    const r2 = mergePack(pk, { 'Main Lead': { nativePreset: 'L' } }, { targets: { clean: 'Main Lead' } }, [], {}, {});
    assert.equal(r2.ta.targets.clean, 'Main Lead', 'without the flag a user target stays');
});

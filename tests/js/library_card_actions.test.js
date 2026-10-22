'use strict';
const { test } = require('node:test');
const assert = require('node:assert');
const reg = require('../../static/capabilities/library-card-actions.js');

function freshIds() { reg.snapshot().actions.forEach((a) => reg.unregister(a.id)); }

test('register + list returns applicable actions sorted by order', () => {
    freshIds();
    reg.register({ id: 'b', label: 'B', order: 20, run() {} });
    reg.register({ id: 'a', label: 'A', order: 10, run() {} });
    const ids = reg.list({ filename: 'x.archive' }).map((a) => a.id);
    assert.deepStrictEqual(ids, ['a', 'b']);
});

test('applies() filters out non-applicable actions', () => {
    freshIds();
    reg.register({ id: 'bassonly', label: 'Bass', applies: (s) => s.format === 'sloppak', run() {} });
    assert.strictEqual(reg.list({ filename: 'x.archive', format: 'archive' }).length, 0);
    assert.strictEqual(reg.list({ filename: 'y.sloppak', format: 'sloppak' }).length, 1);
});

test('enabled() reflected in the summary but action still listed', () => {
    freshIds();
    reg.register({ id: 'maybe', label: 'Maybe', enabled: (s) => !!s.allow, run() {} });
    const off = reg.list({ filename: 'x' })[0];
    assert.strictEqual(off.enabled, false);
    const on = reg.list({ filename: 'x', allow: true })[0];
    assert.strictEqual(on.enabled, true);
});

test('run() invokes the handler and reports handled', async () => {
    freshIds();
    let got = null;
    reg.register({ id: 'go', label: 'Go', run: (song) => { got = song.filename; return 'done'; } });
    const r = await reg.run('go', { filename: 'song.archive' }, {});
    assert.strictEqual(r.ok, true);
    assert.strictEqual(r.outcome, 'handled');
    assert.strictEqual(got, 'song.archive');
});

test('run() of a disabled / non-applicable / unknown action does not throw', async () => {
    freshIds();
    reg.register({ id: 'dis', label: 'Dis', enabled: () => false, run() { throw new Error('should not run'); } });
    assert.strictEqual((await reg.run('dis', {})).outcome, 'disabled');
    assert.strictEqual((await reg.run('nope', {})).outcome, 'no-action');
});

test('run() surfaces handler errors as failed (no throw)', async () => {
    freshIds();
    reg.register({ id: 'boom', label: 'Boom', run() { throw new Error('kaboom'); } });
    const r = await reg.run('boom', {});
    assert.strictEqual(r.ok, false);
    assert.strictEqual(r.outcome, 'failed');
});

test('unregister removes the action', () => {
    freshIds();
    const off = reg.register({ id: 'temp', label: 'T', run() {} });
    assert.strictEqual(reg.list({}).length, 1);
    off();
    assert.strictEqual(reg.list({}).length, 0);
});

test('bad specs are rejected (no id / no run)', () => {
    freshIds();
    reg.register({ label: 'no id', run() {} });
    reg.register({ id: 'no-run' });
    assert.strictEqual(reg.list({}).length, 0);
});

test("'play-as' placement keeps group and hint", () => {
    freshIds();
    reg.register({ id: 'pa', label: 'Drums', placement: 'play-as', group: 'Controller', hint: 'kit or pad', run() {} });
    const a = reg.list({ filename: 'x' })[0];
    assert.strictEqual(a.placement, 'play-as');
    assert.strictEqual(a.group, 'Controller');
    assert.strictEqual(a.hint, 'kit or pad');
    assert.strictEqual(a.variants, undefined);
});

test('variants: listed per song, empty or throwing variants hide the action', () => {
    freshIds();
    reg.register({
        id: 'ff', label: '5-Fret', placement: 'play-as',
        variants: (s) => (s.parts || []).map((p) => ({ key: p, label: '5-Fret ' + p, enabled: p !== 'bass' })),
        run() {},
    });
    reg.register({ id: 'bad', label: 'Bad', placement: 'play-as', variants() { throw new Error('x'); }, run() {} });
    const a = reg.list({ parts: ['guitar', 'guitar', 'bass'] });
    assert.deepStrictEqual(a.map((x) => x.id), ['ff']);
    assert.deepStrictEqual(a[0].variants, [
        { key: 'guitar', label: '5-Fret guitar', hint: '', enabled: true },
        { key: 'bass', label: '5-Fret bass', hint: '', enabled: false },
    ]);
    assert.strictEqual(reg.list({ parts: [] }).length, 0);
});

test('run() passes the chosen variant and refuses one the song does not offer', async () => {
    freshIds();
    let got = null;
    reg.register({
        id: 'ff', label: '5-Fret', placement: 'play-as',
        variants: () => [{ key: 'guitar', label: 'G' }, { key: 'bass', label: 'B', enabled: false }],
        run: (song, ctx) => { got = ctx.variant; },
    });
    assert.strictEqual((await reg.run('ff', {}, { variant: 'guitar' })).outcome, 'handled');
    assert.strictEqual(got, 'guitar');
    assert.strictEqual((await reg.run('ff', {}, { variant: 'rhythm' })).outcome, 'not-applicable');
    assert.strictEqual((await reg.run('ff', {}, { variant: 'bass' })).outcome, 'disabled');
    assert.strictEqual((await reg.run('ff', {}, {})).outcome, 'not-applicable');
});

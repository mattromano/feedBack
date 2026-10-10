// Library practice badges (feedBack#49): the REAL accuracyBadge from
// static/v3/songs.js now also paints the play count (/api/stats/plays) and a
// full-combo marker, independently of the accuracy pill, and the sort menu
// offers "Most played".

'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const SRC = fs.readFileSync(path.join(__dirname, '..', '..', 'static', 'v3', 'songs.js'), 'utf8');

function extractFunctionSource(src, name) {
    const start = src.indexOf(`function ${name}(`);
    assert.ok(start !== -1, `function '${name}' not found in songs.js`);
    let depth = 0;
    for (let i = src.indexOf('{', start); i < src.length; i++) {
        if (src[i] === '{') depth++;
        else if (src[i] === '}' && --depth === 0) return src.slice(start, i + 1);
    }
    throw new Error(`unbalanced braces in '${name}'`);
}

function badge(accuracy, plays) {
    const sandbox = { state: { accuracy, plays }, MASTERY_ACCURACY: 0.9 };
    vm.createContext(sandbox);
    vm.runInContext(extractFunctionSource(SRC, 'accuracyBadge') + '\nglobalThis.__b = accuracyBadge;', sandbox);
    return sandbox.__b;
}

test('no stats → no badge (an untouched card stays byte-identical)', () => {
    const b = badge({}, {});
    assert.equal(b('x.sloppak'), '');
    assert.equal(b('x.sloppak', 'tree'), '');
});

test('played but unscored → a neutral pill with just the count', () => {
    const b = badge({}, { 'p.sloppak': [7, 0] });
    const grid = b('p.sloppak');
    assert.match(grid, /fb-acc-badge/);
    assert.match(grid, /bg-black\/70/);
    assert.match(grid, /▶ 7/);
    assert.match(grid, /Played 7 times/);
    assert.doesNotMatch(grid, /%/);
    assert.doesNotMatch(grid, />FC</);
    assert.match(b('p.sloppak', 'tree'), /▶ 7/);
});

test('scored + played + full combo → count, accuracy and FC in one badge', () => {
    const b = badge({ 'fc.sloppak': 1 }, { 'fc.sloppak': [3, 2] });
    for (const variant of [undefined, 'tree']) {
        const html = b('fc.sloppak', variant);
        assert.equal((html.match(/fb-acc-badge/g) || []).length, 1, 'one repaintable badge');
        assert.match(html, /▶ 3/);
        assert.match(html, /100%/);
        assert.match(html, />FC</);
        assert.match(html, /2 full combos/);
    }
    assert.match(b('fc.sloppak'), /bg-fb-good\/90/);
});

test('scored with no play count yet (pre-#49 history) keeps the classic pill', () => {
    const b = badge({ 's.sloppak': 0.42 }, {});
    const html = b('s.sloppak');
    assert.match(html, /42%/);
    assert.match(html, /bg-fb-low/);
    assert.doesNotMatch(html, /▶/);
});

test('the sort menu offers Most played', () => {
    assert.match(SRC, /\['plays', 'Most played'\]/);
});

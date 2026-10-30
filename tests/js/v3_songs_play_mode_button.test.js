'use strict';
// Pins the library card's play-mode button in static/v3/songs.js (registry in
// static/v3/play-modes.js, feedBack.ui.playModeState): a registered mode adds a
// button to the card's chips row, outside the [data-v3-play] hitbox, hidden
// while picking or selecting; clicking it runs the mode, never a card play.
// Source checks plus a mirror (the v3_songs_pick_mode pattern) — keep the
// mirror in sync with songs.js.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const songsPath = path.join(__dirname, '..', '..', 'static', 'v3', 'songs.js');
const src = fs.readFileSync(songsPath, 'utf8');
const css = fs.readFileSync(path.join(__dirname, '..', '..', 'static', 'v3', 'v3.css'), 'utf8');
const { cardButtonsHtml } = require('../../static/v3/play-modes.js');

function fnBody(name) {
    const at = src.indexOf('function ' + name + '(');
    assert.ok(at >= 0, name + ' not found');
    const ends = ['\n    function ', '\n    async function ']
        .map((m) => src.indexOf(m, at + 10)).filter((i) => i >= 0);
    return src.slice(at, ends.length ? Math.min(...ends) : undefined);
}

// ── Source checks ────────────────────────────────────────────────────────────

const MODE_ACTS = 'const modeActs = (pms && window.v3PlayModes && !pickOn() && !state.selectMode) ? pms.cardActions(shown) : [];';
const MODE_BTNS = "const modeBtns = modeActs.length ? window.v3PlayModes.cardButtonsHtml(modeActs, esc) : '';";

test('songCard puts the mode buttons first in the chips row', () => {
    assert.ok(fnBody('songCard').includes(
        '\'<div class="v3-card-chips flex gap-1 mt-1">\' + modeBtns + chartsChip + drumChip + arrChips + \'</div>\''));
});

test('songCard guards cardActions(shown) with !pickOn() && !state.selectMode', () => {
    const body = fnBody('songCard');
    assert.ok(body.includes('const pms = playModeState();'));
    assert.ok(body.includes(MODE_ACTS), 'modeActs drifted from the mirror');
    assert.ok(body.includes(MODE_BTNS), 'modeBtns drifted from the mirror');
});

test('the button sits outside [data-v3-play]', () => {
    const body = fnBody('songCard');
    const ret = body.indexOf('return \'<div class="group relative"');
    assert.ok(ret >= 0);
    const hitboxClose = body.indexOf("'</div></div>'", ret);
    const btns = body.indexOf('modeBtns + chartsChip', ret);
    assert.ok(hitboxClose >= 0 && btns >= 0);
    assert.ok(btns > hitboxClose, 'mode buttons must come after the art hitbox closes');
});

test('wireCards runs the mode with the displayed chart and never plays', () => {
    const body = fnBody('wireCards');
    const at = body.indexOf("el.querySelectorAll('[data-play-mode-card]')");
    assert.ok(at >= 0, 'no [data-play-mode-card] wiring');
    const handler = body.slice(at, body.indexOf('}));', at));
    assert.match(handler, /e\.stopPropagation\(\);/);
    assert.match(handler, /e\.preventDefault\(\);/);
    assert.ok(handler.includes("runCardAction(b.getAttribute('data-play-mode-card'), playTarget)"));
    assert.doesNotMatch(handler, /playCard\(/);
});

test('_cardSig carries the play-modes epoch', () => {
    assert.ok(fnBody('_cardSig').includes("'m' + playModesEpoch"));
    assert.match(src, /let playModesEpoch = 0;/);
    assert.ok(src.includes('const playModeState = () => (sm && sm.ui && sm.ui.playModeState) || null;'));
});

test('ui:play-modes-changed bumps the epoch and re-renders the window', () => {
    const at = src.indexOf("sm.on('ui:play-modes-changed'");
    assert.ok(at >= 0, 'no ui:play-modes-changed listener');
    const handler = src.slice(at, src.indexOf('});', at));
    assert.match(handler, /playModesEpoch\+\+/);
    assert.match(handler, /state\.winRange = null;/);
    assert.match(handler, /requestWindowRender\(\)/);
});

test('a mode change while Songs is inactive still re-renders on re-entry (epoch tracked)', () => {
    assert.match(src, /renderedPlayModesEpoch: -1,/);
    const at = src.indexOf('const same = state.winRange');
    assert.ok(at >= 0, 'no `same` check');
    const same = src.slice(at, src.indexOf('if (same', at));
    assert.match(same, /state\.renderedPlayModesEpoch === playModesEpoch/);
    assert.match(src, /state\.renderedPlayModesEpoch = playModesEpoch;/);
    assert.match(src, /state\.renderedPlayModesEpoch = -1;/);
});

test('CRLF kept', () => {
    const raw = fs.readFileSync(songsPath);
    for (let i = 0; i < raw.length; i++) {
        if (raw[i] === 0x0a) assert.equal(raw[i - 1], 0x0d, 'bare LF at byte ' + i);
    }
});

test('v3.css has .v3-card-mode with height 1.25rem', () => {
    const m = css.match(/\.v3-card-mode\s*\{([^}]*)\}/);
    assert.ok(m, 'no .v3-card-mode rule');
    assert.match(m[1], /height:\s*1\.25rem/);
    assert.match(css, /\.v3-card-mode:hover\s*\{[^}]*background:\s*#0ea5e9/);
});

// ── Mirror of the songCard button decision ───────────────────────────────────

function harness({ pms = null, pick = false, select = false } = {}) {
    const state = { selectMode: select };
    const window = { v3PlayModes: { cardButtonsHtml } };
    const esc = (s) => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/"/g, '&quot;');
    const pickOn = () => pick;
    const playModeState = () => pms;
    return (song) => {
        const shown = song.display_chart ? Object.assign({}, song, song.display_chart) : song;
        const p = playModeState();
        // eslint-disable-next-line no-eval
        const modeActs = eval('(() => { const pms = p; ' + MODE_ACTS + ' return modeActs; })()');
        // eslint-disable-next-line no-eval
        return eval('(() => { ' + MODE_BTNS + ' return modeBtns; })()');
    };
}

function fakeState() {
    const calls = [];
    return {
        calls,
        cardActions(song) {
            calls.push(song);
            return [{ id: 'rockherosmith', label: 'RockHeroSmith', title: 'Play in RockHeroSmith' }];
        },
    };
}

const ROW = { filename: 'rep.sloppak', title: 'Rep', display_chart: { filename: 'member.sloppak', title: 'Member' } };

test('mirror: no play-mode state renders nothing', () => {
    assert.equal(harness()(ROW), '');
});

test('mirror: pick mode renders nothing and never asks the modes', () => {
    const pms = fakeState();
    assert.equal(harness({ pms, pick: true })(ROW), '');
    assert.equal(pms.calls.length, 0);
});

test('mirror: select mode renders nothing', () => {
    const pms = fakeState();
    assert.equal(harness({ pms, select: true })(ROW), '');
    assert.equal(pms.calls.length, 0);
});

test('mirror: an available mode renders its button', () => {
    const html = harness({ pms: fakeState() })(ROW);
    assert.ok(html.includes('data-play-mode-card="rockherosmith"'));
    assert.ok(html.includes('>RockHeroSmith</button>'));
});

test('mirror: cardActions gets the display_chart-merged row', () => {
    const pms = fakeState();
    harness({ pms })(ROW);
    assert.equal(pms.calls.length, 1);
    assert.equal(pms.calls[0].filename, 'member.sloppak');
    assert.equal(pms.calls[0].title, 'Member');
});

test('mirror: a mode with no actions for the song renders nothing', () => {
    const pms = { cardActions: () => [] };
    assert.equal(harness({ pms })(ROW), '');
});

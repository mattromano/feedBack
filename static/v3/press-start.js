/*
 * fee[dB]ack v0.3.0 — Press Start (gamepad:start-pressed).
 *
 * When a play mode asked for it (`startPress`), the standard Start button on a
 * free controller emits `gamepad:start-pressed { pad, id, controllerKey }` on
 * the bus. Never during a song (loading, playing or paused), on the player, a
 * plugin screen or FeedBarcade, under a visible modal, during a library pick,
 * or while any controller is claimed. One press gives one event: the watch
 * only reports changes (a held Start doesn't repeat) and a cooldown swallows
 * the same press on a mirror slot. Nothing is watched during a song.
 * Browser-global; also CommonJS-exported for node tests.
 */
(function (root) {
    'use strict';

    const START_BUTTON = 9;     // standard mapping: Start / Menu
    const COOLDOWN_MS = 1000;

    function isStartEligible(ctx) {
        if (!ctx) return false;
        const id = ctx.activeId;
        if (typeof id !== 'string' || !id) return false;
        if (id === 'player' || id === 'v3-feedbarcade' || id.startsWith('plugin-')) return false;
        if (ctx.songLoaded || ctx.modalOpen || ctx.picking) return false;
        return !(ctx.claimCount > 0);
    }

    /** A song is loading, playing or paused. Reads the src attribute: an emptied `.src` resolves to the page URL. */
    function songLoaded(doc, win, activeId) {
        if (activeId === 'player') return true;
        if (win && win.feedBack && win.feedBack.isPlaying === true) return true;
        const audio = doc && typeof doc.getElementById === 'function' ? doc.getElementById('audio') : null;
        if (audio && audio.getAttribute('src')) return true;
        return !!(win && win._juceAudioUrl);
    }

    /** A visible `[aria-modal="true"]` or `.feedBack-modal`; a mounted but hidden one doesn't count. */
    function anyVisibleModal(doc, win) {
        try {
            const nodes = doc.querySelectorAll('[aria-modal="true"], .feedBack-modal');
            for (let i = 0; i < nodes.length; i++) {
                const n = nodes[i];
                if (!n || n.hidden === true) continue;
                if (typeof n.closest === 'function' && n.closest('[hidden]')) continue;
                if (typeof n.getClientRects !== 'function' || n.getClientRects().length === 0) continue;
                if (win && typeof win.getComputedStyle === 'function') {
                    const cs = win.getComputedStyle(n);
                    if (cs && (cs.display === 'none' || cs.visibility === 'hidden')) continue;
                }
                return true;
            }
        } catch (_) { return false; }
        return false;
    }

    function createPressStart({ gamepadInput, emit, env, now }) {
        let unwatch = null;
        let last = -Infinity;

        function onWatch(ev) {
            if (!ev || ev.type !== 'button' || ev.index !== START_BUTTON || !ev.pressed) return;
            const p = gamepadInput.list().find((x) => x.index === ev.pad);
            if (!p || !p.connected || p.mapping !== 'standard' || p.claimedBy) return;
            if (now() - last < COOLDOWN_MS || !isStartEligible(env())) return;
            last = now();
            emit('gamepad:start-pressed', { pad: ev.pad, id: p.id, controllerKey: p.controllerKey });
        }

        function sync(wanted) {
            if (wanted && !unwatch && typeof gamepadInput.watch === 'function') {
                unwatch = gamepadInput.watch(onWatch);
            } else if (!wanted && unwatch) {
                const off = unwatch;
                unwatch = null;
                off();
            }
        }

        return { sync, onWatch, watching: () => !!unwatch };
    }

    const api = { START_BUTTON, COOLDOWN_MS, isStartEligible, songLoaded, anyVisibleModal, createPressStart };
    if (typeof module !== 'undefined' && module.exports) module.exports = api;

    const fb = root && root.feedBack;
    const gi = fb && fb.gamepadInput;
    const pms = fb && fb.ui && fb.ui.playModeState;
    if (!gi || !pms) return;

    const activeId = () => {
        const el = document.querySelector('.screen.active');
        return el ? el.id : '';
    };
    const inst = createPressStart({
        gamepadInput: gi,
        emit: (t, d) => fb.emit(t, d),
        env: () => {
            const id = activeId();
            return {
                activeId: id,
                songLoaded: songLoaded(document, root, id),
                modalOpen: anyVisibleModal(document, root),
                claimCount: gi.claimCount(),
                picking: !!(fb.library && fb.library.pickState && fb.library.pickState.session()),
            };
        },
        now: () => performance.now(),
    });
    const resync = () => inst.sync(pms.startPressWanted() && !songLoaded(document, root, activeId()));
    ['ui:play-modes-changed', 'screen:changed', 'song:stop', 'song:ended'].forEach((t) => fb.on(t, resync));
    fb.on('song:loading', () => inst.sync(false));
    resync();
})(typeof window !== 'undefined' ? window : null);

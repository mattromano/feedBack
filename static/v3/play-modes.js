/*
 * fee[dB]ack v0.3.0 — the play-mode registry (feedBack.ui.registerPlayMode).
 *
 * A plugin registers a way to play: the Home hero gets a second half, library
 * cards get a button, Press Start can launch it, and the tour can mention it.
 * With no mode registered nothing changes. songs.js / dashboard.js read the
 * registry through `feedBack.ui.playModeState` (core-internal) and render with
 * `window.v3PlayModes`. Browser-global; also CommonJS-exported for node tests.
 */
(function (root) {
    'use strict';

    const isStr = (v) => typeof v === 'string';
    const isFn = (v) => typeof v === 'function';

    /** Validate and default a plugin's spec; `null` when it has no id or label. */
    function normalizeMode(spec) {
        if (!spec || !isStr(spec.id) || !spec.id || !isStr(spec.label) || !spec.label) return null;
        const label = spec.label;
        const t = spec.tour;
        const ca = spec.cardAction;
        return {
            id: spec.id,
            label,
            startLabel: isStr(spec.startLabel) ? spec.startLabel : 'Play ' + label,
            order: typeof spec.order === 'number' && Number.isFinite(spec.order) ? spec.order : 100,
            renderHero: isFn(spec.renderHero) ? spec.renderHero : null,
            start: isFn(spec.start) ? spec.start : null,
            startPress: spec.startPress === true,
            tour: t && isStr(t.title) && isStr(t.content) ? { title: t.title, content: t.content } : null,
            cardAction: ca && isFn(ca.run) ? {
                label: isStr(ca.label) ? ca.label : label,
                title: isStr(ca.title) ? ca.title : 'Play with ' + label,
                isAvailable: isFn(ca.isAvailable) ? ca.isAvailable : () => false,
                run: ca.run,
            } : null,
        };
    }

    function createPlayModes(deps) {
        const emit = deps && isFn(deps.emit) ? deps.emit : () => {};
        const modes = new Map();

        function list() {
            return Array.from(modes.values()).sort((a, b) =>
                a.order - b.order || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0));
        }
        function changed() { emit('ui:play-modes-changed', { ids: list().map((m) => m.id) }); }

        function register(spec) {
            const mode = normalizeMode(spec);
            if (!mode) return () => {};
            if (modes.has(mode.id)) {
                console.warn('[play-modes] duplicate play mode ignored:', mode.id);
                return () => {};
            }
            modes.set(mode.id, mode);
            changed();
            return function unregister() {
                if (modes.get(mode.id) !== mode) return;
                modes.delete(mode.id);
                changed();
            };
        }

        function heroMode() {
            return list().find((m) => m.renderHero && m.start) || null;
        }

        function cardActions(song) {
            const out = [];
            list().forEach((m) => {
                if (!m.cardAction) return;
                let ok = false;
                try { ok = m.cardAction.isAvailable(song) === true; } catch (e) { ok = false; }
                if (ok) out.push({ id: m.id, label: m.cardAction.label, title: m.cardAction.title });
            });
            return out;
        }

        function runCardAction(id, song) {
            const m = modes.get(id);
            if (!m || !m.cardAction) return false;
            try { m.cardAction.run(song); } catch (e) { console.warn('[play-modes] card action failed:', id, e); }
            return true;
        }

        function start(id) {
            const m = modes.get(id);
            if (!m || !m.start) return false;
            try { m.start(); } catch (e) { console.warn('[play-modes] start failed:', id, e); }
            return true;
        }

        function startPressWanted() {
            return list().some((m) => m.startPress);
        }

        return { register, list, heroMode, cardActions, runCardAction, start, startPressWanted };
    }

    /** The Home hero with the mode's half beside the solo hero; unchanged with no mode. */
    function splitHeroHtml(soloHtml, mode, esc) {
        if (!mode) return soloHtml;
        return '<div id="v3-hero-split" class="lg:col-span-2 v3-hero-split">' + soloHtml
            + '<div id="v3-hero-mode" class="v3-hero-mode" data-play-mode="' + esc(mode.id) + '">'
            + '<div class="v3-hero-mode-art" data-play-mode-art></div>'
            + '<button type="button" id="v3-play-mode-start" class="v3-hero-mode-start" data-play-mode-start="'
            + esc(mode.id) + '">' + esc(mode.startLabel) + '</button></div></div>';
    }

    /** The per-card mode buttons for the chips row. */
    function cardButtonsHtml(actions, esc) {
        return (actions || []).map((a) =>
            '<button type="button" class="v3-card-mode" data-play-mode-card="' + esc(a.id)
            + '" title="' + esc(a.title) + '" aria-label="' + esc(a.title) + '">' + esc(a.label) + '</button>'
        ).join('');
    }

    const api = { createPlayModes, normalizeMode, splitHeroHtml, cardButtonsHtml };
    if (typeof module !== 'undefined' && module.exports) module.exports = api;

    if (root && root.feedBack) {
        const inst = createPlayModes({ emit: (t, d) => root.feedBack.emit(t, d) });
        root.feedBack.ui = Object.assign(root.feedBack.ui || {}, {
            registerPlayMode: inst.register,
            playModes: inst.list,
            playModeState: inst,
        });
        root.v3PlayModes = { splitHeroHtml, cardButtonsHtml };
    }
})(typeof window !== 'undefined' ? window : null);

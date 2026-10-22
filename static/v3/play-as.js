/*
 * fee[dB]ack v0.3.0 — "Play as": every way to play one song.
 *
 * The Songs table opens this menu on a row click (Enter / double-click /
 * gamepad A still just play), and the card ⋮ menu offers it as "Play as…".
 * It lists the song's `placement: 'play-as'` actions from
 * window.feedBack.libraryCardActions (static/capabilities/library-card-actions.js),
 * so core and plugins contribute the same way:
 *
 *   core      Play (the default), Real instrument (one row per Lead / Rhythm /
 *             Bass / Combo / Keys arrangement)
 *   plugins   Drums, Vocals (karaoke), 5-Fret Guitar / Rhythm / Bass,
 *             RockHeroSmith, ... — each registers its own entry.
 *
 * feedBack.ui.playSongAs(filename, { arrangement?, viz? }) is how an entry
 * plays. A `viz` (e.g. 'five_fret', 'drums') is set for that play only: the
 * viewer's own pick comes back on the next play that isn't a Play-as one, so
 * a 5-fret play never sticks to later songs. `resetView` (the Real
 * instrument entries) also puts a non-guitar view back to Auto, so a guitar
 * part never lands on, say, the 5-Fret view; a plain Play keeps the
 * viewer's pick, as a card play does.
 *
 * Browser-global (window.v3PlayAs, feedBack.ui.playSongAs); the pure parts
 * are CommonJS-exported for node tests.
 */
(function (root) {
    'use strict';

    const PLACEMENT = 'play-as';
    // Views that draw a guitar part; anything else is an instrument / controller view.
    const GUITAR_VIEWS = ['auto', 'default', 'highway_3d', 'venue'];
    const FORCED_KEY = 'v3:play-as-viz';
    const REAL_PART = /\b(lead|rhythm|bass|combo|keys|piano)\b/i;

    /** The song's real-instrument arrangements: [{ index, name }] (none on a 5-fret-only pack). */
    function realArrangements(song) {
        if (!song || song.five_fret_only === true || !Array.isArray(song.arrangements)) return [];
        return song.arrangements
            .map((a, i) => (a && typeof a === 'object' ? { index: Number.isInteger(a.index) ? a.index : i, name: String(a.name || '') }
                : { index: i, name: String(a || '') }))
            .filter((a) => REAL_PART.test(a.name));
    }

    /**
     * The menu rows for list(song)'s play-as actions, in order:
     * [{ id, variant, label, hint, group, enabled, isDefault }].
     */
    function menuRows(actions) {
        const rows = [];
        (actions || []).filter((a) => a && a.placement === PLACEMENT).forEach((a) => {
            const base = { id: a.id, group: a.group || '', isDefault: a.id === 'core.play-as.play' };
            if (Array.isArray(a.variants)) {
                a.variants.forEach((v) => rows.push(Object.assign({}, base, {
                    variant: v.key, label: v.label, hint: v.hint || a.hint || '', enabled: a.enabled !== false && v.enabled !== false,
                })));
            } else {
                rows.push(Object.assign({}, base, { variant: null, label: a.label, hint: a.hint || '', enabled: a.enabled !== false }));
            }
        });
        return rows;
    }

    /**
     * The per-play viz rule. deps: { current() -> viz id, setViz(id), load() -> state|null, save(state|null) }.
     * State { forced, prev }: `forced` was set by a Play-as entry over the viewer's `prev`.
     */
    function createVizKeeper(deps) {
        let ownPlay = null;   // the filename of the Play-as play under way

        function restore() {
            const st = deps.load();
            if (!st) return;
            deps.save(null);
            // The viewer picked another view meanwhile: theirs stands.
            if (deps.current() === st.forced) deps.setViz(st.prev);
        }

        /** Before a Play-as play of `filename` (as passed to playSong). */
        function beforePlay(filename, viz, resetView) {
            ownPlay = filename;
            if (viz) {
                const st = deps.load();
                const cur = deps.current();
                if (st && cur === st.forced) {
                    if (viz !== cur) { deps.save({ forced: viz, prev: st.prev }); deps.setViz(viz); }
                    return;
                }
                if (cur === viz) { deps.save(null); return; }   // the viewer's own pick: nothing to undo
                deps.save({ forced: viz, prev: cur });
                deps.setViz(viz);
                return;
            }
            restore();
            if (resetView && !GUITAR_VIEWS.includes(deps.current())) deps.setViz('auto');
        }

        /** song:loading: any play but ours ends a forced view. */
        function onSongLoading(filename) {
            if (ownPlay !== null && filename === ownPlay) { ownPlay = null; return; }
            ownPlay = null;
            restore();
        }

        return { beforePlay, onSongLoading };
    }

    const api = { realArrangements, menuRows, createVizKeeper, GUITAR_VIEWS, PLACEMENT };
    if (typeof module !== 'undefined' && module.exports) module.exports = api;
    if (!root || !root.document) return;

    const w = root;
    const sm = w.feedBack = w.feedBack || {};
    const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => (
        { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

    // ── Viz for one play ────────────────────────────────────────────────────
    function currentViz() {
        const sel = w.document.getElementById('viz-picker');
        if (sel && sel.value) return sel.value;
        try { return w.localStorage.getItem('vizSelection') || 'auto'; } catch (_) { return 'auto'; }
    }
    const keeper = createVizKeeper({
        current: currentViz,
        setViz: (id) => { if (typeof w.setViz === 'function') { try { w.setViz(id); } catch (e) { console.warn('[play-as] setViz failed:', id, e); } } },
        load: () => {
            try {
                const v = JSON.parse(w.localStorage.getItem(FORCED_KEY) || 'null');
                return v && typeof v.forced === 'string' && typeof v.prev === 'string' ? v : null;
            } catch (_) { return null; }
        },
        save: (st) => {
            try { if (st) w.localStorage.setItem(FORCED_KEY, JSON.stringify(st)); else w.localStorage.removeItem(FORCED_KEY); } catch (_) { /* */ }
        },
    });
    if (typeof sm.on === 'function') {
        sm.on('song:loading', (e) => keeper.onSongLoading(e && e.detail ? e.detail.filename : null));
    }

    /** Play `filename` (unencoded) as one part with one view: opts { arrangement?, viz?, resetView? }; see the header. */
    function playSongAs(filename, opts) {
        if (!filename || typeof w.playSong !== 'function') return false;
        const o = opts || {};
        const target = encodeURIComponent(filename);
        keeper.beforePlay(target, typeof o.viz === 'string' && o.viz ? o.viz : null, o.resetView === true);
        if (Number.isInteger(o.arrangement)) w.playSong(target, o.arrangement);
        else w.playSong(target);
        return true;
    }
    sm.ui = Object.assign(sm.ui || {}, { playSongAs });

    // ── Core entries ────────────────────────────────────────────────────────
    const reg = sm.libraryCardActions;
    const hooks = w.__feedBackV3PlayAs || (w.__feedBackV3PlayAs = {});
    if (reg && !hooks.installed) {
        hooks.installed = true;
        reg.register({
            id: 'core.play-as.play', pluginId: 'core', label: 'Play', placement: PLACEMENT, order: 0,
            applies: (song) => !!(song && song.filename),
            run: (song) => playSongAs(song.filename),
        });
        reg.register({
            id: 'core.play-as.real', pluginId: 'core', label: 'Real instrument', group: 'Real instrument',
            placement: PLACEMENT, order: 10,
            applies: (song) => !!(song && song.filename),
            variants: (song) => realArrangements(song).map((a) => ({ key: String(a.index), label: a.name })),
            run: (song, ctx) => playSongAs(song.filename, { arrangement: parseInt(ctx.variant, 10), resetView: true }),
        });
    }

    // ── The menu ────────────────────────────────────────────────────────────
    let openMenu = null;   // { el, close }

    function rowsFor(song) {
        const r = sm.libraryCardActions;
        return r && typeof r.list === 'function' ? menuRows(r.list(song)) : [];
    }

    function close() { if (openMenu) openMenu.close(); }

    /**
     * Open the menu for `song` (a library item: filename, title, artist,
     * arrangements, has_drum_tab, has_vocal_pitch, five_fret_parts, ...).
     * opts: { pos: {x, y} (viewport) | anchor: Element, beforeRun(row)?, returnFocus?: Element }.
     */
    function open(song, opts) {
        close();
        const o = opts || {};
        const rows = rowsFor(song);
        if (!rows.length) return null;
        const doc = w.document;
        const el = doc.createElement('div');
        el.className = 'v3-play-as';
        el.setAttribute('role', 'dialog');
        el.setAttribute('aria-modal', 'true');
        el.setAttribute('aria-label', 'Play as');
        let html = '<div class="v3-play-as-title">Play <span class="text-fb-text">' + esc(song.title || song.filename)
            + '</span>' + (song.artist ? ' · ' + esc(song.artist) : '') + ' as</div>';
        let group = '';
        rows.forEach((r, i) => {
            if (r.group && r.group !== group) html += '<div class="v3-play-as-group">' + esc(r.group) + '</div>';
            group = r.group || group;
            html += '<button type="button" data-row="' + i + '"' + (r.isDefault ? ' data-default' : '')
                + (r.enabled ? '' : ' disabled') + '>' + esc(r.label)
                + (r.hint ? '<span class="v3-play-as-hint">' + esc(r.hint) + '</span>' : '')
                + (r.isDefault ? '<span class="v3-play-as-hint">Enter</span>' : '') + '</button>';
        });
        el.innerHTML = html;
        doc.body.appendChild(el);

        // Place at the pointer, else under the anchor; kept on screen.
        let x = 16, y = 16;
        // Just off the pointer: a double-click's second press must land on
        // the row (it closes the menu and the row's dblclick plays).
        if (o.pos && Number.isFinite(o.pos.x)) { x = o.pos.x + 8; y = o.pos.y + 8; }
        else if (o.anchor && o.anchor.getBoundingClientRect) { const b = o.anchor.getBoundingClientRect(); x = b.left + 24; y = b.bottom; }
        const vw = w.innerWidth, vh = w.innerHeight;
        el.style.left = Math.max(8, Math.min(x, vw - el.offsetWidth - 8)) + 'px';
        el.style.top = Math.max(8, Math.min(y, vh - el.offsetHeight - 8)) + 'px';

        const buttons = () => Array.from(el.querySelectorAll('button:not([disabled])'));
        const focusAt = (i) => { const b = buttons(); if (b.length) b[(i + b.length) % b.length].focus(); };
        const returnTo = o.returnFocus || o.anchor || null;

        function onKey(e) {
            if (!el.isConnected) return;
            const b = buttons();
            const at = b.indexOf(doc.activeElement);
            if (e.key === 'Escape') {
                e.preventDefault(); e.stopPropagation();
                finish(true);
            } else if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
                e.preventDefault(); e.stopPropagation();
                focusAt(at < 0 ? 0 : at + (e.key === 'ArrowDown' ? 1 : -1));
            } else if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {
                // The screen's own grid nav must not move focus out of the menu.
                e.preventDefault(); e.stopPropagation();
            } else if (e.key === 'Home' || e.key === 'End') {
                e.preventDefault(); e.stopPropagation();
                focusAt(e.key === 'Home' ? 0 : b.length - 1);
            } else if (e.key === 'Tab') {
                e.preventDefault();
                focusAt(at + (e.shiftKey ? -1 : 1));
            }
        }
        function onDown(e) { if (!el.contains(e.target)) finish(false); }
        // Focus leaving the menu, another screen or a song starting closes it.
        function onFocusOut(e) { if (e.relatedTarget && !el.contains(e.relatedTarget)) finish(false); }
        const offBus = [];
        if (typeof sm.on === 'function' && typeof sm.off === 'function') {
            ['screen:changed', 'song:loading'].forEach((t) => {
                const fn = () => finish(false);
                sm.on(t, fn);
                offBus.push(() => sm.off(t, fn));
            });
        }
        function finish(refocus) {
            if (!openMenu || openMenu.el !== el) return;
            openMenu = null;
            doc.removeEventListener('keydown', onKey, true);
            doc.removeEventListener('mousedown', onDown, true);
            w.removeEventListener('resize', close);
            el.removeEventListener('focusout', onFocusOut);
            offBus.forEach((off) => off());
            el.remove();
            if (refocus && returnTo && returnTo.isConnected && typeof returnTo.focus === 'function') returnTo.focus();
        }
        el.addEventListener('click', (e) => {
            const btn = e.target.closest('button[data-row]');
            if (!btn || btn.disabled) return;
            const row = rows[Number(btn.getAttribute('data-row'))];
            finish(false);
            if (typeof o.beforeRun === 'function') { try { o.beforeRun(row); } catch (_) { /* */ } }
            const r = sm.libraryCardActions;
            if (!r) return;
            const ctx = { source: 'v3-play-as' };
            if (row.variant != null) ctx.variant = row.variant;
            Promise.resolve(r.run(row.id, song, ctx)).then((res) => {
                if (res && !res.ok) console.warn('[play-as]', row.id, res.outcome, res.error || '');
            });
        });
        doc.addEventListener('keydown', onKey, true);
        doc.addEventListener('mousedown', onDown, true);
        w.addEventListener('resize', close);
        el.addEventListener('focusout', onFocusOut);
        openMenu = { el, close: () => finish(false) };
        focusAt(0);
        return el;
    }

    w.v3PlayAs = { open, close, rows: rowsFor, isOpen: () => !!openMenu };
})(typeof window !== 'undefined' ? window : null);

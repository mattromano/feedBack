/*
 * fee[dB]ack v0.3.0 — the library pick session (feedBack.library.pick).
 *
 * A plugin calls `feedBack.library.pick(opts)` to send the user to the Songs
 * screen in pick mode: cards select instead of playing, the plugin's filter
 * chips narrow the grid, and "Use this song" / Back / Esc / leaving the screen
 * end the session. The promise resolves `{ item, filters, reason }` and never
 * rejects. songs.js reads the session through `feedBack.library.pickState`
 * (core-internal). Browser-global; also CommonJS-exported for node tests.
 */
(function (root) {
    'use strict';

    const SONGS_SCREEN = 'v3-songs';

    /** Union the `params` of the chips that are on; a repeated key's comma
     *  lists are joined in first-seen order without duplicates. */
    function mergeParams(filters) {
        const out = {};
        (filters || []).forEach((f) => {
            if (!f || f.on !== true || !f.params) return;
            Object.keys(f.params).forEach((key) => {
                const val = f.params[key];
                if (typeof val !== 'string') return;
                if (!(key in out)) { out[key] = val; return; }
                const seen = [];
                out[key].split(',').concat(val.split(',')).forEach((part) => {
                    if (!seen.includes(part)) seen.push(part);
                });
                out[key] = seen.join(',');
            });
        });
        return out;
    }

    /** Esc on the Songs screen means Back, unless something else owns it. */
    function isPickBackKey(e, doc, activeId) {
        if (!e || e.key !== 'Escape') return false;
        if (e.ctrlKey || e.metaKey || e.altKey || e.shiftKey) return false;
        if (e.defaultPrevented) return false;
        if (activeId !== SONGS_SCREEN) return false;
        const t = e.target;
        if (t) {
            const tag = String(t.tagName || '').toUpperCase();
            if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return false;
            if (t.isContentEditable) return false;
        }
        if (!doc) return true;
        const overlay = doc.getElementById('v3-songs-overlay');
        if (overlay && !(overlay.classList && overlay.classList.contains('hidden'))) return false;
        if (doc.querySelector('[role="dialog"][aria-modal="true"], .feedBack-modal') !== null) return false;
        return true;
    }

    function normaliseFilters(list) {
        if (!Array.isArray(list)) return [];
        return list
            .filter((f) => f && typeof f.id === 'string' && typeof f.label === 'string')
            .map((f) => ({
                id: f.id,
                label: f.label,
                on: !!f.on,
                params: (f.params && typeof f.params === 'object') ? Object.assign({}, f.params) : {},
            }));
    }

    function createLibraryPick({ emit, showScreen, songsScreen = SONGS_SCREEN } = {}) {
        let current = null;

        function fire(active) {
            if (typeof emit !== 'function') return;
            try { emit('library:pick-changed', { active }); } catch (_) { /* a listener's throw isn't ours */ }
        }

        function go(id) {
            if (typeof showScreen !== 'function') return;
            try { showScreen(id); } catch (_) { /* navigation failure leaves the session state as is */ }
        }

        function end(reason) {
            const s = current;
            if (!s) return;
            current = null;
            const filters = {};
            s.filters.forEach((f) => { filters[f.id] = f.on; });
            s.resolve({ item: reason === 'picked' ? s.selected : null, filters, reason });
            fire(false);
        }

        function pick(opts) {
            const o = opts || {};
            if (current) end('replaced');
            let resolve;
            const promise = new Promise((r) => { resolve = r; });
            current = {
                title: typeof o.title === 'string' ? o.title : '',
                confirmLabel: typeof o.confirmLabel === 'string' ? o.confirmLabel : 'Use this song',
                backLabel: typeof o.backLabel === 'string' ? o.backLabel : 'Back',
                returnTo: o.returnTo || null,
                filters: normaliseFilters(o.filters),
                selected: (o.selected && typeof o.selected.filename === 'string') ? Object.assign({}, o.selected) : null,
                resolve,
            };
            fire(true);
            go(songsScreen);
            return promise;
        }

        function session() {
            if (!current) return null;
            return {
                title: current.title,
                confirmLabel: current.confirmLabel,
                backLabel: current.backLabel,
                filters: current.filters.map((f) => ({ id: f.id, label: f.label, on: f.on })),
                selected: current.selected ? Object.assign({}, current.selected) : null,
            };
        }

        function params() {
            return current ? mergeParams(current.filters) : {};
        }

        function select(item) {
            if (!current || !item || typeof item.filename !== 'string' || !item.filename) return false;
            current.selected = Object.assign({}, item);
            fire(true);
            return true;
        }

        function toggle(id) {
            if (!current) return false;
            const chip = current.filters.find((f) => f.id === id);
            if (!chip) return false;
            chip.on = !chip.on;
            fire(true);
            return true;
        }

        function finish(reason) {
            const returnTo = current.returnTo;
            end(reason);
            if (returnTo) go(returnTo);
            return true;
        }

        function confirm() {
            if (!current || !current.selected) return false;
            return finish('picked');
        }

        function back() {
            if (!current) return false;
            return finish('back');
        }

        function onScreenChanged(id) {
            if (current && id !== songsScreen) end('left');
        }

        function handleKey(e, doc) {
            if (!current) return false;
            const active = doc && typeof doc.querySelector === 'function' ? doc.querySelector('.screen.active') : null;
            if (!isPickBackKey(e, doc, active ? active.id : undefined)) return false;
            back();
            if (typeof e.preventDefault === 'function') e.preventDefault();
            return true;
        }

        return { pick, session, params, select, toggle, confirm, back, onScreenChanged, handleKey };
    }

    const api = { createLibraryPick, mergeParams, isPickBackKey };
    if (typeof module !== 'undefined' && module.exports) module.exports = api;

    if (root && root.feedBack) {
        const inst = createLibraryPick({
            emit: (t, d) => root.feedBack.emit(t, d),
            showScreen: (id) => root.showScreen && root.showScreen(id),
        });
        root.feedBack.library = Object.assign(root.feedBack.library || {}, { pick: inst.pick, pickState: inst });
        root.feedBack.on('screen:changed', (e) => inst.onScreenChanged(e && e.detail && e.detail.id));
        root.addEventListener('keydown', (e) => inst.handleKey(e, root.document), false);
    }
})(typeof window !== 'undefined' ? window : null);

/*
 * fee[dB]ack v0.3.0 — Songs table view (#v3-songs-table).
 *
 * A compact, sortable table of the whole (filtered) library: one dense row per
 * song with the practice columns the cards can't fit side by side — plays,
 * best accuracy, full combo, last played, time played — next to artist /
 * title / album / year / tuning / parts / length. Click a column header to
 * sort (again to reverse). Click a row for its "Play as" menu (opts.menu);
 * Enter, double-click or gamepad A plays it straight away (opts.play).
 *
 * Data comes from GET /api/library/table, which applies the SAME filters as the
 * grid (search, format, drawer, grouping) server-side and returns every match
 * in one columnar payload, so sorting is local and instant. Rows are
 * virtualised inside their own scroll box — only the visible window plus a
 * small overscan is in the DOM — so a 10k-song library stays light (see the
 * CLAUDE.md note on DOM-size-driven jank).
 *
 * songs.js owns the view switch and calls render(); this file owns nothing
 * else. Public: window.v3SongsTable = { render(host, opts), COLUMNS }.
 */
(function () {
    'use strict';

    const ROW_H = 30;
    const OVERSCAN = 12;
    const SORT_KEY = 'v3:songs-table-sort';
    const MASTERY_ACCURACY = 0.9;   // same green threshold as the card badge
    const LAST = '￿';          // sorts blank text after everything

    const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => (
        { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    const norm = (s) => String(s || '').toLowerCase().replace(/^the\s+/, '').replace(/[^a-z0-9]+/g, '');

    // Arrangement names → compact part letters, in a fixed instrument order.
    // A drum chart (has_drum_tab) is D and a vocal-pitch track V even when no
    // arrangement is named for them; a 5-fret-only pack's placeholder
    // arrangement is not a real part.
    const PART_ORDER = ['lead', 'rhythm', 'bass', 'combo', 'keys', 'drums', 'vocals'];
    function parts(names, facts) {
        const f = facts || {};
        const kinds = new Set();
        if (!f.fiveFretOnly) {
            for (const n of names || []) {
                const low = String(n).toLowerCase();
                const k = PART_ORDER.find((p) => low.includes(p)) || (low.includes('piano') ? 'keys' : '');
                if (k) kinds.add(k);
            }
        }
        if (f.drums) kinds.add('drums');
        if (f.vocals) kinds.add('vocals');
        return PART_ORDER.filter((k) => kinds.has(k)).map((k) => k[0].toUpperCase()).join('');
    }

    // 5-fret parts (["guitar:chart", "bass:auto", ...]) → pills in G R B order,
    // one per instrument (a real chart beats an auto one). Auto charts only
    // while the five-fret plugin's "Show auto charts" is on.
    const FF_ORDER = [['guitar', 'G', 'guitar'], ['rhythm', 'R', 'rhythm'], ['bass', 'B', 'bass']];
    function fiveFretPills(tags, showAuto) {
        const kind = {};
        for (const t of tags || []) {
            const [inst, src] = String(t).split(':');
            if (src === 'chart') kind[inst] = 'chart';
            else if (src === 'auto' && !kind[inst]) kind[inst] = 'auto';
        }
        return FF_ORDER.filter(([inst]) => kind[inst] === 'chart' || (kind[inst] === 'auto' && showAuto))
            .map(([inst, letter, name]) => ({ inst, letter, name, auto: kind[inst] === 'auto' }));
    }
    function showAutoCharts() {
        const ff = window.feedBackFiveFret;
        try { return !!(ff && typeof ff.showAutoCharts === 'function' && ff.showAutoCharts()); } catch (_) { return false; }
    }

    // [key, header, grid width, align, sort accessor, first-click direction]
    const COLUMNS = [
        ['fc', 'FC', '34px', 'center', (r) => r.fcs, -1],
        ['artist', 'Artist', 'minmax(110px,1.1fr)', 'left', (r) => r.artistSort || LAST, 1],
        ['title', 'Title', 'minmax(140px,1.5fr)', 'left', (r) => r.titleSort || LAST, 1],
        ['album', 'Album', 'minmax(100px,1fr)', 'left', (r) => r.albumSort || LAST, 1],
        ['year', 'Year', '52px', 'right', (r) => r.yearNum, -1],
        ['tuning', 'Tuning', '104px', 'left', (r) => r.tuning || LAST, 1],
        ['parts', 'Parts', '104px', 'left', (r) => r.partsKey || LAST, 1],
        ['duration', 'Length', '58px', 'right', (r) => r.duration, -1],
        ['plays', 'Plays', '56px', 'right', (r) => r.plays, -1],
        ['best', 'Best', '58px', 'right', (r) => (r.best == null ? -1 : r.best), -1],
        ['last', 'Last played', '96px', 'right', (r) => r.lastMs, -1],
        ['time', 'Time', '56px', 'right', (r) => r.seconds, -1],
    ];
    const GRID = COLUMNS.map((c) => c[2]).join(' ');

    function loadSort() {
        try {
            const v = JSON.parse(localStorage.getItem(SORT_KEY) || 'null');
            if (v && COLUMNS.some((c) => c[0] === v.key) && (v.dir === 1 || v.dir === -1)) return v;
        } catch (_) { /* private mode / bad JSON */ }
        return { key: 'artist', dir: 1 };
    }
    function saveSort(s) { try { localStorage.setItem(SORT_KEY, JSON.stringify(s)); } catch (_) { /* */ } }

    // Server last_played_at is SQLite UTC ('YYYY-MM-DD HH:MM:SS.fff').
    function parseUtc(ts) {
        if (!ts) return 0;
        const t = Date.parse(String(ts).replace(' ', 'T') + 'Z');
        return Number.isFinite(t) ? t : 0;
    }

    // The 5-fret pills and the Parts sort key (pills after the real letters).
    function setPills(r, showAuto) {
        r.ff = fiveFretPills(r.song.five_fret_parts, showAuto);
        r.partsKey = r.parts + (r.ff.length ? ' ' + r.ff.map((p) => p.letter).join('') : '');
    }

    // Columnar payload → row objects with precomputed sort keys. `r.song` is
    // the library item the "Play as" menu and plugins are asked about.
    function toRows(data, showAuto) {
        const ix = {};
        (data.cols || []).forEach((c, i) => { ix[c] = i; });
        return (data.rows || []).map((a) => {
            const r = {
                filename: a[ix.filename], playFilename: a[ix.play_filename] || a[ix.filename],
                artist: a[ix.artist] || '', title: a[ix.title] || '', album: a[ix.album] || '',
                year: a[ix.year] || '', duration: a[ix.duration] || 0, tuning: a[ix.tuning] || '',
                arrangements: a[ix.arrangements] || [], format: a[ix.format] || '',
                plays: a[ix.plays] || 0, best: a[ix.best_accuracy], fcs: a[ix.full_combos] || 0,
                last: a[ix.last_played] || null, seconds: a[ix.seconds] || 0,
            };
            const idx = a[ix.arr_index] || [];
            r.song = {
                filename: r.playFilename, title: r.title, artist: r.artist, album: r.album,
                format: r.format, tuning: r.tuning,
                arrangements: r.arrangements.map((name, i) => ({ index: Number.isInteger(idx[i]) ? idx[i] : i, name })),
                has_drum_tab: a[ix.has_drum_tab] === true, has_vocal_pitch: a[ix.has_vocal_pitch] === true,
                five_fret_parts: a[ix.five_fret_parts] || [], five_fret_only: a[ix.five_fret_only] === true,
                stem_ids: a[ix.stem_ids] || [],
            };
            r.parts = parts(r.arrangements, {
                drums: r.song.has_drum_tab, vocals: r.song.has_vocal_pitch, fiveFretOnly: r.song.five_fret_only,
            });
            setPills(r, !!showAuto);
            r.artistSort = norm(r.artist);
            r.titleSort = norm(r.title);
            r.albumSort = norm(r.album);
            r.yearNum = parseInt(r.year, 10) || 0;
            r.lastMs = parseUtc(r.last);
            return r;
        });
    }

    function sortRows(rows, sort) {
        const col = COLUMNS.find((c) => c[0] === sort.key) || COLUMNS[1];
        const acc = col[4];
        // Artist → title tiebreak keeps equal keys (0 plays, same year…) in the
        // order the grid would show them; filename makes it total.
        return rows.slice().sort((a, b) => {
            const x = acc(a), y = acc(b);
            const c = x < y ? -1 : x > y ? 1 : 0;
            if (c) return c * sort.dir;
            const aa = a.artistSort || LAST, ba = b.artistSort || LAST;
            if (aa !== ba) return aa < ba ? -1 : 1;
            const at = a.titleSort || LAST, bt = b.titleSort || LAST;
            if (at !== bt) return at < bt ? -1 : 1;
            return a.filename < b.filename ? -1 : a.filename > b.filename ? 1 : 0;
        });
    }

    const fmtLen = (s) => (s ? Math.floor(s / 60) + ':' + String(Math.round(s) % 60).padStart(2, '0') : '');
    const fmtTime = (s) => (!s ? '' : s >= 3600 ? (s / 3600).toFixed(1) + 'h' : Math.max(1, Math.round(s / 60)) + 'm');
    function fmtLast(ms, now) {
        if (!ms) return '';
        const days = Math.floor((now - ms) / 86400000);
        if (days < 1) return 'today';
        if (days < 2) return 'yesterday';
        if (days < 31) return days + 'd ago';
        if (days < 365) return Math.round(days / 30) + 'mo ago';
        return (days / 365).toFixed(1) + 'y ago';
    }

    function cell(r, key, now) {
        switch (key) {
            case 'fc': return r.fcs ? '<span class="v3-table-fc" title="' + r.fcs + ' full combo' + (r.fcs === 1 ? '' : 's') + '">FC</span>' : '';
            case 'artist': return esc(r.artist);
            case 'title': return '<span class="text-fb-text">' + esc(r.title || r.filename) + '</span>';
            case 'album': return esc(r.album);
            case 'year': return esc(r.year);
            case 'tuning': return esc(r.tuning);
            case 'parts': {
                const ff = r.ff.map((p) => '<span class="v3-table-ff' + (p.auto ? ' v3-table-ff-auto' : '') + '" title="5-fret '
                    + p.name + (p.auto ? ' (auto chart)' : '') + '">' + p.letter + '</span>').join('');
                return '<span title="' + esc(r.arrangements.join(', ')) + '">' + esc(r.parts) + '</span>'
                    + (ff ? '<span class="v3-table-ffs" aria-label="5-fret parts">' + ff + '</span>' : '');
            }
            case 'duration': return fmtLen(r.duration);
            case 'plays': return r.plays ? String(r.plays) : '';
            case 'best': {
                if (r.best == null) return '';
                // Floor, never round: 100% must mean every note hit (card rule).
                const cls = r.best >= MASTERY_ACCURACY ? 'text-fb-good' : r.best >= 0.5 ? 'text-fb-mid' : 'text-fb-low';
                return '<span class="font-bold ' + cls + '">' + Math.floor(r.best * 100) + '%</span>';
            }
            case 'last': return r.lastMs ? '<span title="' + esc(new Date(r.lastMs).toLocaleString()) + '">' + fmtLast(r.lastMs, now) + '</span>' : '';
            case 'time': return fmtTime(r.seconds);
        }
        return '';
    }

    // One table instance per host; render() on the same host replaces its data
    // but keeps the scroll box (and the user's place when only data changed).
    const instances = new WeakMap();

    function mount(host, opts) {
        host.innerHTML =
            '<div class="v3-table-info flex items-center gap-3 text-xs text-fb-textDim mb-2">' +
            '<span class="v3-table-count text-fb-text font-semibold"></span>' +
            '<span class="v3-table-note"></span>' +
            '<span class="flex-1"></span><span>Click a column to sort · click a song for Play as · double-click or Enter plays</span></div>' +
            '<div class="v3-table-box" role="table" aria-label="Song library">' +
            '<div class="v3-table-head" role="row" style="grid-template-columns:' + GRID + '"></div>' +
            '<div class="v3-table-scroll"><div class="v3-table-spacer"></div></div></div>';
        const inst = {
            host, opts, rows: [], view: [], sort: loadSort(),
            info: host.querySelector('.v3-table-count'),
            note: host.querySelector('.v3-table-note'),
            head: host.querySelector('.v3-table-head'),
            scroll: host.querySelector('.v3-table-scroll'),
            spacer: host.querySelector('.v3-table-spacer'),
            raf: 0, lastKey: '',
        };
        inst.head.addEventListener('click', (e) => {
            const b = e.target.closest('[data-sort]');
            if (!b) return;
            const key = b.getAttribute('data-sort');
            const col = COLUMNS.find((c) => c[0] === key);
            inst.sort = key === inst.sort.key ? { key, dir: -inst.sort.dir } : { key, dir: col ? col[5] : 1 };
            saveSort(inst.sort);
            inst.scroll.scrollTop = 0;
            resort(inst);
        });
        inst.scroll.addEventListener('scroll', () => {
            if (inst.raf) return;
            inst.raf = requestAnimationFrame(() => { inst.raf = 0; paint(inst); });
        }, { passive: true });
        const rowOf = (rowEl) => rowEl && inst.view[Number(rowEl.getAttribute('data-i'))];
        const playRow = (rowEl) => {
            const r = rowOf(rowEl);
            if (r && typeof inst.opts.play === 'function') inst.opts.play(r);
        };
        // Click (the first of a double-click too) / right-click: the "Play as"
        // menu; without a menu handler a click plays, as before.
        const menuRow = (e, pos) => {
            const rowEl = e.target.closest('[data-i]');
            const r = rowOf(rowEl);
            if (!r) return;
            if (typeof inst.opts.menu === 'function') inst.opts.menu(r, rowEl, pos);
            else playRow(rowEl);
        };
        inst.spacer.addEventListener('click', (e) => { if (e.detail < 2) menuRow(e, { x: e.clientX, y: e.clientY }); });
        inst.spacer.addEventListener('dblclick', (e) => {
            if (typeof inst.opts.menu !== 'function') return;   // the click already played
            playRow(e.target.closest('[data-i]'));
        });
        inst.spacer.addEventListener('contextmenu', (e) => {
            if (!e.target.closest('[data-i]') || typeof inst.opts.menu !== 'function') return;
            e.preventDefault();
            menuRow(e, { x: e.clientX, y: e.clientY });
        });
        inst.spacer.addEventListener('keydown', (e) => {
            if (e.key === 'ContextMenu' && typeof inst.opts.menu === 'function') {
                const rowEl = e.target.closest('[data-i]');
                if (!rowEl) return;
                e.preventDefault();
                const b = rowEl.getBoundingClientRect();
                const r = rowOf(rowEl);
                if (r) inst.opts.menu(r, rowEl, { x: b.left + 24, y: b.bottom });
                return;
            }
            if (e.key !== 'Enter' && e.key !== ' ') return;
            const rowEl = e.target.closest('[data-i]');
            if (!rowEl) return;
            e.preventDefault();
            playRow(rowEl);
        });
        inst.onResize = () => { fitHeight(inst); paint(inst); };
        // The five-fret plugin's "Show auto charts" changed: re-pill and repaint.
        const bus = window.feedBack;
        if (bus && typeof bus.on === 'function') {
            bus.on('five_fret:settings-changed', () => {
                if (!host.isConnected || !inst.rows.length) return;
                const show = showAutoCharts();
                inst.rows.forEach((r) => setPills(r, show));
                resort(inst);
            });
        }
        window.addEventListener('resize', inst.onResize, { passive: true });
        instances.set(host, inst);
        return inst;
    }

    // The scroll box fills the viewport below it (the rows scroll, the header
    // and the library toolbar stay put).
    function fitHeight(inst) {
        if (!inst.host.isConnected) return;
        const top = inst.scroll.getBoundingClientRect().top;
        const h = Math.max(320, Math.floor(window.innerHeight - top - 16));
        inst.scroll.style.height = h + 'px';
    }

    function renderHead(inst) {
        inst.head.innerHTML = COLUMNS.map((c) => {
            const on = c[0] === inst.sort.key;
            const arrow = on ? (inst.sort.dir < 0 ? ' ▼' : ' ▲') : '';
            return '<button type="button" role="columnheader" data-sort="' + c[0] + '"' +
                ' aria-sort="' + (on ? (inst.sort.dir < 0 ? 'descending' : 'ascending') : 'none') + '"' +
                ' class="' + (on ? 'text-fb-text' : '') + '" style="text-align:' + c[3] + '">' + esc(c[1]) + arrow + '</button>';
        }).join('');
    }

    function resort(inst) {
        inst.view = sortRows(inst.rows, inst.sort);
        inst.spacer.style.height = (inst.view.length * ROW_H) + 'px';
        inst.lastKey = '';
        renderHead(inst);
        paint(inst);
    }

    function paint(inst) {
        const top = inst.scroll.scrollTop;
        const h = inst.scroll.clientHeight || 600;
        const first = Math.max(0, Math.floor(top / ROW_H) - OVERSCAN);
        const last = Math.min(inst.view.length, Math.ceil((top + h) / ROW_H) + OVERSCAN);
        const key = first + ':' + last;
        if (key === inst.lastKey) return;   // same window: nothing to redraw
        inst.lastKey = key;
        const now = Date.now();
        let html = '';
        for (let i = first; i < last; i++) {
            const r = inst.view[i];
            html += '<div class="v3-table-row" role="row" tabindex="0" data-i="' + i + '" style="top:' + (i * ROW_H) +
                'px;grid-template-columns:' + GRID + '" title="' + esc(r.artist + ' — ' + r.title) + '">' +
                COLUMNS.map((c) => '<div role="cell" style="text-align:' + c[3] + '">' + cell(r, c[0], now) + '</div>').join('') +
                '</div>';
        }
        inst.spacer.innerHTML = html;
    }

    async function render(host, opts) {
        if (!host) return;
        const prev = instances.get(host);
        const inst = (prev && host.contains(prev.scroll)) ? prev : mount(host, opts || {});
        inst.opts = opts || {};
        inst.info.textContent = 'Loading…';
        let data = null;
        try {
            const r = await fetch(inst.opts.url);
            data = r.ok ? await r.json() : null;
        } catch (_) { data = null; }
        if (!host.isConnected) return;
        if (!data) { inst.info.textContent = 'Could not load the library.'; return; }
        inst.rows = toRows(data, showAutoCharts());
        inst.info.textContent = inst.rows.length + ' song' + (inst.rows.length === 1 ? '' : 's');
        inst.note.textContent = data.truncated ? '(showing the first ' + inst.rows.length + ' of ' + data.total + ' — narrow the search to see the rest)' : '';
        fitHeight(inst);
        resort(inst);
    }

    window.v3SongsTable = { render, COLUMNS, _internals: { toRows, sortRows, parts, fiveFretPills, fmtLast, parseUtc } };
})();

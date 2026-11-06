/*
 * fee[dB]ack — Album Playthrough Mode.
 *
 * Plays a whole album in track order on one part (Lead / Rhythm / Bass / ...),
 * like playing the record front to back:
 *   - every track is pinned to the chosen part (tracks without it fall back to
 *     the server's default arrangement),
 *   - between tracks a "Next up" card counts down, with Play now / End,
 *   - after the last track a scorecard lists every song's accuracy.
 *
 * Built on window.feedBack.playQueue (the queue owns the advancing) and
 * feedBack.holdAutoExit() (we claim the song-end exit so the card can show,
 * and so the album keeps going even with the "auto-exit" setting off).
 * Accuracy comes from note:hit / note:miss (note_detect); songs played with
 * no detection show "—".
 *
 * Entry point: feedBack.albumPlaythrough.start({ album, artist, songs, part })
 * — called from the album page in songs.js. `songs` are /api/library rows.
 */
(function () {
    'use strict';
    const sm = window.feedBack;
    if (!sm) return;

    const NEXT_UP_SECONDS = 8;
    const TAG_PREFIX = 'Album playthrough: ';
    const esc = (v) => String(v == null ? '' : v).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

    // The active run, or null. results[i] = { title, hits, misses, played, finished }.
    let run = null;
    let cur = -1;          // index of the track now playing
    let overlay = null;    // the card on screen (Next up / scorecard)
    let tick = null;       // countdown interval
    let ending = false;    // between song:ended and the next song:loading

    const q = () => sm.playQueue || null;
    const isOurs = () => !!run && !!q() && q().active() && q().source() === run.tag;

    // ── Parts ────────────────────────────────────────────────────────────────
    // "Lead 2" / "Rhythm1" group under their base part name.
    const baseName = (n) => String(n || '').replace(/\s*\d+$/, '').trim();
    const ORDER = ['lead', 'rhythm', 'bass', 'combo'];

    // Parts offered for an album: every base arrangement name any track has,
    // in Lead / Rhythm / Bass order, with how many tracks have it.
    function partsFor(songs) {
        const counts = new Map();
        for (const s of songs || []) {
            const seen = new Set();
            for (const a of (s.arrangements || [])) {
                const b = baseName(a && a.name);
                if (!b || seen.has(b.toLowerCase())) continue;
                seen.add(b.toLowerCase());
                const k = b.toLowerCase();
                const prev = counts.get(k);
                counts.set(k, { name: prev ? prev.name : b, count: (prev ? prev.count : 0) + 1 });
            }
        }
        const rank = (k) => { const i = ORDER.indexOf(k); return i < 0 ? ORDER.length : i; };
        return [...counts.entries()]
            .sort((x, y) => rank(x[0]) - rank(y[0]) || x[0].localeCompare(y[0]))
            // Count what the playthrough will actually play on this part,
            // including the Lead/Rhythm → Combo fallback.
            .map(([, v]) => ({ name: v.name, count: (songs || []).filter((s) => arrIndexFor(s, v.name) !== undefined).length }));
    }

    // Arrangement index of `part` in a song, or undefined (server default).
    // Prefers the exact name ("Lead") over a numbered one ("Lead 2").
    function arrIndexFor(song, part) {
        if (!part) return undefined;
        const p = part.toLowerCase();
        const arrs = (song && song.arrangements) || [];
        const exact = arrs.find((a) => String(a.name || '').toLowerCase() === p);
        let loose = exact || arrs.find((a) => baseName(a.name).toLowerCase() === p);
        // A guitar part with no Lead/Rhythm chart often has a Combo one.
        if (!loose && (p === 'lead' || p === 'rhythm')) loose = arrs.find((a) => baseName(a.name).toLowerCase() === 'combo');
        if (!loose) return undefined;
        const idx = loose.index != null ? Number(loose.index) : arrs.indexOf(loose);
        return Number.isFinite(idx) ? idx : undefined;
    }

    // ── Track order ──────────────────────────────────────────────────────────
    // Many charts carry no track number, so the album page sorts by title.
    // The user can fix the order once (↑/↓ on the album page); it is kept per
    // album in this browser and used by Play album and the playthrough.
    const orderKey = (a) => 'feedBack.albumOrder.' + String(a.artist || '') + '␟' + String(a.album || '');

    function savedOrder(a) {
        try {
            const v = JSON.parse(window.localStorage.getItem(orderKey(a)) || 'null');
            return Array.isArray(v) ? v : null;
        } catch (_) { return null; }
    }

    // `songs` re-sorted by the saved order; songs the order doesn't know
    // (newly added charts) keep their place after the known ones.
    function applyOrder(a, songs) {
        const order = savedOrder(a);
        if (!order) return songs;
        const pos = new Map(order.map((f, i) => [f, i]));
        return songs
            .map((s, i) => ({ s, i, p: pos.has(s.filename) ? pos.get(s.filename) : order.length + i }))
            .sort((x, y) => x.p - y.p)
            .map((x) => x.s);
    }

    function saveOrder(a, songs) {
        try { window.localStorage.setItem(orderKey(a), JSON.stringify(songs.map((s) => s.filename))); } catch (_) { /* private mode */ }
    }

    // A chart by `artist` titled `title` that isn't on the album page yet
    // (e.g. tagged with no album). Exact title after folding case/punctuation.
    const fold = (s) => String(s || '').normalize('NFKD').replace(/[̀-ͯ]/g, '')
        .toLowerCase().replace(/&/g, 'and').replace(/[^a-z0-9]+/g, '');
    // Loaded once per album open (the artist's whole shelf, no drawer filters
    // and no text search — a search for "The End." misses a chart titled
    // "The End", and a filter can hide a track that IS on the album).
    async function artistCharts(artist) {
        try {
            const p = new URLSearchParams({ artist, size: '500' });
            const r = await fetch('/api/library?' + p.toString());
            const d = r.ok ? await r.json() : null;
            return ((d && d.songs) || []).filter((s) => s && s.filename);
        } catch (_) { return []; }
    }

    function findChart(charts, title, have) {
        const want = fold(title);
        return want ? (charts.find((s) => !have.has(s.filename) && fold(s.title) === want) || null) : null;
    }

    // The order to show: the user's own (▲/▼) if set, else the real track
    // list from MusicBrainz (server-cached per album), else the server's.
    // Resolves { songs, source: 'manual'|'musicbrainz'|'', missing: [titles] }.
    async function orderSongs(a, songs) {
        const o = await albumOrder(a, songs);
        // The user's own order wins; it still includes charts found elsewhere.
        if (savedOrder(a)) return { songs: applyOrder(a, o.songs), source: 'manual', missing: o.missing };
        return o;
    }

    async function albumOrder(a, songs) {
        if (songs.length < 2) return { songs, source: '', missing: [] };
        try {
            const r = await fetch('/api/library/album-order', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    artist: a.artist, album: a.album,
                    songs: songs.map((s) => ({ filename: s.filename, title: s.title || '' })),
                }),
            });
            const d = r.ok ? await r.json() : null;
            const order = (d && d.order) || [];
            if (!order.length) return { songs, source: '', missing: [] };
            // Track number per chart: matched ones from the server; then look
            // for each missing track among the artist's OTHER charts (a chart
            // often has no or a different album tag) and slot it in.
            const slot = (o) => (Number(o.disc) || 1) * 1000 + (Number(o.position) || 0);
            const pos = new Map(order.map((o) => [o.filename, slot(o)]));
            const have = new Set(songs.map((s) => s.filename));
            const extra = [];
            const missing = [];
            const charts = (d.missing || []).length ? await artistCharts(a.artist) : [];
            for (const m of (d.missing || [])) {
                const found = findChart(charts, m.title, have);
                if (found) { have.add(found.filename); extra.push(found); pos.set(found.filename, slot(m)); }
                else missing.push(m.title);
            }
            const sorted = songs.concat(extra)
                .map((s, i) => ({ s, p: pos.has(s.filename) ? pos.get(s.filename) : 1e9 + i }))
                .sort((x, y) => x.p - y.p)
                .map((x) => x.s);
            return { songs: sorted, source: 'musicbrainz', missing };
        } catch (_) {
            return { songs, source: '', missing: [] };
        }
    }

    function resetOrder(a) {
        try { window.localStorage.removeItem(orderKey(a)); } catch (_) { /* */ }
    }

    // ── Overlay ──────────────────────────────────────────────────────────────
    function closeOverlay() {
        if (tick) { clearInterval(tick); tick = null; }
        if (overlay) { overlay.remove(); overlay = null; }
    }

    function showOverlay(html) {
        closeOverlay();
        overlay = document.createElement('div');
        overlay.setAttribute('role', 'dialog');
        overlay.setAttribute('aria-modal', 'true');
        overlay.setAttribute('data-apt-overlay', '');
        overlay.style.cssText = 'position:fixed;inset:0;z-index:2147483000;display:flex;align-items:center;justify-content:center;' +
            'background:rgba(0,0,0,.72);backdrop-filter:blur(4px);padding:16px;';
        overlay.innerHTML = '<div style="width:100%;max-width:520px;max-height:90vh;overflow:auto;border-radius:14px;padding:24px;' +
            'background:#14141c;border:1px solid rgba(255,255,255,.12);box-shadow:0 20px 60px rgba(0,0,0,.6);color:#f3f3f7;font-family:inherit;">' +
            html + '</div>';
        document.body.appendChild(overlay);
        return overlay;
    }

    const btnPrimary = 'bg-fb-primary hover:bg-fb-primaryHi text-white text-sm font-medium px-4 py-2 rounded-md';
    const btnGhost = 'bg-fb-card/60 hover:bg-fb-card border border-fb-border/50 text-fb-text text-sm px-4 py-2 rounded-md';

    function pct(r) {
        const n = r.hits + r.misses;
        return n ? Math.round((r.hits / n) * 100) : null;
    }

    function showNextUp(release) {
        const nxt = q().peekNext();
        const song = run.songs[nxt.index];
        const done = run.results[cur];
        const p = done ? pct(done) : null;
        let left = NEXT_UP_SECONDS;
        const el = showOverlay(
            '<div style="font-size:12px;letter-spacing:.08em;text-transform:uppercase;opacity:.6;">' + esc(run.album) +
            ' · ' + esc(run.part || 'Default part') + '</div>' +
            (done ? '<div style="margin-top:10px;font-size:14px;opacity:.8;">Finished <b>' + esc(done.title) + '</b>' +
                (p != null ? ' · ' + p + '%' : '') + '</div>' : '') +
            '<div style="margin-top:18px;font-size:13px;opacity:.6;">Next up · track ' + (nxt.index + 1) + ' of ' + nxt.total + '</div>' +
            '<div style="margin-top:4px;font-size:26px;font-weight:700;line-height:1.2;">' + esc(song ? (song.title || song.filename) : nxt.filename) + '</div>' +
            '<div style="margin-top:20px;display:flex;gap:8px;align-items:center;">' +
            '<button data-apt-now class="' + btnPrimary + '">▶ Play now <span data-apt-count>(' + left + ')</span></button>' +
            '<button data-apt-end class="' + btnGhost + '">End playthrough</button></div>');
        const go = () => { closeOverlay(); release(); };
        el.querySelector('[data-apt-now]').addEventListener('click', go);
        el.querySelector('[data-apt-end]').addEventListener('click', () => {
            closeOverlay();
            showSummary(() => { q().clear(); release(); }, true);
        });
        tick = setInterval(() => {
            left--;
            const c = el.querySelector('[data-apt-count]');
            if (c) c.textContent = '(' + left + ')';
            if (left <= 0) go();
        }, 1000);
    }

    function showSummary(release, stoppedEarly) {
        const r = run;
        run = null; cur = -1; ending = false;
        const rows = r.songs.map((s, i) => {
            const res = r.results[i];
            const p = res ? pct(res) : null;
            const status = !res ? '<span style="opacity:.4;">not played</span>'
                : !res.finished ? '<span style="opacity:.6;">stopped' + (p != null ? ' · ' + p + '%' : '') + '</span>'
                : p != null ? '<b>' + p + '%</b>' : '<span style="opacity:.6;">played</span>';
            return '<tr><td style="padding:6px 8px 6px 0;opacity:.5;text-align:right;">' + (i + 1) + '</td>' +
                '<td style="padding:6px 8px;">' + esc(s.title || s.filename) + '</td>' +
                '<td style="padding:6px 0;text-align:right;white-space:nowrap;">' + status + '</td></tr>';
        }).join('');
        const scored = r.results.filter((x) => x && x.hits + x.misses > 0);
        const hits = scored.reduce((a, x) => a + x.hits, 0);
        const total = scored.reduce((a, x) => a + x.hits + x.misses, 0);
        const played = r.results.filter((x) => x && x.finished).length;
        const el = showOverlay(
            '<div style="font-size:12px;letter-spacing:.08em;text-transform:uppercase;opacity:.6;">' +
            (stoppedEarly ? 'Playthrough ended' : 'Album complete') + ' · ' + esc(r.part || 'Default part') + '</div>' +
            '<div style="margin-top:6px;font-size:24px;font-weight:700;">' + esc(r.album) + '</div>' +
            '<div style="font-size:14px;opacity:.7;">' + esc(r.artist) + '</div>' +
            '<div style="margin-top:14px;display:flex;gap:24px;">' +
            '<div><div style="font-size:28px;font-weight:700;">' + played + '/' + r.songs.length + '</div><div style="font-size:12px;opacity:.6;">tracks finished</div></div>' +
            (total ? '<div><div style="font-size:28px;font-weight:700;">' + Math.round((hits / total) * 100) + '%</div><div style="font-size:12px;opacity:.6;">overall accuracy</div></div>' : '') +
            '</div>' +
            '<table style="width:100%;margin-top:16px;font-size:14px;border-collapse:collapse;">' + rows + '</table>' +
            '<div style="margin-top:20px;"><button data-apt-done class="' + btnPrimary + '">Done</button></div>');
        el.querySelector('[data-apt-done]').addEventListener('click', () => { closeOverlay(); release(); });
    }

    // ── Lifecycle ────────────────────────────────────────────────────────────
    sm.on('song:loading', (e) => {
        ending = false;
        closeOverlay();
        if (!isOurs()) { if (run && !(q() && q().active())) run = null; return; }
        const fn = e && e.detail && e.detail.filename;
        let raw = fn ? String(fn) : '';
        try { raw = decodeURIComponent(raw); } catch (_) { /* already raw */ }
        const i = run.files.indexOf(raw);
        cur = i >= 0 ? i : Math.min(cur + 1, run.files.length - 1);
        const s = run.songs[cur];
        run.results[cur] = { title: (s && (s.title || s.filename)) || raw, hits: 0, misses: 0, played: true, finished: false };
    });
    // Switching part mid-song restarts scoring for that song.
    sm.on('song:arrangement-changed', () => {
        const r = run && run.results[cur];
        if (r) { r.hits = 0; r.misses = 0; }
    });
    sm.on('note:hit', () => { const r = run && run.results[cur]; if (r && !ending) r.hits++; });
    sm.on('note:miss', () => { const r = run && run.results[cur]; if (r && !ending) r.misses++; });

    sm.on('song:ended', () => {
        if (!isOurs() || ending) return;
        ending = true;
        const r = run.results[cur];
        if (r) r.finished = true;
        // Claim the exit: the card decides when to move on. release() goes
        // through the queue-aware closeCurrentSong, which advances the queue.
        const release = sm.holdAutoExit();
        if (q().hasNext()) showNextUp(release);
        else showSummary(release, false);
    });

    // Quitting the player mid-song (Esc / ✕) abandons the queue: show what
    // was played so far. song:stop also follows a normal end — ignore that.
    sm.on('song:stop', () => {
        if (!run || ending || overlay) return;
        if (q() && q().active() && q().source() === run.tag) return;
        if (run.results.some(Boolean)) showSummary(() => {}, true);
        else run = null;
    });

    function start(opts) {
        const songs = ((opts && opts.songs) || []).filter((s) => s && s.filename);
        if (!songs.length || !q()) return false;
        const part = (opts && opts.part) || '';
        const files = songs.map((s) => s.filename);
        const arrangements = part ? songs.map((s) => arrIndexFor(s, part)) : undefined;
        closeOverlay();
        run = {
            tag: TAG_PREFIX + (opts.album || ''),
            album: opts.album || '', artist: opts.artist || '', part,
            songs, files, results: [],
        };
        cur = -1; ending = false;
        return q().start(files, { source: run.tag, arrangements });
    }

    sm.albumPlaythrough = {
        start, partsFor, arrIndexFor, active: () => !!run,
        applyOrder, orderSongs, saveOrder, resetOrder, hasSavedOrder: (a) => !!savedOrder(a),
    };
})();

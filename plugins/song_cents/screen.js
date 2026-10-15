// Song Cents: show how many cents a song's recording sits off its chart tuning (lots of Oasis
// records are +25..+50c sharp but show plain "E Standard"). feedBack 0.3 port of the 0.2.9 plugin.
//
// routes.py resolves an offset from the cache, the chart, or the source PSARC's CentOffset. When
// nothing is known, this script measures it from the song's guitar stem: decodeAudioData only, no
// playback (assets/estimate.js), then posts it back so it is cached for everyone.
// Shown as a small "+45c" on the library card's tuning chip.
//
// Interface for other plugins (unchanged from 0.2.9; Live Retune's "Match song" uses it):
//   window.__songCents.current  -> { file, cents: number|null, source, confidence? } for the
//                                  song in the player (source 'pending' while resolving)
//   window 'songcents:change'   -> CustomEvent (detail = current) whenever current changes
//   window.__songCents.known / .resolve(file) / .scanLibrary(onProgress)
(function () {
    'use strict';
    const VERSION = '2.0.0';
    const MIN_CONFIDENCE = 0.35;   // below this the measurement is noise (drum solos, ambient)
    const IN_TUNE = 8;             // |cents| under this counts as in tune (measurement is ~±5c)
    const API = '/api/plugins/song_cents';

    const sc = window.__songCents = window.__songCents || {};
    sc.version = VERSION;
    sc.known = sc.known || {};
    sc.current = sc.current || null;

    const emit = () => window.dispatchEvent(new CustomEvent('songcents:change', { detail: sc.current }));

    let estimatorP = null;
    function estimator() {
        if (window.__songCentsEstimate) return Promise.resolve(window.__songCentsEstimate);
        if (!estimatorP) {
            estimatorP = new Promise((resolve, reject) => {
                const s = document.createElement('script');
                s.src = `${API}/assets/estimate.js?v=${VERSION}`;
                s.onload = () => resolve(window.__songCentsEstimate);
                s.onerror = reject;
                document.head.append(s);
            });
        }
        return estimatorP;
    }

    const pathUrl = (p) => p.split('/').map(encodeURIComponent).join('/');

    async function getJson(url, init) {
        const r = await fetch(url, init);
        if (!r.ok) throw new Error(`${r.status} ${url}`);
        return r.json();
    }

    // Measure one pack from its stem. Returns the record the server cached.
    async function measure(file, stem) {
        const est = await estimator();
        const res = await fetch(`/api/sloppak/${pathUrl(file)}/file/${pathUrl(stem)}`);
        if (!res.ok) throw new Error(`stem ${res.status}`);
        const buf = await res.arrayBuffer();
        // Decoding into an OfflineAudioContext resamples to 44.1 kHz and never plays anything.
        const ctx = new OfflineAudioContext(1, 1, 44100);
        const audio = await ctx.decodeAudioData(buf);
        let mono = audio.getChannelData(0);
        if (audio.numberOfChannels > 1) {
            const mix = new Float32Array(audio.length);
            for (let c = 0; c < audio.numberOfChannels; c++) {
                const d = audio.getChannelData(c);
                for (let i = 0; i < d.length; i++) mix[i] += d[i] / audio.numberOfChannels;
            }
            mono = mix;
        }
        const r = est.estimateCents(mono, audio.sampleRate);
        let cents = null;
        if (r.confidence >= MIN_CONFIDENCE) cents = Math.abs(r.cents) < IN_TUNE ? 0 : Math.round(r.cents);
        return getJson(`${API}/measured`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ file, cents, confidence: r.confidence, raw: r.cents }),
        });
    }

    // Resolve (and if needed measure) one song's offset.
    async function resolve(file) {
        let rec = await getJson(`${API}/offset?file=${encodeURIComponent(file)}`);
        if (rec.source === 'unmeasured' && rec.stem) rec = await measure(file, rec.stem);
        sc.known[file] = { cents: rec.cents, source: rec.source };
        scheduleBadges();
        return rec;
    }
    sc.resolve = resolve;

    // ── Current song ────────────────────────────────────────────────────────
    async function onSong(evOrSong) {
        // The bus is an EventTarget: handlers get a CustomEvent with the song in .detail.
        const song = evOrSong && evOrSong.detail !== undefined ? evOrSong.detail : evOrSong;
        const file = song && song.filename;
        if (!file) return;
        if (sc.current && sc.current.file === file && sc.current.source !== 'error') return;   // arrangement switch
        sc.current = { file, cents: null, source: 'pending' };
        emit();
        try {
            const rec = await resolve(file);
            if (sc.current && sc.current.file === file) {
                sc.current = { file, cents: rec.cents, source: rec.source, confidence: rec.confidence };
                emit();
            }
        } catch (e) {
            console.warn('[song_cents]', file, e);
            if (sc.current && sc.current.file === file) { sc.current.source = 'error'; emit(); }
        }
    }

    // ── Library badges (v3 song cards) ──────────────────────────────────────
    // Runs after the grid paints a window of cards (v3:library-window-rendered) and when an
    // offset becomes known; never on a per-frame path.
    const fmt = (c) => (c > 0 ? '+' : '') + Math.round(c) + 'c';
    const SRC = { psarc: 'from the original PSARC chart', chart: 'from the chart', audio: 'measured from the recording (±5c)' };
    let badgeTimer = null;
    function scheduleBadges() {
        if (badgeTimer) return;
        badgeTimer = setTimeout(() => { badgeTimer = null; badges(); }, 150);
    }
    function badges() {
        const grid = document.getElementById('v3-songs-grid');
        if (!grid) return;
        for (const card of grid.querySelectorAll('[data-fn]')) {
            const file = card.getAttribute('data-fn');
            const k = sc.known[file];
            const chip = card.querySelector('[data-tuning-chip]') || card.querySelector('[data-v3-play] > span.bg-fb-mid');
            const has = card.querySelector('.sc-badge');
            if (!k || !k.cents || !chip) { if (has) has.remove(); continue; }
            const label = fmt(k.cents);
            if (has && has.parentNode === chip) { if (has.textContent !== label) has.textContent = label; continue; }
            if (has) has.remove();
            const b = document.createElement('span');
            b.className = 'sc-badge';
            b.style.cssText = 'margin-left:4px;padding:0 3px;border-radius:2px;background:rgba(14,116,144,.85);color:#e0f2fe';
            b.textContent = label;
            b.title = `Recording is ${label} off the chart tuning, ${SRC[k.source] || k.source}. Live Retune → Match song plays your guitar in tune with it.`;
            chip.appendChild(b);
        }
    }

    async function loadKnown() {
        try {
            Object.assign(sc.known, await getJson(`${API}/known`));
            scheduleBadges();
        } catch (_) { /* server route not up yet */ }
    }

    // ── Library scan (plugin settings) ──────────────────────────────────────
    let scanning = false;
    sc.scanLibrary = async function (onProgress) {
        if (scanning) return null;
        scanning = true;
        try {
            const { files } = await getJson(`${API}/pending`);
            let done = 0, failed = 0;
            for (const f of files) {
                try { await resolve(f); } catch (_) { failed++; }
                done++;
                if (onProgress) onProgress(done, files.length, failed, f);
            }
            return { total: files.length, failed };
        } finally {
            scanning = false;
        }
    };

    const bus = window.feedBack;
    if (bus && bus.on) {
        bus.on('song:loaded', onSong);
        bus.on('v3:library-window-rendered', scheduleBadges);
    }
    if (bus && bus.currentSong) onSong(bus.currentSong);
    loadKnown();
})();

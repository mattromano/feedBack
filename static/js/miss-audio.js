// Miss audio: Rock Band-style sound on a missed note, for every instrument.
//
//   off      nothing (the default)
//   clunk    a short, quiet dull fret noise on a miss / overhit
//   dropout  the clunk, and the instrument's stem drops out until the next hit
//
// Listens on the core bus (window.feedBack): drums:miss / drums:overhit /
// drums:hit from the drums plugin, five_fret:miss / five_fret:overstrum /
// five_fret:hit, and note:miss / note:hit (guitar and bass through note
// detection). The stem is muted through the stems capability under a claim of
// its own and restored on the next hit, a seek, or a new song. While a
// RockHeroSmith band session is running (play:judgment events) it stands down:
// the band has its own miss sound and part audio.
//
// The clunk is synthesized with WebAudio (no sample files): a band-passed noise
// burst and a low falling thump, ~130 ms, rate-limited so a burst of misses never
// stacks. Adapted from RockHeroSmith's miss-sound.js and stem-claims.js (MIT).
//
// window.feedBack.missAudio: { mode(), setMode(m), MODES }. The setting is per
// browser (localStorage feedBack_miss_audio).
(function (root, factory) {
    'use strict';
    const api = factory();
    if (typeof module === 'object' && module && module.exports) module.exports = api;
    if (typeof window !== 'undefined' && window) {
        window.feedBackMissAudio = api;
        if (window.feedBack) {
            try { window.feedBack.missAudio = api.install({ win: window }); } catch (e) { console.warn('[miss-audio] install failed:', e); }
        }
    }
})(typeof self !== 'undefined' ? self : this, function () {
    'use strict';

    const MODES = Object.freeze(['off', 'clunk', 'dropout']);
    const STORAGE_KEY = 'feedBack_miss_audio';
    const GAP_MS = 90;
    const VOLUME = 0.16;
    const BAND_QUIET_MS = 5000;     // a play:judgment this recent means a band session owns miss audio
    const CLAIM_PREFIX = 'feedback:missaudio:';
    const REQUESTER = 'feedback.missaudio';

    const MISS_EVENTS = { 'drums:miss': 'drums', 'drums:overhit': 'drums', 'five_fret:miss': 'five_fret',
        'five_fret:overstrum': 'five_fret', 'note:miss': 'guitar' };
    const HIT_EVENTS = { 'drums:hit': 'drums', 'five_fret:hit': 'five_fret', 'note:hit': 'guitar' };
    const RESET_EVENTS = ['song:seek', 'song:loaded', 'song:ended', 'song:stop'];

    function normalizeMode(m) { return MODES.includes(m) ? m : 'off'; }

    // The stem an instrument plays: drums -> drums; guitar / 5-fret -> bass on a
    // bass arrangement, else guitar.
    function stemFor(instrument, arrangement) {
        if (instrument === 'drums') return 'drums';
        return /bass/i.test(String(arrangement || '')) ? 'bass' : 'guitar';
    }

    function createClunk({ win, audioFactory, now }) {
        const factory = typeof audioFactory === 'function' ? audioFactory : () => {
            const Ctx = win && (win.AudioContext || win.webkitAudioContext);
            return typeof Ctx === 'function' ? new Ctx() : null;
        };
        let ctx = null, failed = false, noise = null, last = -Infinity;
        function context() {
            if (ctx || failed) return ctx;
            try { ctx = factory() || null; } catch (_) { ctx = null; }
            if (!ctx) failed = true;
            return ctx;
        }
        function noiseBuffer(c) {
            if (noise) return noise;
            const rate = c.sampleRate || 44100;
            const n = Math.max(1, Math.floor(rate * 0.12));
            noise = c.createBuffer(1, n, rate);
            const data = noise.getChannelData(0);
            for (let i = 0; i < n; i++) data[i] = Math.random() * 2 - 1;
            return noise;
        }
        function synth(c) {
            const t = c.currentTime;
            const out = c.createGain();
            out.gain.value = VOLUME;
            out.connect(c.destination);
            const src = c.createBufferSource();
            src.buffer = noiseBuffer(c);
            const band = c.createBiquadFilter();
            band.type = 'bandpass';
            band.frequency.value = 900;
            band.Q.value = 0.9;
            const nEnv = c.createGain();
            nEnv.gain.setValueAtTime(0.0001, t);
            nEnv.gain.exponentialRampToValueAtTime(0.6, t + 0.003);
            nEnv.gain.exponentialRampToValueAtTime(0.0001, t + 0.09);
            src.connect(band); band.connect(nEnv); nEnv.connect(out);
            const osc = c.createOscillator();
            osc.type = 'sine';
            osc.frequency.setValueAtTime(140, t);
            osc.frequency.exponentialRampToValueAtTime(55, t + 0.1);
            const oEnv = c.createGain();
            oEnv.gain.setValueAtTime(0.0001, t);
            oEnv.gain.exponentialRampToValueAtTime(1, t + 0.004);
            oEnv.gain.exponentialRampToValueAtTime(0.0001, t + 0.13);
            osc.connect(oEnv); oEnv.connect(out);
            src.start(t); src.stop(t + 0.15);
            osc.start(t); osc.stop(t + 0.15);
            osc.onended = () => { try { out.disconnect(); } catch (_) { /* gone */ } };
        }
        return {
            play() {
                const at = Number(now());
                if (Number.isFinite(at) && at - last < GAP_MS) return false;
                const c = context();
                if (!c) return false;
                last = Number.isFinite(at) ? at : last;
                try {
                    if (c.state === 'suspended' && typeof c.resume === 'function') {
                        const r = c.resume();
                        if (r && typeof r.catch === 'function') r.catch(() => {});
                    }
                    synth(c);
                    return true;
                } catch (_) { return false; }
            },
        };
    }

    // create({ win, bus?, caps?, storage?, audioFactory?, now? }) -> controller. Pure of
    // the DOM: tests pass fakes. handle(type, detail) is what the bus listeners call.
    function create(opts) {
        const o = opts || {};
        const win = o.win || null;
        const now = typeof o.now === 'function' ? o.now
            : () => (win && win.performance && typeof win.performance.now === 'function' ? win.performance.now() : Date.now());
        const storage = o.storage !== undefined ? o.storage : (win && win.localStorage) || null;
        const caps = () => o.caps || (win && win.feedBack && win.feedBack.capabilities) || null;
        const clunk = createClunk({ win, audioFactory: o.audioFactory, now });
        const muted = new Set();       // stem ids this module has dropped
        let lastBand = -Infinity;
        let mode = 'off';
        try { mode = normalizeMode(storage && storage.getItem(STORAGE_KEY)); } catch (_) { mode = 'off'; }

        function arrangement() {
            const s = win && win.feedBack && win.feedBack.currentSong;
            return s && (s.arrangement || s.arrangementName) || '';
        }
        function mute(stemId) {
            const c = caps();
            if (muted.has(stemId) || !c || typeof c.claim !== 'function' || typeof c.dispatch !== 'function') return;
            const claimId = CLAIM_PREFIX + stemId;
            muted.add(stemId);
            try {
                c.claim({ capability: 'stems', claimId, requester: REQUESTER });
                const r = c.dispatch({ capability: 'stems', command: 'mute', requester: REQUESTER,
                    args: { id: stemId, claimId }, claim: { claimId, capability: 'stems' } });
                if (r && typeof r.catch === 'function') r.catch(() => {});
            } catch (_) { /* the stem plays on */ }
        }
        function restore(stemId) {
            if (!muted.delete(stemId)) return;
            const c = caps();
            if (!c) return;
            const claimId = CLAIM_PREFIX + stemId;
            // stems drops a released claim's snapshot, so the restore lands first.
            Promise.resolve()
                .then(() => (typeof c.dispatch === 'function'
                    ? c.dispatch({ capability: 'stems', command: 'restore', requester: REQUESTER, args: { claimId } }) : null))
                .catch(() => {})
                .then(() => { try { if (typeof c.release === 'function') c.release({ capability: 'stems', claimId, requester: REQUESTER }); } catch (_) { /* gone */ } });
        }
        function restoreAll() { for (const id of [...muted]) restore(id); }

        function handle(type, detail) {
            if (type === 'play:judgment') { lastBand = now(); restoreAll(); return null; }
            if (RESET_EVENTS.includes(type)) { restoreAll(); return null; }
            const bandOn = now() - lastBand < BAND_QUIET_MS;
            if (HIT_EVENTS[type]) {
                const stem = stemFor(HIT_EVENTS[type], arrangement());
                if (muted.has(stem)) restore(stem);
                return null;
            }
            const inst = MISS_EVENTS[type];
            if (!inst || mode === 'off' || bandOn) return null;
            // A judgment the player didn't make (an auto note) never clunks.
            if (detail && detail.auto) return null;
            const played = clunk.play();
            if (mode === 'dropout') mute(stemFor(inst, arrangement()));
            return played ? 'clunk' : null;
        }

        return {
            MODES,
            mode: () => mode,
            setMode(m) {
                mode = normalizeMode(m);
                try { if (storage) storage.setItem(STORAGE_KEY, mode); } catch (_) { /* per session then */ }
                if (mode !== 'dropout') restoreAll();
                return mode;
            },
            muted: () => [...muted],
            handle,
            restoreAll,
        };
    }

    // Wire a controller to the core bus (window.feedBack) and the Gameplay
    // settings' Miss sound select (#setting-miss-audio).
    function install(opts) {
        const ctl = create(opts);
        const win = opts && opts.win;
        const bus = (opts && opts.bus) || (win && win.feedBack);
        if (bus && typeof bus.on === 'function') {
            const types = [...Object.keys(MISS_EVENTS), ...Object.keys(HIT_EVENTS), ...RESET_EVENTS, 'play:judgment'];
            for (const type of types) bus.on(type, (e) => ctl.handle(type, e && e.detail));
        }
        const sel = win && win.document && typeof win.document.getElementById === 'function'
            ? win.document.getElementById('setting-miss-audio') : null;
        if (sel) {
            sel.value = ctl.mode();
            sel.addEventListener('change', () => { sel.value = ctl.setMode(sel.value); });
        }
        return ctl;
    }

    return { MODES, STORAGE_KEY, CLAIM_PREFIX, stemFor, normalizeMode, create, install };
});

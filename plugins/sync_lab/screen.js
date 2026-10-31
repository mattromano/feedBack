// Sync Lab (main menu → Calibrate): measured calibration of the A/V offset and
// every instrument's input offset: real guitar / bass (Note Detection), the
// drum kit, gamepad drum controllers and 5-Fret guitar controllers, for one
// player or split view (one result per panel).
//
// How it works. The calibration song (built by routes.py, in the library as
// "Sync Calibration") is an ordinary song played through the normal player, so
// every judgment comes from the same clocks, highway and engines as real play.
// One note per beat, three sections:
//   listen  clicks + notes, the screen is blanked: you play by ear
//   watch   no clicks: you play by eye
//   play    both (a check)
// Every judgment is stored offset-free (error + the offset in use), so later
// offset changes don't invalidate it. Judgments are made on the highway's
// visual clock (Note Detection, drums and 5-Fret all work that way), so:
//   eye error = display latency + input latency          (A/V offset cancels)
//   ear error = input latency + audio latency + A/V offset
// Ear and eye agree when A/V offset = current + (eye - ear): that is the A/V
// suggestion (one per machine: averaged over the players). The eye median is
// each player's input offset, and does not depend on the A/V offset.
//
// Inputs:
//   guitar    notedetect:hit / notedetect:miss events (timingError ms, + = late,
//             latencyMs). Main player from the window copy (isDefault); split
//             panels from the copy that bubbles from the panel's detector.
//   drums     'drums:judgment' window events ({noteTime, errMs, inputOffsetMs,
//             kickOffsetMs, controllerKey, canvas}); controllerKey null = the kit.
//   5-Fret    five_fret:hit / five_fret:miss bus events ({noteTime, offsetMs,
//             inputOffsetMs, controllerKey, canvas}).
// Settings it changes (TARGETS):
//   av            window.setAvOffsetMs (core; reaches every highway)
//   guitarLat     Note Detection applySettings({latencyOffset}) on every instance
//   drumsKit      drums input offset (window.__drumsSetInputOffset)
//   drumsKick     drums kick offset (feedBackDrums.setKickOffset)
//   drumsPad:<k>  a drum controller's offset (feedBackDrums.setControllerOffset)
//   fiveFret:<k>  a 5-Fret controller's offset (feedBackFiveFret.setControllerOffset)
//
// Lock: applying a calibration sets the A/V offset as 'manual' (core then
// ignores automatic writes) and turns Note Detection's end-of-song A/V
// auto-calibrate off. Only the A/V auto-follow below still moves it.
//
// A/V auto-follow: the stems clock reports how far ahead of the speakers it
// renders (window.__hwtRenderAheadMs, highway_tweaks). That value is stored
// with the A/V offset after a calibration (or any manual A/V change); when a
// later song shows a different output latency (other headphones / speakers),
// the A/V offset is shifted by the difference, so picture and sound stay together.
(function () {
    'use strict';

    const MIN_N = 8;

    // ── Pure helpers (unit-tested in tests/plugins/sync_lab) ────────────────
    const median = (a) => {
        if (!a.length) return NaN;
        const s = a.slice().sort((x, y) => x - y), m = s.length >> 1;
        return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
    };
    const iqr = (a) => {
        if (a.length < 4) return NaN;
        const s = a.slice().sort((x, y) => x - y);
        return s[Math.floor(s.length * 0.75)] - s[Math.floor(s.length * 0.25)];
    };

    // Section a note time belongs to (half a beat of slack on each side).
    function sectionOf(plan, t) {
        if (!plan) return null;
        for (const [name, [a, b]] of Object.entries(plan.sections)) {
            if (t >= a - plan.beat * 0.5 && t < b - plan.beat * 0.5) return name;
        }
        return null;
    }

    // Every timing setting: range and step (ms). Per-controller ids are
    // '<base>:<controllerKey>'.
    const TARGETS = {
        av:        { label: 'A/V offset', min: -1000, max: 1000, step: 1 },
        guitarLat: { label: 'Guitar latency', min: 0, max: 250, step: 1 },
        drumsKit:  { label: 'Drum kit input offset', min: -250, max: 250, step: 1 },
        drumsKick: { label: 'Kick pedal extra offset', min: -250, max: 250, step: 1 },
        drumsPad:  { label: 'Drum controller offset', min: -250, max: 250, step: 1 },
        fiveFret:  { label: '5-Fret controller offset', min: -200, max: 200, step: 5 },
    };
    const targetBase = (id) => String(id).split(':')[0];
    const targetSpec = (id) => TARGETS[targetBase(id)] || null;
    function clampTarget(id, v) {
        const t = targetSpec(id);
        if (!t || !Number.isFinite(v)) return NaN;
        const stepped = Math.round(v / t.step) * t.step;
        return Math.max(t.min, Math.min(t.max, stepped));
    }
    // The setting a player's input offset goes to.
    function targetFor(p) {
        if (!p) return null;
        if (p.kind === 'guitar') return 'guitarLat';
        if (p.kind === 'drums') return p.controllerKey ? 'drumsPad:' + p.controllerKey : 'drumsKit';
        if (p.kind === 'five_fret') return p.controllerKey ? 'fiveFret:' + p.controllerKey : null;
        return null;
    }

    // How much to trust a median: n samples with spread (IQR, ms).
    function confidence({ n, spread }) {
        if (!(n >= MIN_N)) return { rating: 'unreliable', plusMinus: NaN, words: 'Not enough hits: run it again' };
        const sp = Number.isFinite(spread) ? spread : 0;
        const plusMinus = Math.max(1, Math.round(1.57 * sp / Math.sqrt(n)));
        if (n >= 16 && sp <= 25) return { rating: 'great', plusMinus, words: 'Very consistent (±' + plusMinus + ' ms)' };
        if (sp <= 45) return { rating: 'good', plusMinus, words: 'Consistent (±' + plusMinus + ' ms)' };
        if (sp <= 70) return { rating: 'rough', plusMinus, words: 'A bit uneven (±' + plusMinus + ' ms)' };
        return { rating: 'unreliable', plusMinus, words: 'Very uneven: run it again for a better number' };
    }

    // players: { key: { key, kind: 'guitar'|'drums'|'five_fret', controllerKey?, label, listen: [], watch: [], play: [] } }
    // (offset-free errors in ms). cur: { av, offsets?: {targetId: ms}, guitarLat, drumOffset } (ms, null = unknown);
    // cur.av is the A/V offset the ear samples were measured WITH (the ear
    // error includes it), so the suggestion stays right after it is applied.
    function analyse(players, cur) {
        const offsets = (cur && cur.offsets) || {};
        const currentOf = (id, p) => {
            if (id in offsets) return offsets[id];
            if (p.kind === 'guitar') return cur.guitarLat;
            if (id === 'drumsKit') return cur.drumOffset;
            return null;
        };
        const rows = [];
        let dSum = 0, dW = 0, nEar = 0, nEye = 0, spreadMax = 0;
        for (const p of Object.values(players || {})) {
            const ear = median(p.listen), eye = median(p.watch), both = median(p.play);
            const okEar = p.listen.length >= MIN_N, okEye = p.watch.length >= MIN_N;
            const target = targetFor(p);
            const r = { key: p.key, kind: p.kind, label: p.label, controllerKey: p.controllerKey || null, target,
                n: [p.listen.length, p.watch.length, p.play.length],
                ear, eye, both, spread: Math.max(iqr(p.watch) || 0, iqr(p.listen) || 0) || NaN,
                d: NaN, input: NaN, wanted: NaN, current: NaN, suggested: NaN, delta: NaN,
                confidence: confidence({ n: p.watch.length, spread: iqr(p.watch) }) };
            if (okEar && okEye) {
                r.d = eye - ear;
                const w = Math.min(p.listen.length, p.watch.length);
                dSum += r.d * w; dW += w;
                nEar += p.listen.length; nEye += p.watch.length;
                spreadMax = Math.max(spreadMax, r.spread || 0);
            }
            if (okEye && target) {
                r.wanted = Math.round(eye);
                r.current = currentOf(target, p);
                r.input = r.suggested = clampTarget(target, r.wanted);
                if (Number.isFinite(r.current)) r.delta = r.suggested - r.current;
            }
            rows.push(r);
        }
        const d = dW ? dSum / dW : NaN;
        const avNew = Number.isFinite(d) ? Math.max(-1000, Math.min(1000, Math.round(cur.av + d))) : NaN;
        return { av: cur.av, d, rows, avNew,
            avConfidence: confidence({ n: Math.min(nEar, nEye), spread: spreadMax }) };
    }

    // A/V auto-follow: the A/V offset for a new output latency, or null.
    function followAv(ref, aheadMs, currentAv) {
        if (!ref || !Number.isFinite(aheadMs) || !Number.isFinite(ref.aheadMs) || !Number.isFinite(ref.av)) return null;
        const delta = aheadMs - ref.aheadMs;
        if (Math.abs(delta) < 10) return null;
        const want = Math.max(-1000, Math.min(1000, Math.round(ref.av - delta)));
        return want === currentAv ? null : want;
    }

    // The coach banner's step at song time t.
    const STEPS = {
        'get-ready': { title: 'GET READY', hint: 'Press Play. First part: eyes closed, play on every click.' },
        listen: { title: 'LISTEN', hint: 'Eyes closed. Play one note on every click.' },
        'next-watch': { title: 'NEXT: WATCH', hint: 'The clicks stop. Play exactly when each note hits the line.' },
        watch: { title: 'WATCH', hint: 'No clicks. Play exactly when each note hits the line.' },
        'next-play': { title: 'NEXT: PLAY', hint: 'Clicks and notes together. Play normally.' },
        play: { title: 'PLAY', hint: 'Clicks and notes. Play normally.' },
        done: { title: 'DONE', hint: 'Working out your results…' },
    };
    function coachState(plan, t) {
        const s = plan.sections, half = plan.beat * 0.5;
        let step;
        if (t < s.listen[0] - half) step = 'get-ready';
        else if (t < s.listen[1] - half) step = 'listen';
        else if (t < s.watch[0] - half) step = 'next-watch';
        else if (t < s.watch[1] - half) step = 'watch';
        else if (t < s.play[0] - half) step = 'next-play';
        else if (t < s.play[1]) step = 'play';
        else step = 'done';
        const sec = s[step];
        const beats = sec ? Math.round((sec[1] - sec[0]) / plan.beat) : 0;
        const beat = sec ? Math.max(0, Math.min(beats, Math.floor((t - sec[0]) / plan.beat + 0.5) + 1)) : 0;
        return Object.assign({ step, beat, beats }, STEPS[step]);
    }

    // Does a player belong to an expected input ({kind: 'guitar'|'kit'|'drumpad'|'five_fret', controllerKey?})?
    function matchesExpect(p, e) {
        if (!p || !e) return false;
        if (e.kind === 'guitar') return p.kind === 'guitar';
        if (e.kind === 'kit') return p.kind === 'drums' && !p.controllerKey;
        if (e.kind === 'drumpad') return p.kind === 'drums' && !!p.controllerKey && (!e.controllerKey || p.controllerKey === e.controllerKey);
        if (e.kind === 'five_fret') return p.kind === 'five_fret' && (!e.controllerKey || p.controllerKey === e.controllerKey);
        return false;
    }

    // Plain-words problems while a run plays: an expected input with no hits
    // 6+ beats into a section, or mostly misses.
    function warnings(players, expect, plan, t) {
        const out = [];
        const sec = sectionOf(plan, t);
        if (!sec || !Array.isArray(expect)) return out;
        const into = (t - plan.sections[sec][0]) / plan.beat;
        for (const e of expect) {
            const ps = Object.values(players || {}).filter((p) => matchesExpect(p, e));
            const n = ps.reduce((a, p) => a + p[sec].length, 0);
            const miss = ps.reduce((a, p) => a + (p.misses || 0), 0);
            const total = ps.reduce((a, p) => a + p.listen.length + p.watch.length + p.play.length, 0);
            if (into >= 6 && n === 0) {
                out.push({ label: e.label, text: e.kind === 'guitar'
                    ? 'No notes heard from ' + e.label + ' yet. Is Note Detection on and the guitar plugged in?'
                    : 'No hits from ' + e.label + ' yet. Is it connected and set up?' });
            } else if (miss >= 8 && miss > total) {
                out.push({ label: e.label, text: e.label + ': most hits are missing the notes. Play one note right on each beat.' });
            }
        }
        return out;
    }

    if (typeof module !== 'undefined' && module.exports) {
        module.exports = { median, iqr, sectionOf, analyse, followAv, MIN_N, TARGETS, targetFor, clampTarget,
            confidence, coachState, warnings, matchesExpect };
        return;
    }
    if (window.__syncLab) return;

    const API = '/api/plugins/sync_lab';
    const LS = { ref: 'synclab_av_ref_v1', follow: 'synclab_follow_v1', last: 'synclab_last_v1', prevSplit: 'synclab_prev_split_v1',
        pending: 'synclab_pending_v1', lock: 'synclab_lock_v1', ndRestore: 'synclab_nd_restore_v1' };
    const COL = { early: '#5fb6ff', late: '#ff9a40', ok: '#5dffa8', dim: '#6b7280', warn: '#ffc531' };
    const bus = window.feedBack || window.slopsmith;

    let plan = null;           // from /info (sections, blank window, file)
    let run = null;            // current calibration run
    const ui = { coach: null, blank: null, card: null };

    const lsGet = (k, d) => { try { const v = localStorage.getItem(k); return v == null ? d : JSON.parse(v); } catch (_) { return d; } };
    const lsSet = (k, v) => { try { localStorage.setItem(k, JSON.stringify(v)); } catch (_) { /* ignore */ } };
    const lsDel = (k) => { try { localStorage.removeItem(k); } catch (_) { /* ignore */ } };
    const fmt = (ms) => (ms > 0 ? '+' : '') + Math.round(ms) + ' ms';
    const esc = (s) => String(s == null ? '' : s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
    const audio = () => document.getElementById('audio');
    const avMs = () => { try { return Math.round(window.highway.getAvOffset()); } catch (_) { return 0; } };
    const avLocked = () => {
        try { return typeof window.isAvOffsetLocked === 'function' ? !!window.isAvOffsetLocked() : !!lsGet(LS.lock, null); } catch (_) { return false; }
    };
    const ndLoaded = () => !!(window.noteDetect || (window.__ndShared && window.__ndShared.instances));
    const ndLatMs = () => {
        if (!ndLoaded()) return null;
        try { if (window.__hwtNdLatency) return Math.round(window.__hwtNdLatency() * 1000); } catch (_) { /* fall through */ }
        try {
            const v = JSON.parse(localStorage.getItem('slopsmith_notedetect') || '{}').latencyOffset;
            return Math.round((Number.isFinite(v) ? v : 0.08) * 1000);
        } catch (_) { return 80; }
    };
    const drumsCfg = () => { try { return window.__drumsGetConfig ? window.__drumsGetConfig() : null; } catch (_) { return null; } };
    const drumOffMs = () => { const c = drumsCfg(); return c ? (c.inputOffsetMs || 0) : null; };
    const kickOffMs = () => { const c = drumsCfg(); return c ? (c.kickOffsetMs || 0) : null; };
    const drums = () => window.feedBackDrums || null;
    const fiveFret = () => window.feedBackFiveFret || null;
    const safeList = (api) => {
        try { return api && typeof api.listControllers === 'function' ? Promise.resolve(api.listControllers()).catch(() => []) : Promise.resolve([]); }
        catch (_) { return Promise.resolve([]); }
    };
    // Stored per-controller offsets: the plugin's API, else its settings key.
    function storedOffsets(api, lsKey) {
        try {
            if (api && typeof api.getControllerOffsets === 'function') return Promise.resolve(api.getControllerOffsets()).then((o) => o || {}).catch(() => ({}));
        } catch (_) { /* fall through */ }
        const s = lsGet(lsKey, null);
        return Promise.resolve(s && s.offsetMs && typeof s.offsetMs === 'object' ? s.offsetMs : {});
    }
    const FF_KEY = 'five_fret.settings.v1', DP_KEY = 'drums.gamepad.settings.v1';

    // Current value of every setting: { id: { label, value, connected } } (async: controller lists).
    async function readOffsets() {
        const out = {};
        out.av = { label: TARGETS.av.label, value: avMs() };
        const gl = ndLatMs();
        if (gl != null) out.guitarLat = { label: TARGETS.guitarLat.label, value: gl };
        const dk = drumOffMs();
        if (dk != null) {
            out.drumsKit = { label: TARGETS.drumsKit.label, value: dk };
            out.drumsKick = { label: TARGETS.drumsKick.label, value: kickOffMs() || 0 };
        }
        const [dl, dOff, fl, fOff] = await Promise.all([safeList(drums()), storedOffsets(drums(), DP_KEY), safeList(fiveFret()), storedOffsets(fiveFret(), FF_KEY)]);
        const add = (base, list, offs, what) => {
            const keys = new Set([...list.map((c) => c.controllerKey).filter(Boolean), ...Object.keys(offs)]);
            for (const k of keys) {
                const c = list.find((x) => x.controllerKey === k);
                out[base + ':' + k] = { label: (c && c.name ? c.name : k) + ' (' + what + ')', value: Number.isFinite(offs[k]) ? offs[k] : 0,
                    connected: !!(c && c.connected) };
            }
        };
        if (drums()) add('drumsPad', dl.filter((c) => c.kind !== 'kit'), dOff, 'drum controller');
        if (fiveFret()) add('fiveFret', fl, fOff, '5-Fret');
        return out;
    }

    function loadPlan() {
        return fetch(API + '/info').then((r) => r.json()).then((d) => { plan = d.ok ? d.plan : null; return plan; }).catch(() => null);
    }

    function isCalibrationSong() {
        if (!plan) return false;
        try {
            const i = window.highway.getSongInfo();
            return !!(i && i.title === plan.title && i.artist === plan.artist);
        } catch (_) { return false; }
    }

    // ── players: one per input (main / split panel × instrument × controller) ──
    const ss = () => window.feedBackSplitscreen || window.slopsmithSplitscreen;
    const splitActive = () => { try { return !!(ss() && ss().isActive && ss().isActive()); } catch (_) { return false; } };
    function panelIndexOfNode(el) {
        const wrap = document.getElementById('splitscreen-wrap');
        if (!wrap || !el || !el.parentElement) return -1;
        let n = el;
        while (n && n.parentElement !== wrap) n = n.parentElement;
        return n ? [...wrap.children].filter((c) => c.tagName === 'DIV').indexOf(n) : -1;
    }
    function panelOfCanvas(canvas) {
        try { if (splitActive()) { const i = ss().panelIndexFor(canvas); if (i != null && i >= 0) return i; } } catch (_) { /* ignore */ }
        return -1;
    }
    function player(key, kind, label, controllerKey) {
        if (!run) return null;
        const probe = { kind, controllerKey: controllerKey || null };
        // Only the inputs picked on the Calibrate screen count (a mic picking
        // up noise during a 5-Fret run must not become a guitar row).
        if (run.expect && !run.expect.some((e) => matchesExpect(probe, e))) return null;
        if (!run.players[key]) run.players[key] = { key, kind, label, controllerKey: controllerKey || null, listen: [], watch: [], play: [], misses: 0, live: [] };
        return run.players[key];
    }
    const prefix = (idx) => (idx >= 0 ? 'p' + idx : 'main');
    const panelLabel = (idx, label) => (idx >= 0 ? 'P' + (idx + 1) + ' ' : '') + label;
    function sample(p, sec, raw, judged) {
        p[sec].push(raw);
        p.live.push(judged);
        if (p.live.length > 8) p.live.shift();
        render();
    }
    function controllerName(kind, key) {
        const c = run && run.names && run.names[kind + ':' + key];
        return c || (kind === 'five_fret' ? '5-Fret controller' : 'Drum controller');
    }

    function addGuitar(j, panelIdx) {
        if (!run || !j || !Number.isFinite(j.noteTime)) return;
        const sec = sectionOf(plan, j.noteTime);
        if (!sec) return;
        let arr = 'Guitar';
        try { arr = (window.highway.getSongInfo() || {}).arrangement || arr; } catch (_) { /* ignore */ }
        const p = player(prefix(panelIdx) + '-g', 'guitar', panelLabel(panelIdx, panelIdx >= 0 ? 'guitar' : arr));
        if (!p) return;
        if (!Number.isFinite(j.timingError) || Math.abs(j.timingError) > 250) { if (!j.hit) p.misses++; render(); return; }
        sample(p, sec, j.timingError + (Number.isFinite(j.latencyMs) ? j.latencyMs : 0), j.timingError);
    }
    // Each judgment is dispatched on window AND on its detector's root (which
    // bubbles through document). The main player's is taken from the window
    // copy; a split panel's from the bubbling copy, whose target says which panel.
    function onGuitarWindow(e) {
        const j = e && e.detail;
        if (j && j.isDefault !== false && !splitActive()) addGuitar(j, -1);
    }
    function onGuitarDom(e) {
        const j = e && e.detail;
        if (!j || !splitActive()) return;
        const idx = panelIndexOfNode(e.target);
        if (idx >= 0) addGuitar(j, idx);
    }

    function onDrums(ev) {
        if (!run || !ev || !ev.detail) return;
        const { noteTime, errMs, inputOffsetMs, kickOffsetMs, controllerKey, canvas } = ev.detail;
        if (!Number.isFinite(noteTime) || !Number.isFinite(errMs)) return;
        const sec = sectionOf(plan, noteTime);
        if (!sec) return;
        const idx = panelOfCanvas(canvas);
        const key = controllerKey || null;
        const p = key
            ? player(prefix(idx) + '-d:' + key, 'drums', panelLabel(idx, controllerName('drums', key)), key)
            : player(prefix(idx) + '-d', 'drums', panelLabel(idx, 'Drum kit'));
        if (!p) return;
        sample(p, sec, errMs + (inputOffsetMs || 0) + (kickOffsetMs || 0), errMs);
    }

    function onFiveFret(e, missed) {
        const d = e && (e.detail || e);
        if (!run || !d || !Number.isFinite(d.noteTime)) return;
        const sec = sectionOf(plan, d.noteTime);
        if (!sec) return;
        const key = d.controllerKey || null;
        if (!key) return;
        const idx = panelOfCanvas(d.canvas);
        const p = player(prefix(idx) + '-ff:' + key, 'five_fret', panelLabel(idx, controllerName('five_fret', key)), key);
        if (!p) return;
        if (missed || !Number.isFinite(d.offsetMs)) { p.misses++; render(); return; }
        // Older 5-Fret builds don't send the offset in use: read the stored one.
        let inUse = d.inputOffsetMs;
        if (!Number.isFinite(inUse)) {
            const s = lsGet(FF_KEY, null);
            inUse = s && s.offsetMs && Number.isFinite(s.offsetMs[key]) ? s.offsetMs[key] : 0;
        }
        sample(p, sec, d.offsetMs + inUse, d.offsetMs);
    }

    function result() {
        const cur = { av: avMs(), offsets: run ? run.offsets : {}, guitarLat: ndLatMs(), drumOffset: drumOffMs() };
        const avNow = cur.av;
        if (run && Number.isFinite(run.av)) cur.av = run.av;
        return Object.assign(analyse(run ? run.players : {}, cur), { avNow, aheadMs: renderAhead() });
    }

    // ── render-ahead (Web Audio output latency the stems clock runs ahead by) ──
    const aheadSamples = [];
    function renderAhead() { return aheadSamples.length >= 5 ? median(aheadSamples) : NaN; }
    setInterval(() => {
        const a = audio();
        const v = window.__hwtRenderAheadMs;
        if (!a || a.paused || !Number.isFinite(v) || !(window.__hwtStemsSmooth && window.__hwtStemsSmooth())) return;
        aheadSamples.push(v);
        if (aheadSamples.length > 30) aheadSamples.shift();
    }, 250);

    // ── apply ──
    let setByUs = false;
    // Core ignores untagged A/V changes once the user has set the offset by
    // hand (feedBack#1046), so say what each one is: an Apply click is the
    // user's ('manual'); auto-follow keeps the user's calibration ('follow').
    function setAv(v, source) {
        setByUs = true;
        try { window.setAvOffsetMs(v, false, source); } finally { setByUs = false; lastAv = avMs(); }
    }
    function applyAv(v) { setAv(v, 'manual'); saveRef(v); }
    function detectors() {
        if (window.__hwtDetectors) return window.__hwtDetectors();
        const out = window.noteDetect ? [window.noteDetect] : [];
        const set = window.__ndShared && window.__ndShared.instances;
        if (set) for (const d of set) if (d && !out.includes(d)) out.push(d);
        return out;
    }
    function applyGuitar(ms) {
        for (const d of detectors()) {
            try { d.applySettings({ latencyOffset: ms / 1000 }); } catch (e) { console.warn('[sync_lab] latency apply failed', e); }
        }
    }
    function writeControllerOffsetLs(lsKey, key, ms) {
        const s = lsGet(lsKey, null) || {};
        s.offsetMs = Object.assign({}, s.offsetMs || {}, { [key]: ms });
        lsSet(lsKey, s);
    }
    // One setting → its owner. Resolves when done (controller setters may load assets).
    async function applyTarget(id, ms) {
        const v = clampTarget(id, ms);
        if (!Number.isFinite(v)) return false;
        const base = targetBase(id), key = String(id).slice(base.length + 1);
        try {
            if (base === 'av') applyAv(v);
            else if (base === 'guitarLat') applyGuitar(v);
            else if (base === 'drumsKit') {
                if (typeof window.__drumsSetInputOffset === 'function') window.__drumsSetInputOffset(v);
                else if (drums() && drums().setInputOffset) drums().setInputOffset(v);
            } else if (base === 'drumsKick') {
                if (drums() && typeof drums().setKickOffset === 'function') await drums().setKickOffset(v);
                else return false;
            } else if (base === 'drumsPad') {
                if (drums() && typeof drums().setControllerOffset === 'function') await drums().setControllerOffset(key, v);
                else writeControllerOffsetLs(DP_KEY, key, v);
            } else if (base === 'fiveFret') {
                if (fiveFret() && typeof fiveFret().setControllerOffset === 'function') await fiveFret().setControllerOffset(key, v);
                else {
                    writeControllerOffsetLs(FF_KEY, key, v);
                    try { if (bus && bus.emit) bus.emit('five_fret:offset-changed', { controllerKey: key, offsetMs: v }); } catch (_) { /* ignore */ }
                }
            } else return false;
        } catch (e) {
            console.warn('[sync_lab] apply failed', id, e);
            return false;
        }
        return true;
    }
    function saveRef(av) {
        const ah = renderAhead();
        if (Number.isFinite(ah)) lsSet(LS.ref, { av, aheadMs: Math.round(ah * 10) / 10, at: Date.now() });
    }

    // Lock: core's hand-set override (set by the 'manual' A/V write) plus Note
    // Detection's end-of-song A/V auto-calibrate off, so nothing but the
    // output-latency follow moves the calibration.
    function setNdAuto(on) {
        try { if (window.noteDetect && typeof window.noteDetect.setAutoCalibrate === 'function') window.noteDetect.setAutoCalibrate(!!on); } catch (_) { /* ignore */ }
    }
    function ndAuto() {
        try { return window.noteDetect && typeof window.noteDetect.isAutoCalibrate === 'function' ? !!window.noteDetect.isAutoCalibrate() : null; } catch (_) { return null; }
    }
    function lock() {
        if (!avLocked()) setAv(avMs(), 'manual');
        setNdAuto(false);
        lsSet(LS.lock, { at: Date.now() });
    }
    function unlock() {
        setAv(avMs(), 'reset');
        setNdAuto(true);
        lsDel(LS.lock);
    }

    // Manual A/V changes (Settings slider, player bar, [ / ] keys) become the
    // new reference, so auto-follow never undoes them. Core emits
    // settings:av-offset; older cores are watched by polling.
    let lastAv = null;
    const avChanged = (v) => { if (lastAv !== null && v !== lastAv && !setByUs) saveRef(v); lastAv = v; };
    if (bus && bus.on) bus.on('settings:av-offset', (e) => { const d = e && e.detail; if (d && Number.isFinite(d.ms)) avChanged(Math.round(d.ms)); });
    setInterval(() => avChanged(avMs()), 1000);

    // A/V auto-follow: once per song, a few seconds into playback.
    let followSong = null;
    setInterval(() => {
        const a = audio();
        if (!a || a.paused || !lsGet(LS.follow, true)) return;
        let key = null;
        try { const i = window.highway.getSongInfo(); key = i ? i.title + '|' + i.artist : null; } catch (_) { /* ignore */ }
        if (!key || key === followSong || aheadSamples.length < 10) return;
        followSong = key;
        const ref = lsGet(LS.ref, null);
        const ah = renderAhead();
        const want = followAv(ref, ah, avMs());
        if (want === null) return;
        setAv(want, 'follow');
        toast('Audio output latency changed (' + Math.round(ref.aheadMs) + ' → ' + Math.round(ah) + ' ms): A/V offset ' +
            ref.av + ' → ' + want + ' ms');
        post({ kind: 'av-follow', from: ref, aheadMs: ah, av: want });
    }, 1000);
    // Each new song gathers fresh latency samples.
    if (bus && bus.on) bus.on('song:loading', () => { aheadSamples.length = 0; });

    function toast(text) {
        const el = document.createElement('div');
        el.style.cssText = 'position:fixed;left:50%;top:70px;transform:translateX(-50%);z-index:9500;padding:8px 14px;border-radius:8px;' +
            'background:rgba(17,24,39,.95);border:1px solid #374151;color:#e5e7eb;font:13px system-ui,sans-serif;pointer-events:none';
        el.textContent = text;
        document.body.appendChild(el);
        setTimeout(() => el.remove(), 6000);
    }
    function post(entry) {
        try { fetch(API + '/log', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(entry) }).catch(() => {}); } catch (_) { /* ignore */ }
    }

    // ── in-song UI: coach, blank screen, results card ──
    const BTN = 'pointer-events:auto;cursor:pointer;margin-left:8px;padding:6px 14px;border-radius:8px;border:1px solid #4b5563;' +
        'background:#1f2937;color:#e5e7eb;font:600 13px system-ui,sans-serif;';
    const BTN_MAIN = BTN + 'background:#2563eb;border-color:#3b82f6;color:#fff;';
    function ensureUi() {
        if (!ui.coach) {
            ui.coach = document.createElement('div');
            ui.coach.className = 'fb-selectable';
            ui.coach.style.cssText = 'position:fixed;left:50%;top:12px;transform:translateX(-50%);z-index:9400;width:min(540px,92vw);padding:12px 18px 14px;' +
                'border-radius:14px;background:rgba(10,12,24,.92);border:1px solid #1f2937;color:#e5e7eb;font:14px system-ui,sans-serif;' +
                'text-align:center;pointer-events:none;box-shadow:0 10px 40px rgba(0,0,0,.5)';
            document.body.appendChild(ui.coach);
        }
        if (!ui.blank) {
            ui.blank = document.createElement('div');
            ui.blank.style.cssText = 'position:fixed;inset:0;z-index:9300;background:#000;display:none;align-items:center;justify-content:center;' +
                'flex-direction:column;color:#9ca3af;font:700 34px system-ui,sans-serif;text-align:center;pointer-events:none;';
            document.body.appendChild(ui.blank);
        }
    }
    function removeUi() {
        for (const k of Object.keys(ui)) { if (ui[k]) ui[k].remove(); ui[k] = null; }
    }

    // Last hits as dots on an early/late strip (±150 ms).
    function dotStrip(live) {
        let h = '<div style="position:relative;height:16px;flex:1;margin:0 8px;border-radius:8px;background:#111827;border:1px solid #1f2937">' +
            '<div style="position:absolute;left:50%;top:1px;bottom:1px;width:2px;margin-left:-1px;background:' + COL.ok + ';opacity:.7"></div>';
        live.forEach((err, i) => {
            const x = 50 + Math.max(-1, Math.min(1, err / 150)) * 48;
            const col = Math.abs(err) <= 30 ? COL.ok : err < 0 ? COL.early : COL.late;
            const op = 0.35 + 0.65 * (i + 1) / live.length;
            h += '<div style="position:absolute;top:2px;left:calc(' + x.toFixed(1) + '% - 5px);width:10px;height:10px;border-radius:5px;background:' + col +
                ';opacity:' + op.toFixed(2) + '"></div>';
        });
        return h + '</div>';
    }
    // Every picked input gets a row from the start, so a silent one shows 0.
    function expectedRows() {
        const ps = Object.values(run.players);
        if (!run.expect) return ps.map((p) => ({ label: p.label, players: [p] }));
        return run.expect.map((e) => ({ label: e.label, players: ps.filter((p) => matchesExpect(p, e)) }));
    }
    function coachHtml(t, blank) {
        const c = coachState(plan, t);
        const pct = c.beats ? Math.round(100 * c.beat / c.beats) : 0;
        const warn = warnings(run.players, run.expect, plan, t);
        if (blank) {
            let h = '<div style="color:#e5e7eb;font-size:56px;letter-spacing:.08em">LISTEN</div>' +
                '<div style="font-size:20px;font-weight:500;margin-top:10px">Eyes closed. Play one note on every click.</div>' +
                '<div style="font-size:16px;font-weight:500;margin-top:6px;color:#6b7280">Beat ' + c.beat + ' of ' + c.beats + '. The picture comes back for the next part.</div>' +
                '<div style="margin-top:28px;display:flex;gap:28px;justify-content:center">';
            for (const r of expectedRows()) {
                const n = r.players.reduce((a, p) => a + p.listen.length, 0);
                h += '<div><div style="font-size:44px;color:' + (n ? COL.ok : '#4b5563') + '">' + n + '</div><div style="font-size:14px;font-weight:500">' +
                    esc(r.label) + ' hits heard</div></div>';
            }
            h += '</div>';
            for (const w of warn) h += '<div style="margin-top:14px;font-size:16px;color:' + COL.warn + '">' + esc(w.text) + '</div>';
            return h;
        }
        let h = '<div style="font:800 26px system-ui;letter-spacing:.08em;color:#fff">' + c.title + '</div>' +
            '<div style="margin:2px 0 8px;color:#d1d5db">' + esc(c.hint) + '</div>';
        if (c.beats) {
            h += '<div style="height:6px;border-radius:3px;background:#1f2937;overflow:hidden"><div style="height:100%;width:' + pct + '%;background:#3b82f6"></div></div>' +
                '<div style="font-size:12px;color:' + COL.dim + ';margin-top:3px">beat ' + c.beat + ' of ' + c.beats + '</div>';
        }
        const rows = expectedRows();
        if (rows.length && c.step !== 'get-ready') {
            h += '<div style="margin-top:8px">';
            for (const r of rows) {
                const n = r.players.reduce((a, p) => a + p.listen.length + p.watch.length + p.play.length, 0);
                const live = r.players.reduce((a, p) => a.concat(p.live), []).slice(-8);
                h += '<div style="display:flex;align-items:center;font-size:12px;margin-top:4px">' +
                    '<div style="width:130px;text-align:left;color:#fff;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + esc(r.label) + '</div>' +
                    '<span style="color:' + COL.early + '">early</span>' + dotStrip(live) + '<span style="color:' + COL.late + '">late</span>' +
                    '<div style="width:62px;text-align:right;color:' + (n ? COL.ok : COL.dim) + '">' + n + ' hits</div></div>';
            }
            h += '</div>';
        }
        for (const w of warn) h += '<div style="margin-top:6px;font-size:13px;color:' + COL.warn + '">' + esc(w.text) + '</div>';
        return h;
    }

    function tick() {
        if (!plan) return;
        if (!isCalibrationSong()) {
            if (run) endRun();
            return;
        }
        if (!run) startRun();
        const a = audio();
        const t = a ? a.currentTime : 0;
        // Restarted from the top: start over.
        if (run.maxT > plan.sections.listen[0] + 2 && t < plan.sections.listen[0] - 0.5 && a && !a.paused) startRun();
        run.maxT = Math.max(run.maxT, t);
        ensureUi();
        const blank = a && !a.paused && t >= plan.blank[0] && t < plan.blank[1];
        ui.blank.style.display = blank ? 'flex' : 'none';
        if (!run.cardShown) {
            ui.coach.style.display = blank ? 'none' : '';
            const el = blank ? ui.blank : ui.coach;
            const html = coachHtml(t, blank);
            if (el.__html !== html) { el.innerHTML = html; el.__html = html; }
        }
        const finished = t >= plan.sections.play[1] + 0.3 || (a && a.ended);
        if (finished && !run.cardShown) showCard();
    }

    function startRun() {
        const pending = lsGet(LS.pending, null);
        const prevNdAuto = run ? run.prevNdAuto : ndAuto();
        run = { players: {}, maxT: 0, cardShown: false, split: splitActive(), mutedPads: false, startedAt: Date.now(), av: avMs(),
            expect: pending && Array.isArray(pending.expect) && pending.expect.length ? pending.expect : null,
            names: (pending && pending.names) || {}, offsets: {}, prevNdAuto, applied: false, picked: {}, done: {} };
        const mine = run;
        readOffsets().then((o) => { for (const [id, v] of Object.entries(o)) mine.offsets[id] = v.value; }).catch(() => {});
        // Pad sounds reach the ears late (Web Audio output latency): mute them so
        // the drummer plays to the clicks / gems, not to a late echo.
        if (!window.__drumsMutePads) { window.__drumsMutePads = true; run.mutedPads = true; }
        // Wide hit windows (+-250 ms) so hits far off (wrong A/V, by ear) are measured, not missed.
        window.__drumsCalibrationWindow = 0.5;
        window.__fiveFretCalibrationWindow = 0.5;
        if (window.__drumsRefreshParams) window.__drumsRefreshParams();
        try { if (fiveFret() && fiveFret().refreshParams) fiveFret().refreshParams(); } catch (_) { /* ignore */ }
        // Note Detection's end-of-song A/V sweep must not move A/V under the results.
        // Remembered, so a reload or crash mid-run still turns it back on.
        if (prevNdAuto) lsSet(LS.ndRestore, true);
        setNdAuto(false);
        if (ui.card) { ui.card.remove(); ui.card = null; }
    }
    function endRun() {
        if (run && run.mutedPads) window.__drumsMutePads = false;
        window.__drumsCalibrationWindow = 0;
        window.__fiveFretCalibrationWindow = 0;
        if (window.__drumsRefreshParams) window.__drumsRefreshParams();
        try { if (fiveFret() && fiveFret().refreshParams) fiveFret().refreshParams(); } catch (_) { /* ignore */ }
        // Locked (applied now or before): auto-calibrate stays off; else back as it was.
        if (run && run.prevNdAuto && !run.applied && !lsGet(LS.lock, null)) setNdAuto(true);
        lsDel(LS.ndRestore);
        run = null;
        lsDel(LS.pending);
        removeUi();
    }

    // ── results card ──
    // Rows: A/V first, then each player's setting. A row is ticked by default
    // when it is trustworthy and changes by 5 ms or more.
    function resultRows(res) {
        const rows = [];
        if (Number.isFinite(res.avNew)) {
            rows.push({ id: 'av', label: 'A/V offset', what: 'Picture vs sound (one per computer)', now: res.avNow, want: res.avNew,
                wanted: res.avNew, conf: res.avConfidence });
        }
        for (const r of res.rows) {
            if (!r.target || !Number.isFinite(r.suggested)) continue;
            const spec = targetSpec(r.target);
            rows.push({ id: r.target, label: r.label, what: spec ? spec.label : '', now: r.current, want: r.suggested, wanted: r.wanted, conf: r.confidence });
        }
        for (const row of rows) {
            row.change = Number.isFinite(row.now) ? row.want - row.now : NaN;
            row.same = Number.isFinite(row.change) && Math.abs(row.change) < 5;
            // Clamped to the setting's range (not just rounded to its step).
            const spec = targetSpec(row.id);
            row.limit = !!spec && Number.isFinite(row.wanted) && (row.wanted < spec.min || row.wanted > spec.max);
            if (!(row.id in run.picked)) run.picked[row.id] = row.conf.rating !== 'unreliable' && !row.same;
        }
        return rows;
    }
    function summaryLine(res) {
        const parts = [];
        if (Number.isFinite(res.d)) {
            const gap = Math.round(Math.abs(res.avNew - res.avNow));
            parts.push(gap < 5 ? 'Picture and sound already line up.' : 'Picture and sound were ' + gap + ' ms apart.');
        }
        for (const r of res.rows) {
            if (!Number.isFinite(r.eye) || r.n[1] < MIN_N) continue;
            const e = Math.round(r.eye);
            parts.push(r.label + (Math.abs(e) < 5 ? ' hits register on time.' : ' hits register ' + Math.abs(e) + ' ms ' + (e > 0 ? 'late.' : 'early.')));
        }
        return parts.join(' ');
    }

    async function onCardClick(ev) {
        const cb = ev.target.closest && ev.target.closest('input[data-pick]');
        if (cb) { run.picked[cb.dataset.pick] = cb.checked; return; }
        const b = ev.target.closest && ev.target.closest('button[data-act]');
        if (!b) return;
        ev.stopPropagation();
        const act = b.dataset.act;
        const res = result();
        const rows = resultRows(res);
        const applyRows = async (list) => {
            const ok = [];
            for (const row of list) { if (await applyTarget(row.id, row.want)) { run.done[row.id] = row.want; ok.push(row); } }
            if (!ok.length) return;
            run.applied = true;
            lock();
            const applied = ok.map((r) => ({ id: r.id, label: r.label, from: r.now, to: r.want }));
            post({ kind: 'apply', applied, result: summary(res) });
            const last = lsGet(LS.last, {}) || {};
            lsSet(LS.last, Object.assign(last, { applied: (last.applied || []).filter((a) => !ok.some((r) => r.id === a.id)).concat(applied), appliedAt: Date.now() }));
        };
        if (act === 'one') await applyRows(rows.filter((r) => r.id === b.dataset.id));
        else if (act === 'selected') await applyRows(rows.filter((r) => run.picked[r.id] && !(r.id in run.done)));
        else if (act === 'all') await applyRows(rows.filter((r) => !r.same && !(r.id in run.done)));
        else if (act === 'details') run.details = !run.details;
        else if (act === 'done') {
            if (ui.card) ui.card.remove();
            ui.card = null;
            try { const a = audio(); if (a && !a.paused) a.pause(); } catch (_) { /* ignore */ }
            if (typeof window.showScreen === 'function') window.showScreen('plugin-sync_lab');
            return;
        } else if (act === 'again') {
            const a = audio();
            if (ui.card) ui.card.remove();
            ui.card = null;
            startRun();
            if (a) { a.currentTime = 0; a.play(); }
            return;
        }
        if (run) renderCard();
    }

    function summary(res) {
        return { av: res.av, avNow: res.avNow, d: res.d, avNew: res.avNew, aheadMs: res.aheadMs, split: !!(run && run.split),
            rows: res.rows.map((r) => ({ label: r.label, kind: r.kind, target: r.target, n: r.n, ear: r.ear, eye: r.eye, both: r.both, spread: r.spread,
                d: r.d, input: r.input, current: r.current, rating: r.confidence.rating })),
            raw: run ? Object.values(run.players).map((p) => ({ key: p.key, listen: p.listen, watch: p.watch, play: p.play, misses: p.misses })) : [] };
    }

    function showCard() {
        run.cardShown = true;
        ui.coach.style.display = 'none';
        const res = result();
        lsSet(LS.last, Object.assign({ at: Date.now(), summaryText: summaryLine(res) }, summary(res), { raw: undefined }));
        post({ kind: 'calibration', result: summary(res) });
        renderCard();
    }

    const CONF_COL = { great: COL.ok, good: '#a3e635', rough: COL.warn, unreliable: '#f87171' };
    function renderCard() {
        if (!run) return;
        if (!ui.card) {
            ui.card = document.createElement('div');
            ui.card.className = 'fb-selectable';
            ui.card.style.cssText = 'position:fixed;left:50%;top:50%;transform:translate(-50%,-50%);z-index:9600;width:min(720px,94vw);max-height:90vh;overflow:auto;' +
                'padding:20px 24px;border-radius:16px;background:rgba(10,12,24,.97);border:1px solid #374151;color:#d1d5db;' +
                'font:14px system-ui,sans-serif;box-shadow:0 20px 60px rgba(0,0,0,.6)';
            ui.card.addEventListener('click', onCardClick);
            document.body.appendChild(ui.card);
        }
        const res = result();
        const rows = resultRows(res);
        const ms = (v) => (Number.isFinite(v) ? Math.round(v) + ' ms' : '?');
        const TH = '<th style="padding:4px 6px">';
        let h = '<div style="font:800 22px system-ui;color:#fff;margin-bottom:4px">Your timing results</div>' +
            '<div style="color:#e5e7eb;margin-bottom:14px;line-height:1.45">' + esc(summaryLine(res) || 'No hits were measured.') + '</div>';
        if (!rows.length) {
            h += '<div style="color:' + COL.warn + ';margin:10px 0">Nothing to suggest yet: each part needs at least ' + MIN_N + ' hits. ' +
                'Check that your instrument is connected (Note Detection on for guitar), then Run again.</div>';
        } else {
            h += '<table style="border-collapse:collapse;width:100%;font-size:13px"><tr style="color:' + COL.dim + ';text-align:left">' +
                TH + '</th>' + TH + 'Setting</th>' + TH + 'Now</th>' + TH + 'Suggested</th>' + TH + 'Change</th>' + TH + 'Confidence</th><th></th></tr>';
            for (const r of rows) {
                const done = r.id in run.done;
                const chg = r.same ? '<span style="color:' + COL.ok + '">already right</span>' : Number.isFinite(r.change) ? fmt(r.change) : '';
                h += '<tr style="border-top:1px solid #1f2937">' +
                    '<td style="padding:6px"><input type="checkbox" data-pick="' + esc(r.id) + '"' + (run.picked[r.id] ? ' checked' : '') + (done ? ' disabled' : '') + '></td>' +
                    '<td style="padding:6px"><div style="color:#fff;font-weight:600">' + esc(r.label) + '</div><div style="color:' + COL.dim + ';font-size:11px">' + esc(r.what) + '</div></td>' +
                    '<td style="padding:6px;white-space:nowrap">' + ms(r.now) + '</td>' +
                    '<td style="padding:6px;color:#fff;font-weight:700;white-space:nowrap">' + ms(r.want) +
                    (r.limit ? '<div style="color:' + COL.late + ';font-size:11px;font-weight:400">limit reached (wanted ' + r.wanted + ')</div>' : '') + '</td>' +
                    '<td style="padding:6px;white-space:nowrap">' + chg + '</td>' +
                    '<td style="padding:6px;color:' + CONF_COL[r.conf.rating] + '">' + esc(r.conf.words) + '</td>' +
                    '<td style="padding:6px;text-align:right">' + (done ? '<span style="color:' + COL.ok + '">✓ applied</span>'
                        : r.same ? '' : '<button data-act="one" data-id="' + esc(r.id) + '" style="' + BTN + '">Apply</button>') + '</td></tr>';
            }
            h += '</table>';
        }
        if (Object.keys(run.done).length) {
            h += '<div style="margin-top:12px;color:' + COL.ok + '">Saved and locked: automatic tools won\'t change your A/V offset now. ' +
                'You can fine-tune everything on the Calibrate screen.</div>';
        }
        h += '<div style="margin-top:12px"><button data-act="details" style="' + BTN + 'margin-left:0;background:transparent">' +
            (run.details ? 'Hide' : 'Show') + ' details</button></div>';
        if (run.details) {
            h += '<table style="border-collapse:collapse;width:100%;font-size:12px;margin-top:8px"><tr style="color:' + COL.dim + '">' +
                '<td>input</td><td>ear (eyes closed)</td><td>eye (no clicks)</td><td>both</td><td>spread</td><td>misses</td></tr>';
            for (const r of res.rows) {
                const c = (v, n) => n >= MIN_N && Number.isFinite(v) ? fmt(v) + ' <span style="color:' + COL.dim + '">(' + n + ')</span>'
                    : '<span style="color:' + COL.dim + '">' + n + ' hits</span>';
                const p = run.players[r.key];
                h += '<tr><td style="padding:3px 8px 3px 0;color:#fff">' + esc(r.label) + '</td><td>' + c(r.ear, r.n[0]) + '</td><td>' + c(r.eye, r.n[1]) +
                    '</td><td>' + c(r.both, r.n[2]) + '</td><td>' + (Number.isFinite(r.spread) ? Math.round(r.spread) + ' ms' : '–') + '</td><td>' + (p ? p.misses : 0) + '</td></tr>';
            }
            h += '</table><div style="color:' + COL.dim + ';font-size:11px;margin-top:4px">Medians, + = late. A/V = current + (eye − ear)' +
                (Number.isFinite(res.aheadMs) ? '; audio output latency ' + Math.round(res.aheadMs) + ' ms' : '') + '.</div>';
        }
        const anyLeft = rows.some((r) => !r.same && !(r.id in run.done));
        h += '<div style="margin-top:16px;display:flex;justify-content:flex-end;flex-wrap:wrap;gap:6px">' +
            (anyLeft ? '<button data-act="selected" style="' + BTN_MAIN + '">Apply selected</button><button data-act="all" style="' + BTN + '">Apply all</button>' : '') +
            '<button data-act="again" style="' + BTN + '">Run again</button><button data-act="done" style="' + BTN + '">Done</button></div>';
        ui.card.innerHTML = h;
    }

    // Late judgments (the last notes) still update an open results card.
    function render() { if (run && run.cardShown && ui.card) renderCard(); }

    // ── starting a calibration (Calibrate screen) ──
    // picks: [{ id, kind: 'guitar'|'bass'|'kit'|'drumpad'|'five_fret', controllerKey?, label }]
    const PANEL_VIZ = { guitar: '__viz__:highway_3d:Lead', bass: '__viz__:highway_3d:Bass', kit: '__viz__:drums:Drums',
        drumpad: '__viz__:drums:Drums', five_fret: '__viz__:five_fret:Lead' };
    async function start(picks) {
        if (!plan) await loadPlan();
        if (!plan) { alert('Calibrate: could not write the calibration song (is a song folder set in Settings?)'); return; }
        if (!picks || !picks.length) return;
        const expect = picks.map((p) => ({ kind: p.kind === 'bass' ? 'guitar' : p.kind, controllerKey: p.controllerKey || null, label: p.label }));
        const names = {};
        for (const p of picks) if (p.controllerKey) names[(p.kind === 'five_fret' ? 'five_fret' : 'drums') + ':' + p.controllerKey] = p.label;
        lsSet(LS.pending, { expect, names, at: Date.now() });
        const idx = (name) => Math.max(0, plan.arrangements.indexOf(name));
        if (picks.length > 1) {
            const prev = localStorage.getItem('splitscreenPanelPrefs');
            if (prev && !lsGet(LS.prevSplit, null)) lsSet(LS.prevSplit, prev);
            const panel = (arrName) => ({ arrName, lyrics: false, inverted: false, lefty: false, detectChannel: 'mono', barHidden: false, mastery: 1 });
            localStorage.setItem('splitscreenPanelPrefs', JSON.stringify(picks.map((p) => panel(PANEL_VIZ[p.kind]))));
            localStorage.setItem('splitscreenActive', 'true');
            await window.playSong(plan.file, 0);
            // Give the user's own split layout back once this one has been read.
            setTimeout(() => {
                const p = lsGet(LS.prevSplit, null);
                if (p) { try { localStorage.setItem('splitscreenPanelPrefs', p); } catch (_) { /* ignore */ } }
                lsDel(LS.prevSplit);
            }, 8000);
            return;
        }
        const btn = document.getElementById('btn-splitscreen');
        if (splitActive() && btn) btn.click();
        try { localStorage.setItem('splitscreenActive', 'false'); } catch (_) { /* ignore */ }
        const kind = picks[0].kind;
        const coreUi = window.feedBack && window.feedBack.ui;
        if (kind === 'five_fret' && coreUi && typeof coreUi.playSongAs === 'function') {
            coreUi.playSongAs(plan.file, { viz: 'five_fret', arrangement: idx('Lead') });
            return;
        }
        await window.playSong(plan.file, idx(kind === 'bass' ? 'Bass' : kind === 'kit' || kind === 'drumpad' ? 'Drums' : 'Lead'));
    }

    // ── Calibrate screen: a two-step wizard plus every timing setting ──
    const wiz = { step: 'pick', picks: [], tiles: [], seen: {} };

    async function buildTiles() {
        const tiles = [];
        const nd = ndLoaded();
        tiles.push({ id: 'guitar', kind: 'guitar', label: 'Real guitar', sub: 'Plugged in, through Note Detection', ok: nd, why: 'Note Detection plugin not loaded' });
        tiles.push({ id: 'bass', kind: 'bass', label: 'Real bass', sub: 'Plugged in, through Note Detection', ok: nd, why: 'Note Detection plugin not loaded' });
        tiles.push({ id: 'kit', kind: 'kit', label: 'Drum kit', sub: 'Electronic kit over MIDI', ok: !!drums(), why: 'Drums plugin not loaded' });
        const [dl, fl] = await Promise.all([safeList(drums()), safeList(fiveFret())]);
        const pads = dl.filter((c) => c.connected && c.kind !== 'kit');
        const ffs = fl.filter((c) => c.connected);
        for (const c of pads) {
            tiles.push({ id: 'drumpad:' + c.controllerKey, kind: 'drumpad', controllerKey: c.controllerKey, index: c.index, label: c.name || 'Drum controller',
                sub: 'Drum controller', ok: true });
        }
        for (const c of ffs) {
            tiles.push({ id: 'five_fret:' + c.controllerKey, kind: 'five_fret', controllerKey: c.controllerKey, index: c.index, label: c.name || '5-Fret controller',
                sub: c.kind === 'gamepad' ? '5-Fret on a gamepad' : '5-Fret guitar controller', ok: !!c.kind, why: 'Set it up in 5-Fret controls first' });
        }
        if (!pads.length && !ffs.length) {
            tiles.push({ id: 'none', kind: 'none', label: 'Controller', sub: '', ok: false, why: 'Plug in a drum or 5-Fret controller, then Refresh' });
        }
        return tiles;
    }

    function tileHtml(t) {
        const n = wiz.picks.findIndex((p) => p.id === t.id);
        return '<button class="sl-tile' + (n >= 0 ? ' on' : '') + '" data-tile="' + esc(t.id) + '"' + (t.ok ? '' : ' disabled') + '>' +
            '<div class="sl-tile-t">' + esc(t.label) + '</div><div class="sl-tile-s">' + esc(t.ok ? t.sub : t.why) + '</div>' +
            (n >= 0 ? '<div class="sl-tile-n">' + (n + 1) + '</div>' : '') + '</button>';
    }

    const CHECKS = {
        guitar: ['Tune to E standard.', 'Note Detection on, with your guitar input picked.'],
        bass: ['Tune to E standard.', 'Note Detection on, with your bass input picked.'],
        kit: ['Pad sounds are muted for the test (they reach your ears late), so you hear just the clicks.'],
        drumpad: ['Pad sounds are muted for the test, so you hear just the clicks.'],
        five_fret: ['Every note is an open strum: strum without holding a fret.'],
    };

    function renderWizard(root) {
        const box = root.querySelector('[data-k=wizard]');
        if (!box) return;
        let h = '';
        if (wiz.step === 'pick') {
            h += '<div class="sl-step">Step 1 of 2</div><div class="sl-h">What are you calibrating?</div>' +
                '<p class="sl-p">Pick one. Pick two to calibrate two players at once in split view.</p>' +
                '<div class="sl-tiles">' + wiz.tiles.map(tileHtml).join('') + '</div>' +
                '<div class="sl-actions"><button data-act="refresh">Refresh controllers</button>' +
                '<button class="sl-main" data-act="next"' + (wiz.picks.length ? '' : ' disabled') + '>Next</button></div>';
        } else {
            h += '<div class="sl-step">Step 2 of 2</div><div class="sl-h">Get ready</div>' +
                '<p class="sl-p">One 64-second song in three parts. Use the headphones or speakers you normally play with, volume up.</p>' +
                '<div class="sl-parts">' +
                '<div class="sl-part"><div class="sl-part-n">1</div><b>Listen</b><span>The screen goes black. Close your eyes and play on every click.</span></div>' +
                '<div class="sl-part"><div class="sl-part-n">2</div><b>Watch</b><span>The clicks stop. Play exactly when each note hits the line.</span></div>' +
                '<div class="sl-part"><div class="sl-part-n">3</div><b>Play</b><span>Clicks and notes together, as a check. Then you get your results.</span></div>' +
                '</div><div class="sl-checks">';
            for (const p of wiz.picks) {
                const live = p.kind === 'drumpad' || p.kind === 'five_fret' || p.kind === 'kit';
                const seen = wiz.seen[p.id];
                const status = live
                    ? '<div class="sl-live' + (seen ? ' ok' : '') + '">' + (seen ? '✓ Input received' : (p.kind === 'kit' ? 'Hit a pad to test it…' : 'Press any button to test it…')) + '</div>'
                    : '<div class="sl-live' + (ndLoaded() ? ' ok' : '') + '">' + (ndLoaded() ? '✓ Note Detection is loaded' : 'Note Detection is not loaded') + '</div>';
                h += '<div class="sl-check"><div class="sl-check-t">' + esc(p.label) + '</div><ul>' +
                    (CHECKS[p.kind] || []).map((c) => '<li>' + esc(c) + '</li>').join('') + '</ul>' + status + '</div>';
            }
            h += '</div><div class="sl-actions"><button data-act="back">Back</button>' +
                '<button class="sl-main sl-big" data-act="go">Start calibration</button></div>';
        }
        box.innerHTML = h;
    }

    // Live input check on the Get ready step: gamepad buttons (polled while
    // the step shows) and MIDI note-ons for the kit.
    let pollTimer = null, midiAccess = null;
    function midiNote(e) {
        const d = e && e.data;
        if (!d || (d[0] & 0xf0) !== 0x90 || !d[2]) return;
        const p = wiz.picks.find((x) => x.kind === 'kit');
        if (p && !wiz.seen[p.id]) { wiz.seen[p.id] = true; renderScreen(); }
    }
    function startLiveCheck() {
        stopLiveCheck();
        pollTimer = setInterval(() => {
            if (wiz.step !== 'ready') { stopLiveCheck(); return; }
            let pads = [];
            try { pads = navigator.getGamepads ? [...navigator.getGamepads()] : []; } catch (_) { /* ignore */ }
            for (const p of wiz.picks) {
                if (wiz.seen[p.id] || !Number.isInteger(p.index)) continue;
                const g = pads[p.index];
                if (g && (g.buttons.some((b) => b && (b.pressed || b.value > 0.5)) || g.axes.some((a, i) => i > 1 && Math.abs(a) > 0.6))) {
                    wiz.seen[p.id] = true;
                    renderScreen();
                }
            }
        }, 60);
        if (wiz.picks.some((p) => p.kind === 'kit') && navigator.requestMIDIAccess) {
            navigator.requestMIDIAccess().then((m) => {
                midiAccess = m;
                for (const inp of m.inputs.values()) inp.addEventListener('midimessage', midiNote);
            }).catch(() => {});
        }
    }
    function stopLiveCheck() {
        if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
        if (midiAccess) { for (const inp of midiAccess.inputs.values()) inp.removeEventListener('midimessage', midiNote); midiAccess = null; }
    }

    const DESC = {
        av: 'Moves the picture against the sound. One per computer, every highway.',
        guitarLat: 'Note Detection\'s latency, every guitar and bass panel.',
        drumsKit: 'Subtracted from every pad hit on the MIDI kit.',
        drumsKick: 'Added on top for kick pedal hits only.',
    };
    async function renderOverview(root) {
        const box = root.querySelector('[data-k=overview]');
        if (!box) return;
        const offs = await readOffsets();
        const locked = avLocked();
        const lk = lsGet(LS.lock, null);
        let h = '<div class="sl-row"><div>A/V lock<div class="sl-d">' +
            (locked ? 'Locked' + (lk && lk.at ? ' by Calibrate on ' + esc(new Date(lk.at).toLocaleDateString()) : '') +
                ': nothing changes your timing automatically (except following a headphone / speaker change, if that is on below).'
                : 'Unlocked: Note Detection may adjust the A/V offset after songs.') + '</div></div>' +
            '<div class="sl-v"><span class="sl-badge' + (locked ? ' on' : '') + '">' + (locked ? 'Locked' : 'Unlocked') + '</span>' +
            '<button data-act="' + (locked ? 'unlock' : 'lock') + '">' + (locked ? 'Unlock' : 'Lock') + '</button></div></div>';
        for (const [id, o] of Object.entries(offs)) {
            const spec = targetSpec(id);
            const base = targetBase(id);
            const nudge = spec.step > 1 ? spec.step : 5;
            const desc = DESC[id] || (base === 'drumsPad' || base === 'fiveFret' ? 'This controller only.' + (o.connected ? '' : ' Not connected right now.') : '');
            h += '<div class="sl-row"><div>' + esc(o.label) + '<div class="sl-d">' + esc(desc) + '</div></div><div class="sl-v sl-nudge">' +
                '<button data-nudge="' + esc(id) + '" data-d="-' + nudge + '">−</button>' +
                '<input type="number" data-set="' + esc(id) + '" value="' + Math.round(o.value) + '" min="' + spec.min + '" max="' + spec.max + '" step="' + spec.step + '"> ms' +
                '<button data-nudge="' + esc(id) + '" data-d="' + nudge + '">+</button></div></div>';
        }
        const ref = lsGet(LS.ref, null);
        h += '<div class="sl-row"><div>Audio output latency<div class="sl-d">How far ahead of your speakers / headphones the song is rendered (Web Audio reports it).</div></div><div class="sl-v">' +
            (Number.isFinite(renderAhead()) ? Math.round(renderAhead()) + ' ms (while playing)'
                : ref && Number.isFinite(ref.aheadMs) ? ref.aheadMs + ' ms (at last calibration)' : 'measured while a song plays') + '</div></div>';
        h += '<div class="sl-row"><div>Follow headphone / speaker changes<div class="sl-d">When the output latency changes, shift the A/V offset by the same amount so picture and sound stay together.</div></div>' +
            '<div class="sl-v"><input type="checkbox" data-k="follow"' + (lsGet(LS.follow, true) ? ' checked' : '') + '></div></div>';
        const last = lsGet(LS.last, null);
        h += '<div class="sl-row"><div>Last calibration<div class="sl-d">' +
            (last && last.at ? esc(new Date(last.at).toLocaleString()) + (last.summaryText ? ': ' + esc(last.summaryText) : '') +
                (last.applied && last.applied.length ? ' Applied: ' + last.applied.map((a) => esc(a.label) + ' → ' + a.to + ' ms').join(', ') + '.' : '')
                : 'None yet.') + '</div></div></div>';
        box.innerHTML = h;
    }

    async function renderScreen() {
        const root = document.getElementById('sync-lab-root');
        if (!root) return;
        renderWizard(root);
        await renderOverview(root);
        measurePadLatency(root.querySelector('[data-k=pad]'));
    }

    let padLatency = null;
    async function measurePadLatency(el) {
        if (!el) return;
        if (padLatency != null) { el.textContent = padLatency; return; }
        try {
            const ctx = new (window.AudioContext || window.webkitAudioContext)({ latencyHint: 'interactive' });
            // Silent probe: a muted oscillator just to get the output clock running.
            const g = ctx.createGain(); g.gain.value = 0; const o = ctx.createOscillator(); o.connect(g).connect(ctx.destination); o.start();
            await new Promise((r) => setTimeout(r, 500));
            const ts = ctx.getOutputTimestamp();
            const ms = (ctx.currentTime - ts.contextTime) * 1000 - (performance.now() - ts.performanceTime);
            const base = ctx.baseLatency * 1000, out = (ctx.outputLatency || 0) * 1000;
            await ctx.close();
            padLatency = '~' + Math.round(ms) + ' ms from a pad hit to the sound (Web Audio: ' + Math.round(base) + ' ms buffer + ' + Math.round(out) +
                ' ms device), plus up to a frame of main-thread delay.';
        } catch (_) { padLatency = 'could not be measured'; }
        el.textContent = padLatency;
    }

    async function refreshTiles() {
        wiz.tiles = await buildTiles();
        wiz.picks = wiz.picks.filter((p) => wiz.tiles.some((t) => t.id === p.id && t.ok));
        renderScreen();
    }

    // A hand edit on the Calibrate screen: apply it; an A/V edit locks too.
    async function setOne(id, v) {
        await applyTarget(id, v);
        if (id === 'av') lock();
        renderScreen();
    }

    function wireScreen() {
        const root = document.getElementById('sync-lab-root');
        if (!root || root.__wired) return;
        root.__wired = true;
        root.addEventListener('click', (e) => {
            const tile = e.target.closest && e.target.closest('button[data-tile]');
            if (tile) {
                const t = wiz.tiles.find((x) => x.id === tile.dataset.tile);
                if (!t || !t.ok) return;
                const i = wiz.picks.findIndex((p) => p.id === t.id);
                if (i >= 0) wiz.picks.splice(i, 1);
                else { if (wiz.picks.length >= 2) wiz.picks.shift(); wiz.picks.push(t); }
                renderScreen();
                return;
            }
            const n = e.target.closest && e.target.closest('button[data-nudge]');
            if (n) {
                const input = root.querySelector('input[data-set="' + n.dataset.nudge + '"]');
                setOne(n.dataset.nudge, (input ? Number(input.value) : 0) + Number(n.dataset.d));
                return;
            }
            const b = e.target.closest && e.target.closest('button[data-act]');
            if (!b) return;
            const act = b.dataset.act;
            if (act === 'refresh') refreshTiles();
            else if (act === 'next' && wiz.picks.length) { wiz.step = 'ready'; wiz.seen = {}; renderScreen(); startLiveCheck(); }
            else if (act === 'back') { wiz.step = 'pick'; stopLiveCheck(); renderScreen(); }
            else if (act === 'go') { stopLiveCheck(); const picks = wiz.picks.slice(); wiz.step = 'pick'; start(picks); }
            else if (act === 'lock') { lock(); renderScreen(); }
            else if (act === 'unlock') { unlock(); renderScreen(); }
            else if (act === 'module-sound') {
                if (typeof window.__drumsSetPadVolume === 'function') { window.__drumsSetPadVolume(0); b.textContent = '✓ pad sounds off'; }
                else b.textContent = 'Drums plugin not loaded';
            }
        });
        root.addEventListener('change', (e) => {
            const el = e.target;
            if (el.matches && el.matches('input[data-set]')) setOne(el.dataset.set, Number(el.value));
            else if (el.matches && el.matches('input[data-k=follow]')) lsSet(LS.follow, !!el.checked);
        });
        refreshTiles();
    }

    window.addEventListener('notedetect:hit', onGuitarWindow);
    window.addEventListener('notedetect:miss', onGuitarWindow);
    document.addEventListener('notedetect:hit', onGuitarDom);
    document.addEventListener('notedetect:miss', onGuitarDom);
    window.addEventListener('drums:judgment', onDrums);
    if (bus && bus.on) {
        bus.on('five_fret:hit', (e) => onFiveFret(e, false));
        bus.on('five_fret:miss', (e) => onFiveFret(e, true));
        bus.on('screen:changed', (e) => {
            const id = e && e.detail && (e.detail.screen || e.detail.id);
            if (id === 'plugin-sync_lab' || !id) { wireScreen(); refreshTiles(); } else stopLiveCheck();
        });
    }
    // A run that never ended (reload / crash mid-song) left auto-calibrate off: put it back
    // once Note Detection is up, unless a calibration has been applied (locked) since.
    (function restoreNdAuto(tries) {
        if (!lsGet(LS.ndRestore, false) || run) return;
        if (!window.noteDetect) { if (tries < 60) setTimeout(() => restoreNdAuto(tries + 1), 500); return; }
        if (!lsGet(LS.lock, null)) setNdAuto(true);
        lsDel(LS.ndRestore);
    })(0);
    loadPlan();
    setInterval(tick, 50);
    (function waitScreen() { if (document.getElementById('sync-lab-root')) wireScreen(); else setTimeout(waitScreen, 500); })();

    window.__syncLab = { version: '3.0.0', start, applyTarget, readOffsets, lock, unlock,
        analyse: () => (run ? summary(result()) : null), get run() { return run; }, get plan() { return plan; } };
})();

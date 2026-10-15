// Highway Tweaks (feedBack 0.3 port of the Slopsmith 0.2.9 user plugin).
//
// Personal sync and diagnostics tweaks that 0.3 core and the other plugins
// don't cover. What the 0.2.9 version did that is now elsewhere:
//   colorblind G/B strings ........ per-string colours are configurable in core
//   capability_inspector / sloppak_converter / unpack-lock perf fixes ... core
//   note_detect patches (capo, retune, chord runs, judgment hooks) ... notedetect@custom
//   timing gauge, A/V check, stats card, score, solos, compact card ... scorecard plugin
//   split-view A/V offset on panel highways ... core (static/js/highway-av-offset.js)
//   controls tray minimise ........ the v3 transport auto-hides
//   JUCE clock smoothing .......... was disabled; dropped
//
// What stays here:
//  1. Precise stems clock. The stems plugin drives #audio.currentTime from
//     AudioContext.currentTime, which only moves once per audio callback
//     (~10 ms steps). getOutputTimestamp() maps context time to
//     performance.now() to ~0.1 ms, so we extrapolate from it (keeping
//     currentTime's render-ahead offset, slowly averaged, so A/V is
//     unchanged). Published for other plugins (multiplayer, drums, Sync Lab):
//       window.__hwtStemsSmooth()       true while a sloppak plays on the stems clock
//       window.__hwtPreciseAudioTime()  #audio time, frame-exact (NaN when not smooth)
//       window.__hwtRenderAheadMs       ms the stems AudioContext renders ahead of the speakers
//       window.__hwtFrameTime(bundle)   frame-exact render time for a renderer bundle (NaN = use bundle.currentTime)
//       window.__hwtRenderOffset(bundle) render minus audio time (chart + A/V offset) of that bundle's highway
//       window.__hwtLastSetT            last raw audio time fed to the main highway
//     Every renderer bundle is stamped with ITS highway's last setTime
//     (bundle.__hwtSetT): splitscreen panels are fed on their own timer.
//     2.1: core runs its own frame clock now (#974, highway.setClockSource):
//     the stems clock is put in FRONT of core's source on every highway, so
//     core draws and highway.getTime() reads it, rather than a second
//     smoothing layer on top; render offsets come from bundle.renderOffset.
//  2. 3D highway: draws from that clock (smoothNow) and Rock Band-style
//     verdicts (hits burst and vanish, misses go grey and drift past the
//     line, partly hit chords get an amber plate). The 3D highway's source is
//     rewritten on load; any anchor that no longer matches skips that piece.
//     Kill switches: localStorage highwayTweaksNoRbVerdicts / highwayTweaksNoFrameClock = '1'.
//  3. window.__hwtDetectors(): every live Note Detection instance.
//  4. Frame-drop watchdog: while a song plays, 5 s windows of frame times; two
//     bad windows in a row post a snapshot (canvases, WebGL contexts, DOM
//     size, focus, highway perf) to the log. highwayTweaksSnapshot() posts one now.
//  5. Judgment logs: every counted Note Detection judgment (with per-string
//     verdicts) and every judged drum hit (`drums:judgment`), posted per song
//     (kind "judgments" / "drum_judgments"). Off: highwayTweaksNoJudgmentLog = '1'.
//  6. Drums input offset: +30 ms once, when unset (measured 2026-10-10 on the
//     Alesis: hits landed a steady +30..+37 ms late at 0). Sync Lab measures it properly.
//
// Log: <config>/highway_tweaks/jank_log.jsonl (GET /api/plugins/highway_tweaks/log?n=20&kind=).
(function () {
    'use strict';

    // ── Pure helpers (unit-tested in tests/plugins/highway_tweaks) ──────────

    // Precise clock from an AudioContext's output timestamp. read() takes
    // ctx.currentTime, ctx.getOutputTimestamp(), performance.now(), the
    // (coarse) #audio time sampled with that currentTime and the playback
    // rate, and returns the frame-exact audio time plus the render-ahead.
    function createStemsClock() {
        let off = null;
        return {
            reset() { off = null; },
            read(ct, ts, perfNow, audioT, rate) {
                if (!ts || !(ts.performanceTime > 0) || !Number.isFinite(ts.contextTime)
                        || !Number.isFinite(ct) || !Number.isFinite(audioT)) return null;
                const est = ts.contextTime + (perfNow - ts.performanceTime) / 1000;
                const d = ct - est;
                if (off === null || Math.abs(d - off) > 0.2) off = d;
                else off += (d - off) * 0.002;
                const r = Number.isFinite(rate) && rate > 0 ? rate : 1;
                return { t: audioT + (est + off - ct) * r, renderAheadMs: d * 1000 };
            },
        };
    }

    // A highway's render offset (render time - raw audio time: chart offset +
    // its A/V offset). Core's bundle carries it as renderOffset since the
    // render clock became a frame-time clock (#974); there bundle.currentTime
    // minus the last setTime() value also holds however far the clock moved
    // since that 60 Hz sample (0..17 ms, varying), so it is only the
    // fallback for an older core.
    function renderOffsetOf(bundle, setT) {
        if (!bundle) return NaN;
        if (Number.isFinite(bundle.renderOffset)) return bundle.renderOffset;
        return typeof setT === 'number' && Number.isFinite(bundle.currentTime) ? bundle.currentTime - setT : NaN;
    }

    // Frame time for a bundle: precise audio time + that highway's render
    // offset. NaN when unusable (callers fall back to bundle.currentTime).
    function frameTimeFor(bundleTime, renderOffset, preciseAudio) {
        if (!Number.isFinite(bundleTime) || !Number.isFinite(renderOffset) || !Number.isFinite(preciseAudio)) return NaN;
        const t = preciseAudio + renderOffset;
        return Math.abs(t - bundleTime) < 0.1 ? t : NaN;
    }

    // Core's frame clock source (highway.setClockSource, #974) with the
    // precise stems clock in front: one clock for the renderer AND for
    // highway.getTime() readers (Note Detection), instead of core running its
    // phase-locked loop on the coarse #audio time while the 3D highway draws
    // from the stems clock. A base reading of null ("host is driving time",
    // count-in) always wins.
    function composeClockSource(precise, base) {
        const fn = function () {
            const b = typeof base === 'function' ? base() : NaN;
            if (b === null) return null;
            const p = precise();
            return Number.isFinite(p) ? p : b;
        };
        fn.__hwtClock = true;
        fn.__hwtBase = base;
        return fn;
    }

    // Frame-interval window statistics for the watchdog.
    function frameStats(intervals, elapsedMs) {
        const sorted = intervals.slice().sort((a, b) => a - b);
        const pct = (p) => +sorted[Math.min(sorted.length - 1, Math.floor(p * sorted.length))].toFixed(1);
        const median = pct(0.5);
        const dropped = intervals.filter((d) => d > median * 1.5).length;
        return {
            fps: +(intervals.length * 1000 / elapsedMs).toFixed(1),
            droppedPct: +(100 * dropped / intervals.length).toFixed(1),
            p50: median, p90: pct(0.9), p99: pct(0.99),
        };
    }

    // Compact log row for a Note Detection judgment event detail.
    function judgmentRow(j) {
        const r = (x, k) => (Number.isFinite(x) ? Math.round(x * k) / k : null);
        return {
            t: r(j.noteTime, 1000), c: j.chord ? 1 : 0, h: j.hit ? 1 : 0, ts: j.timingState || null,
            te: Number.isFinite(j.timingError) ? j.timingError : null, hs: j.hitStrings, tt: j.totalStrings,
            sc: r(j.score, 100), pe: r(j.pitchError, 1), sec: j.section || null,
            v: Array.isArray(j.strings) ? j.strings.map((s) => ({ s: s.s, f: s.f, d: s.hit ? 1 : 0,
                ce: Number.isFinite(s.centsError) ? s.centsError : null, snr: r(s.snr, 10) })) : null,
        };
    }

    // ── 3D highway source patches ───────────────────────────────────────────
    // smoothNow: draw from the frame-exact clock when it's available.
    const SMOOTH_NOW_PREFIX = `function smoothNow(bundle) {
            {
                // [highway_tweaks] Frame-exact clock (precise stems clock read now
                // + this highway's render offset) instead of bundle.currentTime,
                // which app.js / splitscreen sampled on a 60 Hz timer.
                const __t = window.__hwtFrameTime ? window.__hwtFrameTime(bundle) : NaN;
                if (Number.isFinite(__t)) {
                    _clkAudioT = bundle.currentTime; _clkPerf = performance.now(); _clkRate = 1;
                    window.__h3dFrameNow = __t;
                    return (_frameNow = __t);
                }
            }
`;

    // Rock Band-style verdicts on the 3D highway (needs a note-state
    // provider, i.e. Note Detection on). Stock highway_3d only re-tints the
    // outline of a judged gem that sits on the now line, and its chord-frame
    // verdict colour is never drawn (the frame stops at the line, the verdict
    // lands ~0.4 s later), so chord runs give almost no feedback. Instead:
    //  - hit: the gem vanishes in a burst in its string colour; a fully hit
    //    chord adds one burst across the chord box;
    //  - miss: the gem (and its sustain) turns dark grey and keeps scrolling
    //    past the now line; a fully missed chord's frame turns grey and
    //    scrolls past too;
    //  - partly hit chord: an amber frame scrolls past with its grey notes.
    // Hit vs miss differs in brightness and motion (burst-and-gone vs grey
    // and moving), not only in hue, so it reads with colour blindness.
    // Kill switch: localStorage.highwayTweaksNoRbVerdicts = '1', then reload.
    const RB_DECL = `
        // [highway_tweaks] Rock Band-style verdicts. __rbV: verdict latch per note
        // (key __rbKey(t, s)) or chord (key -1 - verdictKey) ->
        // { v: 'hit' | 'miss' | 'chord', t0: performance.now() ms, t, end: chart s, x, y, s, w, h }.
        const __rbV = new Map();
        window.__rbVerdicts = __rbV;   // debug handle
        let __rbFx = null, __rbPlate = null, __rbBoxGeo = null, __rbGrey = null, __rbGreyOl = null;
        const __RB_AMBER = 0xffa31a, __RB_MISS_FRAME = 0x9aa0ad, __RB_BURST_S = 0.3, __RB_SHARDS = 8;
        // Past the now line notes head straight at the camera; misses drift at a fraction
        // of the highway speed so they stay on screen for the verdict window.
        const __RB_PAST_SPEED = 0.3;
        function __rbKey(t, s) { return Math.round(t * 1e4) * 10 + s; }
        function __rbFxMesh(x, y, z, sx, sy, sz, hex, op) {
            const m = __rbFx.get();
            m.position.set(x, y, z);
            m.rotation.set(0, 0, 0);
            m.scale.set(sx, sy, sz);
            m.material.color.setHex(hex);
            m.material.opacity = op;
        }
        function __rbPlateAt(x, y, z, sx, sy, hex, op) {
            const m = __rbPlate.get();
            m.position.set(x, y, z);
            m.rotation.set(0, 0, 0);
            m.scale.set(sx, sy, 0.5 * K);
            m.material.color.setHex(hex);
            m.material.opacity = op;
        }
        function __rbDrawFx(now) {
            if (!__rbFx) return;
            __rbFx.reset();
            if (__rbPlate) __rbPlate.reset();
            const pn = performance.now();
            for (const [k, e] of __rbV) {
                if (now < e.t - 0.25 || now > e.end + 2) { __rbV.delete(k); continue; }
                if (e.v === 'miss') continue;
                const p = (pn - e.t0) / 1000 / __RB_BURST_S;
                if (!(p >= 0 && p < 1)) continue;
                const q = 1 - p;
                if (e.v === 'chord') {
                    const sw = e.w / NW, sh = e.h / NH, g = 1 + 0.18 * p;
                    __rbFxMesh(e.x, e.y, 0, sw * g, sh * g, 0.6, 0xbff6ff, 0.55 * q * q);
                    for (let i = 0; i < 12; i++) {
                        const a = (i / 12) * Math.PI * 2 + 0.3;
                        const r = 0.5 + 0.35 * p;
                        __rbFxMesh(e.x + Math.cos(a) * e.w * r, e.y + Math.sin(a) * e.h * r, 0.002, 0.3, 0.3, 1, 0xffffff, q);
                    }
                    continue;
                }
                const col = (activePalette && activePalette[e.s] != null) ? activePalette[e.s] : 0xffffff;
                const g = 1.15 + 1.4 * p;
                __rbFxMesh(e.x, e.y, 0, e.w * g, e.h * g, 1.5, col, 0.95 * q * Math.sqrt(q));
                __rbFxMesh(e.x, e.y, 0.002, e.w * (0.9 + 0.5 * p), e.h * (0.9 + 0.5 * p), 2, 0xffffff, q * q);
                const d = NW * (0.55 + 1.9 * p);
                for (let i = 0; i < __RB_SHARDS; i++) {
                    const a = (i / __RB_SHARDS) * Math.PI * 2 + (k % 7) * 0.4;
                    __rbFxMesh(e.x + Math.cos(a) * d * (e.w > 1.5 ? 0.5 * e.w : 1), e.y + Math.sin(a) * d * 0.7, 0.004,
                        0.24, 0.32, 1, col, q);
                }
            }
        }
`;
    const RB_LATCH = `
            // [highway_tweaks] Rock Band-style verdict latch (see __rbV).
            const __rbK = __rbKey(n.t, n.s);
            let __rbE = __rbV.get(__rbK);
            if (__rbE && dt > 0.25) { __rbV.delete(__rbK); __rbE = undefined; }
            if (!__rbE && _ndHasProvider && dt < 0.12 && (_ndGood || _ndState === 'miss')) {
                __rbE = { v: _ndGood ? 'hit' : 'miss', t0: performance.now(), t: n.t, end: susEnd, x, y: y + techniqueYNow, s,
                    w: n.f === 0 ? (40 * K / NW) * openWScale : 1, h: n.f === 0 ? 0.1 * openSlabThickMul : 1 };
                __rbV.set(__rbK, __rbE);
            }
            const __rbHit = !!__rbE && __rbE.v === 'hit' && _ndState !== 'miss';
            const __rbMiss = !!__rbE && __rbE.v === 'miss';
            if (__rbMiss) {
                _ndState = 'miss'; _ndGood = false;
                if (!_ndCs) { _ndCs = 'miss'; _ndCsIsObj = false; }
                noteZ = dZ(dt) * __RB_PAST_SPEED;
            }
`;
    const RB_CHORD_SCAN = `let allHit = chordNotes.length > 0, anyMiss = false, anyHit = false, anyNull = false, anyState = false;
                                // [highway_tweaks] scan every constituent (partial = amber); the
                                // per-note latch fills in verdicts the provider has already dropped.
                                for (const cn of chordNotes) {
                                    let cs = null;
                                    try { cs = _ndGetNoteState(cn, ch.t); } catch (e) { cs = null; }
                                    let st = (cs && typeof cs === 'object') ? cs.state : cs;
                                    if (st !== 'hit' && st !== 'active' && st !== 'miss') {
                                        const __e = __rbV.get(__rbKey(ch.t, cn.s));
                                        if (__e) st = __e.v;
                                    }
                                    if (st === 'hit' || st === 'active') { anyHit = true; anyState = true; }
                                    else if (st === 'miss') { anyMiss = true; allHit = false; anyState = true; }
                                    else { allHit = false; anyNull = true; }
                                }
                                const __late = chDt < -_ND_UNMATCHED_LATCH_AFTER;
                                if (allHit) {
                                    _chordVerdicts.set(verdictKey, 'green');
                                    rimHex = CHORD_BOX_HIT_BRIGHT_HEX;
                                    __rbV.set(-1 - verdictKey, { v: 'chord', t0: performance.now(), t: ch.t, end: ch.t,
                                        x: cx, y: cY, s: 0, w: width, h: height });
                                } else if (anyHit && (anyMiss || __late)) {
                                    _chordVerdicts.set(verdictKey, 'amber');
                                    rimHex = __RB_AMBER;
                                } else if (anyMiss && (!anyNull || __late)) {
                                    _chordVerdicts.set(verdictKey, 'red');
                                    rimHex = CHORD_BOX_MISS_DARK_HEX;
                                } else if (__late && !anyState) {
                                    _chordVerdicts.set(verdictKey, 'unmatched');
                                }
                                // else: no verdict yet → leave teal default`;

    // Past the line the stock frame is hairline bars + a near-transparent
    // gradient fill, so a verdict frame gets its own solid plate instead.
    const RB_PLATE = `{
                            // [highway_tweaks] amber (partly hit) / grey (missed) chord plate past the line
                            const __rbL = _chordVerdicts.get(verdictKey);
                            if (chDt <= 0 && _ndHasProvider && __rbPlate && (__rbL === 'amber' || __rbL === 'red')) {
                                const hex = __rbL === 'amber' ? __RB_AMBER : __RB_MISS_FRAME;
                                const op = Math.max(0, 1 + chDt / NOTEDETECT_GEM_VERDICT_WINDOW);
                                const pz = dZ(chDt) * __RB_PAST_SPEED;
                                const pw = width, ph = fullChordBoxH, py = (yMinF + yMaxF) * 0.5;
                                const bt = Math.max(1.2 * K, ph * 0.07);
                                __rbPlateAt(cx, py, pz, pw, ph, hex, 0.3 * op);
                                __rbPlateAt(cx, py - ph / 2 + bt / 2, pz, pw, bt, hex, op);
                                __rbPlateAt(cx, py + ph / 2 - bt / 2, pz, pw, bt, hex, op);
                                __rbPlateAt(cx - pw / 2 + bt / 2, py, pz, bt, ph, hex, op);
                                __rbPlateAt(cx + pw / 2 - bt / 2, py, pz, bt, ph, hex, op);
                            }
                        }
                        `;

    // Apply every rbVerdicts edit to a copy; null if any anchor is missing.
    function rbVerdicts(code) {
        const edits = [
            // state + burst drawing, next to the provider handle
            [/( {8}let _ndHasProvider = false;[^\n]*\n)/, (m) => m + RB_DECL],
            // burst pool + grey materials, created with the note pool
            ['            pNote = pool(noteG, () => new T.Mesh(gNote, mStr[0]));', (m) => m +
                '\n            __rbFx = pool(noteG, () => { const fx = new T.Mesh(gNote, new T.MeshBasicMaterial({ color: 0xffffff, transparent: true,' +
                ' opacity: 1, blending: T.AdditiveBlending, depthWrite: false })); fx.renderOrder = 950; return fx; });' +
                '\n            __rbBoxGeo = new T.BoxGeometry(1, 1, 1);' +
                '\n            __rbPlate = pool(noteG, () => { const pl = new T.Mesh(__rbBoxGeo, new T.MeshBasicMaterial({ color: 0xffffff, transparent: true,' +
                ' opacity: 1, depthWrite: false, depthTest: false })); pl.renderOrder = 940; return pl; });' +
                '\n            __rbGrey = new T.MeshLambertMaterial({ color: 0x8a8f9c, emissive: 0x2c2e34, transparent: true, opacity: 0.92, depthWrite: false });' +
                '\n            __rbGreyOl = new T.MeshLambertMaterial({ color: 0x2a2c33, emissive: 0x000000, transparent: true, opacity: 1, depthWrite: false });'],
            ['            mMissOutline?.dispose?.();', (m) => m + ' __rbGrey?.dispose?.(); __rbGreyOl?.dispose?.(); __rbBoxGeo?.dispose?.(); __rbFx = __rbPlate = __rbBoxGeo = __rbGrey = __rbGreyOl = null; __rbV.clear();'],
            // draw the bursts each frame
            ['            const now = smoothNow(bundle);', (m) => m + ' __rbDrawFx(now);'],
            // a latched miss keeps the gem alive past its linger time (until the verdict window ends)
            ["if (_probeSt !== 'hit' && _probeSt !== 'active' && _probeSt !== 'miss') return;",
                () => "if (_probeSt !== 'hit' && _probeSt !== 'active' && _probeSt !== 'miss'" +
                    " && (__rbV.get(__rbKey(n.t, n.s)) || {}).v !== 'miss') return;"],
            ['const noteZ = sustained ? 0 : Math.min(0, dZ(dt));', () => 'let noteZ = sustained ? 0 : Math.min(0, dZ(dt));'],
            [/( {12})const _showHit = \(_ndState === 'miss'\) \? false/, (m) => RB_LATCH + m],
            // hit gems are gone; missed gems draw (grey) past the linger time
            ['if (!effSkipBody && !arpGhostOnlyMode && !_overLinger) {', (() => {
                let i = 0;
                return () => (i++ === 0
                    ? 'if (!effSkipBody && !arpGhostOnlyMode && (!_overLinger || __rbMiss) && !__rbHit) {'
                    : 'if (!effSkipBody && !arpGhostOnlyMode && !_overLinger && !__rbHit) {');
            })(), 2],
            ['outline.material = (n.ac && !_ndVerdict) ? mAccentOutline[s] : _ndOutline;',
                () => 'outline.material = __rbMiss ? __rbGreyOl : ((n.ac && !_ndVerdict) ? mAccentOutline[s] : _ndOutline);'],
            ['if (_ndFaceMat) {', () => 'if (_ndFaceMat && !__rbMiss) {'],
            ['core.material = n.ac ? mAccentCore[s] : mStr[s];', () => 'core.material = __rbMiss ? __rbGrey : (n.ac ? mAccentCore[s] : mStr[s]);'],
            ['core.geometry = (!n.ac && gNoteGrad[s]) ? gNoteGrad[s] : gNote;', () => 'core.geometry = (!__rbMiss && !n.ac && gNoteGrad[s]) ? gNoteGrad[s] : gNote;'],
            // missed sustains: grey trail
            ["const _susOlMat = _ndState === 'miss' ? mMissOutline", () => "const _susOlMat = _ndState === 'miss' ? __rbGreyOl"],
            ['tr.material = _ndState ? mGlow[s] : mSus[s];', () => "tr.material = _ndState === 'miss' ? __rbGrey : (_ndState ? mGlow[s] : mSus[s]);"],
            ['body.material = _ndState ? mGlow[s] : mSus[s];', () => "body.material = _ndState === 'miss' ? __rbGrey : (_ndState ? mGlow[s] : mSus[s]);"],
            // chord verdict: partial = amber; amber / grey frames keep drawing past the line
            [/let allHit = chordNotes\.length > 0;[\s\S]*?\/\/ else: no verdict yet → leave teal default/, () => RB_CHORD_SCAN],
            [/\} else if \(latched === 'red'\) \{/, () => "} else if (latched === 'amber') {\n                                rimHex = __RB_AMBER;\n                            } else if (latched === 'red') {"],
            // amber / grey verdict plate where the frame would be, drifting past the line
            ['if (chDt > 0) { // framebox only on highway, not on the fretboard', (m) => RB_PLATE + m],
        ];
        let out = code;
        for (const [find, rep, want = 1] of edits) {
            const n = typeof find === 'string'
                ? out.split(find).length - 1
                : (out.match(new RegExp(find.source, 'g')) || []).length;
            if (n !== want) {
                console.warn('[highway_tweaks] rb-verdicts anchor matched ' + n + 'x (want ' + want + '):', String(find).slice(0, 80));
                return null;
            }
            out = typeof find === 'string' ? out.split(find).map((p, i) => (i ? rep(find) : '') + p).join('') : out.replace(find, rep);
        }
        return { code: out };
    }


    function patchHighway3d(code, hits, opts) {
        opts = opts || {};
        if (!opts.noFrameClock) {
            code = code.replace(/function smoothNow\(bundle\) \{\r?\n/, () => { hits.push('frame-clock'); return SMOOTH_NOW_PREFIX; });
        }
        if (!opts.noRb) {
            const rb = rbVerdicts(code);
            if (rb) { code = rb.code; hits.push('rb-verdicts'); }
            else console.warn('[highway_tweaks] rb-verdicts: highway_3d changed, keeping the stock hit/miss look');
        }
        return code;
    }

    if (typeof module !== 'undefined' && module.exports) {
        module.exports = { createStemsClock, renderOffsetOf, frameTimeFor, composeClockSource, frameStats, judgmentRow, rbVerdicts, patchHighway3d };
        return;
    }
    if (window.__highwayTweaks) return;
    window.__highwayTweaks = { version: '2.1.0' };

    const bus = window.feedBack || window.slopsmith;
    const lsGet = (k) => { try { return localStorage.getItem(k); } catch (_) { return null; } };
    const LOG_URL = '/api/plugins/highway_tweaks/log';
    function post(entry, keepalive) {
        try {
            fetch(LOG_URL, { method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(entry), keepalive: !!keepalive }).catch(() => {});
        } catch (_) { /* ignore */ }
    }

    // ── 2. Rewrite the 3D highway's script on load ──────────────────────────
    // This plugin sorts first ("_Highway Tweaks"), so it sees the loader's
    // <script data-plugin-id="highway_3d"> before it is appended.
    const PATCHERS = {
        highway_3d(code, hits) {
            return patchHighway3d(code, hits, {
                noRb: lsGet('highwayTweaksNoRbVerdicts') === '1',
                noFrameClock: lsGet('highwayTweaksNoFrameClock') === '1',
            });
        },
    };
    const origAppendChild = Node.prototype.appendChild;
    Node.prototype.appendChild = function (el) {
        const id = el && el.tagName === 'SCRIPT' && el.dataset ? el.dataset.pluginId : null;
        if (!id || !PATCHERS[id] || !el.src || el.__hwtPatched || el.type === 'module') return origAppendChild.call(this, el);
        el.__hwtPatched = true;
        const parent = this, src = el.src;
        fetch(src).then((r) => {
            if (!r.ok) throw new Error('HTTP ' + r.status);
            return r.text();
        }).then((code) => {
            const hits = [];
            const patched = PATCHERS[id](code, hits);
            if (hits.length) {
                el.src = URL.createObjectURL(new Blob([patched + '\n//# sourceURL=' + src], { type: 'text/javascript' }));
                console.log('[highway_tweaks] patched ' + id + ':', hits.join(', '));
            }
        }).catch((e) => {
            console.warn('[highway_tweaks] could not patch ' + id + ', loading stock:', e);
        }).finally(() => origAppendChild.call(parent, el));
        return el;
    };

    // ── 1. Precise stems clock + per-highway setTime stamps ─────────────────
    const NATIVE_PAUSED = (() => {
        try { return Object.getOwnPropertyDescriptor(HTMLMediaElement.prototype, 'paused').get; } catch (_) { return null; }
    })();
    const clock = createStemsClock();
    let clockCtx = null;
    const audioEl = () => document.getElementById('audio');
    function stemsCtx() {
        const b = window.feedBack || window.slopsmith;
        const g = b && b.stems && b.stems.audioGraph;
        return g && g.context ? g.context : null;
    }
    // The stems transport owns #audio while a sloppak plays: its shimmed
    // `paused` is false while the real element stays paused.
    function stemsSmooth() {
        const a = audioEl(), ctx = stemsCtx();
        if (!a || !ctx || ctx.state !== 'running' || typeof ctx.getOutputTimestamp !== 'function') return false;
        if (a.paused) return false;
        return !NATIVE_PAUSED || NATIVE_PAUSED.call(a) === true;
    }
    function preciseAudioTime() {
        if (!stemsSmooth()) return NaN;
        const a = audioEl(), ctx = stemsCtx();
        if (ctx !== clockCtx) { clock.reset(); clockCtx = ctx; }
        let ts = null;
        try { ts = ctx.getOutputTimestamp(); } catch (_) { return NaN; }
        const ct = ctx.currentTime;
        const res = clock.read(ct, ts, performance.now(), a.currentTime, Number(a.playbackRate));
        if (!res) return NaN;
        window.__hwtRenderAheadMs = res.renderAheadMs;
        return res.t;
    }
    window.__hwtStemsSmooth = stemsSmooth;
    window.__hwtPreciseAudioTime = preciseAudioTime;
    window.__hwtRenderAheadMs = NaN;
    // Keep __hwtRenderAheadMs fresh for readers that poll the value.
    setInterval(() => { if (stemsSmooth()) preciseAudioTime(); }, 100);

    const setTOf = (bundle) => (bundle && typeof bundle.__hwtSetT === 'number' ? bundle.__hwtSetT : window.__hwtLastSetT);
    window.__hwtFrameTime = function (bundle) {
        if (!bundle || bundle.isPlaying === false) return NaN;
        return frameTimeFor(bundle.currentTime, renderOffsetOf(bundle, setTOf(bundle)), preciseAudioTime());
    };
    window.__hwtRenderOffset = function (bundle) {
        return renderOffsetOf(bundle, setTOf(bundle));
    };

    function stampRenderer(hw, r) {
        if (!r || typeof r.draw !== 'function' || r.__hwtStamp === hw) return;
        const draw = r.__hwtOrigDraw || r.draw;
        r.__hwtOrigDraw = draw;
        r.__hwtStamp = hw;
        r.draw = function (bundle) {
            if (bundle && typeof bundle === 'object') bundle.__hwtSetT = hw.__hwtLastSetT;
            return draw.apply(this, arguments);
        };
    }
    function hookInstance(hw, isMain) {
        if (!hw || typeof hw.setTime !== 'function' || hw.__hwtSetTime) return hw;
        hw.__hwtSetTime = true;
        const origSet = hw.setTime;
        hw.setTime = isMain
            ? function (t) { hw.__hwtLastSetT = t; window.__hwtLastSetT = t; return origSet.apply(this, arguments); }
            : function (t) { hw.__hwtLastSetT = t; return origSet.apply(this, arguments); };
        if (typeof hw.setClockSource === 'function' && lsGet('highwayTweaksNoFrameClock') !== '1') {
            // Keep the stems clock in front of whatever source core (or anyone
            // later) installs.
            const origClock = hw.setClockSource;
            const install = (base) => origClock.call(hw, base && base.__hwtClock ? base : composeClockSource(preciseAudioTime, base));
            hw.setClockSource = function (fn) { return install(typeof fn === 'function' ? fn : null); };
            try { install(typeof hw.getClockSource === 'function' ? hw.getClockSource() : null); } catch (e) { console.warn('[highway_tweaks] clock source hook failed:', e); }
        }
        if (typeof hw.setRenderer === 'function') {
            const origR = hw.setRenderer;
            hw.setRenderer = function (r) { try { stampRenderer(hw, r); } catch (_) { /* ignore */ } return origR.apply(this, arguments); };
        }
        return hw;
    }
    (function hookMain() {
        const hw = window.highway;
        if (!hw || typeof hw.setTime !== 'function') { setTimeout(hookMain, 500); return; }
        hookInstance(hw, true);
    })();
    (function hookCreateHighway() {
        const orig = window.createHighway;
        if (typeof orig !== 'function') { setTimeout(hookCreateHighway, 500); return; }
        if (orig.__hwt) return;
        const wrapped = function () {
            const hw = orig.apply(this, arguments);
            try { if (hw !== window.highway) hookInstance(hw, false); } catch (e) { console.warn('[highway_tweaks] panel highway hook failed:', e); }
            return hw;
        };
        Object.assign(wrapped, orig);
        wrapped.__hwt = true;
        window.createHighway = wrapped;
    })();

    // ── 3. Every Note Detection instance ────────────────────────────────────
    window.__hwtDetectors = function () {
        const out = [];
        if (window.noteDetect) out.push(window.noteDetect);
        const set = window.__ndShared && window.__ndShared.instances;
        if (set) for (const d of set) if (d && !out.includes(d)) out.push(d);
        return out;
    };
    // Note Detection's latency (s): every instance shares the saved setting
    // (applySettings persists it), so read it from there.
    window.__hwtNdLatency = function () {
        try {
            const v = JSON.parse(lsGet('slopsmith_notedetect') || '{}').latencyOffset;
            return Number.isFinite(v) ? v : 0.08;
        } catch (_) { return 0.08; }
    };

    // ── 4. Frame-drop watchdog ──────────────────────────────────────────────
    const ctxInfo = new WeakMap();
    const ctxCanvases = new Set();
    const origGetContext = HTMLCanvasElement.prototype.getContext;
    HTMLCanvasElement.prototype.getContext = function (type, ...rest) {
        const ctx = origGetContext.call(this, type, ...rest);
        if (ctx && !ctxInfo.has(this)) {
            ctxInfo.set(this, { type, at: Math.round(performance.now() / 1000) });
            ctxCanvases.add(new WeakRef(this));
        }
        return ctx;
    };
    let rafCalls = 0;
    const origRaf = window.requestAnimationFrame;
    window.requestAnimationFrame = function (cb) { rafCalls++; return origRaf.call(window, cb); };

    const events = [];
    function noteEvent(type, extra) {
        events.push(Object.assign({ type, t: Math.round(performance.now() / 1000) }, extra || {}));
        if (events.length > 40) events.shift();
    }
    document.addEventListener('visibilitychange', () => noteEvent('visibility', { state: document.visibilityState }));
    window.addEventListener('resize', () => noteEvent('resize', { w: innerWidth, h: innerHeight, dpr: devicePixelRatio }));
    window.addEventListener('blur', () => noteEvent('blur'));
    window.addEventListener('focus', () => noteEvent('focus'));

    function canvasReport() {
        const out = [];
        for (const ref of ctxCanvases) {
            const c = ref.deref();
            if (!c) { ctxCanvases.delete(ref); continue; }
            const info = ctxInfo.get(c) || {};
            const r = c.isConnected ? c.getBoundingClientRect() : null;
            let lost = null;
            if (/webgl/.test(info.type)) {
                try { lost = origGetContext.call(c, info.type).isContextLost(); } catch (_) { /* ignore */ }
            }
            out.push({
                type: info.type, id: c.id || null, cls: (c.className && String(c.className).slice(0, 40)) || null,
                px: c.width + 'x' + c.height, css: r ? Math.round(r.width) + 'x' + Math.round(r.height) : null,
                connected: c.isConnected, shown: !!(r && r.width && r.height && c.offsetParent !== null),
                lost, bornAtS: info.at,
            });
        }
        return out;
    }

    function snapshot(kind, stats) {
        const hw = window.highway;
        let perf = null; try { perf = hw && hw.getPerfStats ? hw.getPerfStats() : null; } catch (_) { /* ignore */ }
        const canvases = canvasReport();
        const activeScreen = document.querySelector('.screen.active');
        const entry = {
            kind, stats,
            uptimeMin: +(performance.now() / 60000).toFixed(1),
            domNodes: document.getElementsByTagName('*').length,
            audiosPlaying: [...document.querySelectorAll('audio')].filter((a) => !a.paused).length,
            infiniteAnimations: (() => { try { return document.getAnimations().filter((a) => a.playState === 'running').length; } catch (_) { return null; } })(),
            canvasCount: canvases.length,
            webglLive: canvases.filter((c) => /webgl/.test(c.type) && c.lost === false).length,
            webglHiddenOrDetached: canvases.filter((c) => /webgl/.test(c.type) && c.lost === false && !c.shown).length,
            canvases: canvases.filter((c) => /webgl/.test(c.type) || (c.connected && c.css && c.css !== '0x0')),
            screen: activeScreen ? activeScreen.id : null,
            win: { w: innerWidth, h: innerHeight, dpr: devicePixelRatio },
            focused: document.hasFocus(), visibility: document.visibilityState,
            heapMB: performance.memory ? Math.round(performance.memory.usedJSHeapSize / 1048576) : null,
            highway: perf,
            renderer: (() => { try { return hw && hw.isDefaultRenderer ? (hw.isDefaultRenderer() ? '2d' : 'plugin') : null; } catch (_) { return null; } })(),
            stemsClock: stemsSmooth(), renderAheadMs: Number.isFinite(window.__hwtRenderAheadMs) ? Math.round(window.__hwtRenderAheadMs * 10) / 10 : null,
            recentEvents: events.slice(-15),
        };
        console.warn('[highway_tweaks] ' + kind, entry);
        post(entry);
    }

    const WINDOW_MS = 5000;
    let intervals = [], lastFrame = 0, winStart = 0, rafAtWinStart = 0;
    let lastChartT = NaN, lastMoveAt = -Infinity, playingSince = 0, badWindows = 0;
    let lastJankLogAt = -Infinity, baselineLogged = false;
    function isPlaying() {
        const hw = window.highway;
        if (!hw || typeof hw.getTime !== 'function') return false;
        let t; try { t = hw.getTime(); } catch (_) { return false; }
        const nowP = performance.now();
        if (Number.isFinite(t) && t !== lastChartT) { lastChartT = t; lastMoveAt = nowP; }
        return nowP - lastMoveAt < 250 && document.visibilityState === 'visible';
    }
    function tick(now) {
        origRaf.call(window, tick);
        if (!isPlaying()) { intervals = []; lastFrame = 0; winStart = 0; playingSince = 0; return; }
        if (!playingSince) playingSince = now;
        if (lastFrame) intervals.push(now - lastFrame);
        lastFrame = now;
        if (!winStart) { winStart = now; rafAtWinStart = rafCalls; return; }
        if (now - winStart < WINDOW_MS || intervals.length < 30) return;
        const stats = frameStats(intervals, now - winStart);
        stats.rafPerFrame = +((rafCalls - rafAtWinStart) / intervals.length).toFixed(2);
        intervals = []; winStart = now; rafAtWinStart = rafCalls;
        window.__highwayTweaks.lastStats = stats;
        if (!baselineLogged && now - playingSince > 60000 && stats.droppedPct < 3) {
            baselineLogged = true;
            snapshot('baseline', stats);
        }
        badWindows = stats.droppedPct >= 10 ? badWindows + 1 : 0;
        if (badWindows >= 2 && now - lastJankLogAt > 120000) {
            lastJankLogAt = now;
            snapshot('jank', stats);
        }
    }
    origRaf.call(window, tick);
    window.highwayTweaksSnapshot = () => snapshot('manual', window.__highwayTweaks.lastStats || null);

    // ── 5. Judgment logs ────────────────────────────────────────────────────
    const songNow = () => {
        const cs = bus && bus.currentSong;
        return cs ? { filename: cs.filename, arrangement: cs.arrangement, title: cs.title } : null;
    };
    const avNow = () => { try { return Math.round(window.highway.getAvOffset()); } catch (_) { return null; } };
    if (lsGet('highwayTweaksNoJudgmentLog') !== '1') {
        let gBuf = [], gSong = null, dBuf = [], dSong = null, dOffset = null;
        const seen = new WeakSet();   // each judgment is dispatched on window AND bubbles from its panel
        const onJudgment = (e) => {
            const j = e && e.detail;
            if (!j || typeof j !== 'object' || seen.has(j)) return;
            seen.add(j);
            if (j.isDefault === false || j.counted === false) return;   // main player only, scored notes only
            if (!gSong) gSong = songNow();
            gBuf.push(judgmentRow(j));
            if (gBuf.length >= 2000) flush('full');
        };
        window.addEventListener('notedetect:hit', onJudgment);
        window.addEventListener('notedetect:miss', onJudgment);
        const onDrum = (e) => {
            const d = e && (e.detail || e);
            if (!d || !Number.isFinite(d.noteTime) || !Number.isFinite(d.errMs)) return;
            if (seen.has(d)) return;
            seen.add(d);
            if (!dSong) dSong = songNow();
            if (Number.isFinite(d.inputOffsetMs)) dOffset = d.inputOffsetMs;
            dBuf.push({ t: Math.round(d.noteTime * 1000) / 1000, e: Math.round(d.errMs * 10) / 10, p: d.pad });
            if (dBuf.length >= 4000) flush('full');
        };
        window.addEventListener('drums:judgment', onDrum);
        try { if (bus && bus.on) bus.on('drums:judgment', onDrum); } catch (_) { /* ignore */ }

        const flush = (reason, keepalive) => {
            if (gBuf.length) {
                let nd = null;
                try { nd = JSON.parse(lsGet('slopsmith_notedetect') || 'null'); } catch (_) { /* ignore */ }
                post({ kind: 'judgments', reason, song: gSong, av_offset_ms: avNow(), settings: nd && {
                    latencyOffset: nd.latencyOffset, timingTolerance: nd.timingTolerance, timingHitThreshold: nd.timingHitThreshold,
                    chordTimingHitThreshold: nd.chordTimingHitThreshold, pitchTolerance: nd.pitchTolerance, chordHitRatio: nd.chordHitRatio },
                    events: gBuf }, keepalive);
                gBuf = [];
            }
            if (dBuf.length) {
                const ss = window.feedBackSplitscreen || window.slopsmithSplitscreen;
                let split = false;
                try { split = !!(ss && ss.isActive && ss.isActive()); } catch (_) { /* ignore */ }
                post({ kind: 'drum_judgments', reason, song: dSong, av_offset_ms: avNow(), input_offset_ms: dOffset, split, events: dBuf }, keepalive);
                dBuf = [];
            }
        };
        if (bus && bus.on) {
            bus.on('song:ended', () => flush('ended'));
            bus.on('song:loading', () => { flush('next-song'); gSong = null; dSong = null; });
            bus.on('song:stop', () => flush('stop'));
        }
        window.addEventListener('pagehide', () => flush('unload', true));
    }

    // ── 6. Drums input offset seed (once) ───────────────────────────────────
    try {
        if (!localStorage.getItem('hwt.drumInputOffsetSeed')) {
            const cur = Number(localStorage.getItem('drums_input_offset_ms') || 0);
            if (!cur) {
                // Read by the drums plugin when it loads (it sorts after this one).
                localStorage.setItem('drums_input_offset_ms', '30');
                if (typeof window.__drumsSetInputOffset === 'function') window.__drumsSetInputOffset(30);
            }
            localStorage.setItem('hwt.drumInputOffsetSeed', '1');
        }
    } catch (_) { /* storage blocked */ }
})();

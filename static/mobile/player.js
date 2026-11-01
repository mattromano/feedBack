// Full-screen highway player for the phone app.
//
// Mounts core's static/highway.js standalone: createHighway() + init(canvas,
// container) + connect(url, {secondary: true, onSongInfo, onError}).
//   * secondary:true stops highway.js broadcasting global song state
//     (window.feedBack.currentSong / song:* events) — there is no app shell.
//   * onSongInfo makes highway.js skip its own HUD + <audio>.src handling, so
//     this module sets #audio's src itself.
//   * onError stops highway.js falling back to alert() on a server error.
// window.feedBack is left undefined: every highway.js access to it is guarded.
//
// Offline mode replays the stored arrangement through a fake WebSocket
// (fakews.js) that is swapped into window.WebSocket only for the synchronous
// duration of connect().
//
// Renderer: the 3D Highway plugin (h3d.js) installed with h.setRenderer(),
// the desktop's default view. Falls back to highway.js's built-in 2D
// renderer when WebGL2 is missing, the plugin fails to load or init, or the
// user picks 2D in Settings.

// highway.js has no ES exports: evaluating it publishes window.createHighway
// (and a default window.highway instance this app never uses).
import '../highway.js';
import { createFakeWebSocketClass, rewriteSongInfoMessages, songInfoFbmKeys } from './fakews.js';
import { getSong, getBlob } from './store.js';
import { applyZoom, canRun3D, createH3DRenderer, getHighwayMode, loadH3DFactory, minRenderScale } from './h3d.js';

const SPEED_PRESETS = [50, 60, 70, 80, 90, 100];
const HINT_KEY = 'fbm.wakeLockHintShown';

const $ = (id) => document.getElementById(id);

let _hw = null;        // one highway instance, reused across opens
let _session = null;   // state of the open player, or null
let _rendererKind = null; // '3d' | '2d': what is installed on _hw
let _h3d = null;          // the installed 3D renderer instance
let _h3dBroken = '';      // why 3D failed this page lifetime ('' = fine)

function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
}

function fmtTime(s) {
    if (!Number.isFinite(s) || s < 0) s = 0;
    const m = Math.floor(s / 60);
    const sec = Math.floor(s % 60);
    return `${m}:${String(sec).padStart(2, '0')}`;
}

function audioEl() {
    return $('audio');
}

function applyPreservePitch(a) {
    if ('preservesPitch' in a) a.preservesPitch = true;
    if ('webkitPreservesPitch' in a) a.webkitPreservesPitch = true;
    if ('mozPreservesPitch' in a) a.mozPreservesPitch = true;
}

// ── Wake Lock ──────────────────────────────────────────────────────────────
// Same shape as core's js/session.js: `wanted` is the source of truth across
// the async request so a lock granted after pause is released immediately.
const wake = { sentinel: null, wanted: false, pending: false };

async function acquireWake() {
    wake.wanted = true;
    if (wake.sentinel || wake.pending) return;
    if (!navigator.wakeLock || !navigator.wakeLock.request) {
        showWakeHintOnce();
        return;
    }
    wake.pending = true;
    try {
        const s = await navigator.wakeLock.request('screen');
        if (!wake.wanted) { try { await s.release(); } catch (_) { /* gone */ } return; }
        wake.sentinel = s;
        s.addEventListener('release', () => { wake.sentinel = null; });
    } catch (e) {
        console.debug('wakeLock request failed:', e && e.name);
    } finally {
        wake.pending = false;
    }
}

async function releaseWake() {
    wake.wanted = false;
    const s = wake.sentinel;
    wake.sentinel = null;
    if (s) { try { await s.release(); } catch (_) { /* gone */ } }
}

function showWakeHintOnce() {
    let shown = false;
    try { shown = localStorage.getItem(HINT_KEY) === '1'; } catch (_) { /* private mode */ }
    if (shown || !_session) return;
    try { localStorage.setItem(HINT_KEY, '1'); } catch (_) { /* private mode */ }
    setNotice('Tip: set Auto-Lock to Never while playing (Settings > Display & Brightness).', 8000);
}

// ── Notice line ────────────────────────────────────────────────────────────
let _noticeTimer = null;
function setNotice(text, ms) {
    const n = $('pl-notice');
    if (!n) return;
    clearTimeout(_noticeTimer);
    n.textContent = text || '';
    n.hidden = !text;
    if (text && ms) _noticeTimer = setTimeout(() => { n.hidden = true; }, ms);
}

// ── Highway connection ─────────────────────────────────────────────────────
function wsUrlFor(id, index) {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const q = Number.isInteger(index) && index >= 0 ? `?arrangement=${index}` : '';
    return `${proto}://${location.host}/ws/highway/${encodeURIComponent(id)}${q}`;
}

// The full mix keeps one <audio> element in sync with speed control; stems
// are a phase-4 spike. audio_url falls back to stems[0] on the server.
// Offline, the only acceptable audio is a blob: URL built from this song's own
// pack; any other URL in a stored header is ignored.
function pickAudioUrl(info, offline) {
    const url = info.full_mix_url || info.audio_url || null;
    if (offline && !(typeof url === 'string' && url.startsWith('blob:'))) return null;
    return url;
}

function onSongInfo(msg) {
    const s = _session;
    if (!s) return;
    s.songInfo = msg;
    const a = audioEl();
    const url = pickAudioUrl(msg, s.mode === 'offline');
    if (!url) {
        setNotice(msg.audio_error || 'This song has no playable audio.');
    } else if (url !== s.audioUrl) {
        s.audioUrl = url;
        a.src = url;
        a.defaultPlaybackRate = s.rate;
        a.playbackRate = s.rate;
        applyPreservePitch(a);
        a.load();
    }
    if (Number.isInteger(msg.arrangement_index)) s.arrIndex = msg.arrangement_index;
    if (s.mode === 'stream' && Array.isArray(msg.arrangements)) {
        s.arrangements = msg.arrangements.map((x) => ({
            index: x.index,
            name: x.smart_name || x.name || `Arrangement ${x.index + 1}`,
        }));
    }
    renderArrangements();
    $('pl-start').disabled = !url;
    $('pl-start-label').textContent = url ? 'Tap to start' : 'No audio';
}

function onHighwayError(err) {
    setNotice(`Error: ${err}`);
    $('pl-start-label').textContent = 'Could not load song';
}

async function offlineMessages(index) {
    const s = _session;
    const arr = s.record.arrangements.find((a) => a.index === index) || s.record.arrangements[0];
    if (!arr) throw new Error('This download has no arrangements');
    s.arrIndex = arr.index;
    for (const key of songInfoFbmKeys(arr.messages)) {
        if (s.objectUrls.has(key)) continue;
        const blob = await getBlob(s.record.id, key);
        if (blob) s.objectUrls.set(key, URL.createObjectURL(blob));
    }
    return rewriteSongInfoMessages(arr.messages, (key) => s.objectUrls.get(key) || null);
}

async function connect(index) {
    const s = _session;
    const h = _hw;
    const gen = ++s.connectGen;
    const opts = { secondary: true, onSongInfo, onError: onHighwayError };
    let messages = null;
    if (s.mode === 'offline') {
        messages = await offlineMessages(index);
        if (gen !== s.connectGen || _session !== s) return;
    }
    // Fresh mount per connection: stop() closes the previous socket and
    // releases the renderer; init() re-applies it to our canvas.
    h.stop();
    h.init($('pl-canvas'), $('pl-stage'));
    h.setContainer($('pl-stage'));
    watch3D();
    if (s.mode === 'stream') {
        h.connect(wsUrlFor(s.song.id, index), opts);
        return;
    }
    const Fake = createFakeWebSocketClass(messages, { batchSize: 50 });
    const RealWS = window.WebSocket;
    window.WebSocket = Fake;
    try {
        h.connect(wsUrlFor(s.record.id, s.arrIndex), opts);
    } finally {
        window.WebSocket = RealWS;
    }
}

// ── Renderer (3D Highway / 2D fallback) ─────────────────────────────────────
function fail3D(reason) {
    if (_h3dBroken) return;
    _h3dBroken = String(reason || 'unknown error');
    console.warn('3D Highway unavailable, using 2D:', _h3dBroken);
    _h3d = null;
    _rendererKind = '2d';
    // highway.js already reverts to its default renderer on an init failure;
    // this covers the paths where it has not (load failure, no WebGL2).
    if (_hw && !_hw.isDefaultRenderer()) _hw.setRenderer(null);
    setNotice('3D highway is unavailable on this device — showing the 2D highway.', 6000);
}

/** Why the 3D highway is not in use this session ('' when it is or can be). */
export function highwayFallbackReason() {
    return _h3dBroken;
}

// Install the wanted renderer on _hw before connect(). Before the first
// api.init() highway.js only stashes it; afterwards it is applied right away
// and re-applied by the init() inside connect().
async function chooseRenderer() {
    let want = getHighwayMode() === '3d' && !_h3dBroken;
    let factory = null;
    if (want && !canRun3D()) {
        fail3D('WebGL2 is not available');
        want = false;
    }
    if (want) {
        try {
            factory = await loadH3DFactory();
        } catch (e) {
            fail3D((e && e.message) || e);
            want = false;
        }
    }
    if (want && _rendererKind !== '3d') {
        try {
            _h3d = createH3DRenderer(factory);
        } catch (e) {
            fail3D((e && e.message) || e);
            return;
        }
        _rendererKind = '3d';
        _hw.setRenderer(_h3d);
    } else if (!want && _rendererKind !== '2d') {
        _h3d = null;
        _rendererKind = '2d';
        _hw.setRenderer(null);
    }
}

// The plugin initialises asynchronously (three.js import + scene build) and
// mints a fresh readyPromise per init(). highway.js reverts to 2D on a
// rejection from the current cycle; record why. 'superseded' is a newer
// init() or a stop(), not a failure.
function watch3D() {
    const r = _h3d;
    if (_rendererKind !== '3d' || !r) return;
    const rp = r.readyPromise;
    if (!rp || typeof rp.then !== 'function') return;
    rp.then(() => {}, (e) => {
        if (_h3d !== r || r.readyPromise !== rp) return;
        if (e && e.message === 'superseded') return;
        fail3D((e && e.message) || e);
    });
}

// ── Controls ───────────────────────────────────────────────────────────────
function renderArrangements() {
    const s = _session;
    const sel = $('pl-arr');
    sel.replaceChildren();
    for (const a of s.arrangements) {
        const o = el('option', null, a.name || `Arrangement ${a.index + 1}`);
        o.value = String(a.index);
        if (a.index === s.arrIndex) o.selected = true;
        sel.appendChild(o);
    }
    sel.disabled = s.arrangements.length < 2;
}

function setRate(pct) {
    const s = _session;
    const p = Math.max(40, Math.min(100, Math.round(pct)));
    s.rate = p / 100;
    const a = audioEl();
    a.defaultPlaybackRate = s.rate;
    a.playbackRate = s.rate;
    applyPreservePitch(a);
    $('pl-speed-range').value = String(p);
    $('pl-speed-btn').textContent = `${p}%`;
    $('pl-speed-val').textContent = `${p}%`;
    for (const b of $('pl-speed-presets').children) {
        b.classList.toggle('active', Number(b.dataset.pct) === p);
    }
}

function updatePlayButton() {
    const a = audioEl();
    $('pl-play').textContent = a.paused ? 'Play' : 'Pause';
}

function seekBy(dt) {
    const a = audioEl();
    const d = Number.isFinite(a.duration) ? a.duration : Infinity;
    a.currentTime = Math.max(0, Math.min(d, (a.currentTime || 0) + dt));
}

function setAudioSessionPlayback() {
    try {
        if (navigator.audioSession && navigator.audioSession.type !== 'playback') {
            navigator.audioSession.type = 'playback';
        }
    } catch (_) { /* unsupported */ }
}

function startFromGesture() {
    const a = audioEl();
    setAudioSessionPlayback();
    applyPreservePitch(a);
    const p = a.play();
    $('pl-start').hidden = true;
    if (p && typeof p.catch === 'function') {
        p.catch((e) => {
            console.warn('play() failed:', e);
            $('pl-start').hidden = false;
            $('pl-start-label').textContent = 'Tap to start';
            setNotice('Playback was blocked — tap to start again.', 5000);
        });
    }
}

function togglePlay() {
    const a = audioEl();
    if (a.paused) {
        setAudioSessionPlayback();
        a.play().catch(() => { $('pl-start').hidden = false; });
    } else {
        a.pause();
    }
}

function clockLoop() {
    const s = _session;
    if (!s) return;
    const a = audioEl();
    _hw.setTime(a.currentTime || 0);
    // highway.js swaps back to its 2D renderer on its own after an init
    // failure or repeated draw errors; surface that once.
    if (_rendererKind === '3d' && _hw.isDefaultRenderer()) fail3D('the 3D renderer stopped');
    const t = fmtTime(a.currentTime);
    const d = fmtTime(Number.isFinite(a.duration) ? a.duration : (s.duration || 0));
    const label = `${t} / ${d}`;
    if (label !== s.timeLabel) { s.timeLabel = label; $('pl-time').textContent = label; }
    s.raf = requestAnimationFrame(clockLoop);
}

// ── Wiring (once) ──────────────────────────────────────────────────────────
let _wired = false;
function wireOnce() {
    if (_wired) return;
    _wired = true;
    const a = audioEl();
    applyPreservePitch(a);

    for (const p of SPEED_PRESETS) {
        const b = el('button', 'chip', `${p}%`);
        b.type = 'button';
        b.dataset.pct = String(p);
        b.addEventListener('click', () => { if (_session) setRate(p); });
        $('pl-speed-presets').appendChild(b);
    }
    $('pl-speed-range').addEventListener('input', (e) => { if (_session) setRate(Number(e.target.value)); });
    $('pl-speed-btn').addEventListener('click', () => { $('pl-speed-panel').hidden = !$('pl-speed-panel').hidden; });
    $('pl-speed-close').addEventListener('click', () => { $('pl-speed-panel').hidden = true; });

    $('pl-close').addEventListener('click', () => closePlayer());
    $('pl-rotate-close').addEventListener('click', () => closePlayer());
    $('pl-play').addEventListener('click', () => { if (_session) togglePlay(); });
    $('pl-back').addEventListener('click', () => { if (_session) seekBy(-5); });
    $('pl-fwd').addEventListener('click', () => { if (_session) seekBy(5); });
    $('pl-start').addEventListener('click', () => { if (_session) startFromGesture(); });
    $('pl-arr').addEventListener('change', (e) => {
        if (!_session) return;
        const idx = Number(e.target.value);
        if (!Number.isInteger(idx) || idx === _session.arrIndex) return;
        // The <audio> element keeps playing the same track across the
        // switch (onSongInfo only reloads it when the URL changes), so the
        // current time is preserved without a seek.
        connect(idx).catch((err) => setNotice(String(err.message || err)));
    });
    // Tap the highway to show/hide the control bar.
    $('pl-stage').addEventListener('click', () => {
        if (!_session) return;
        $('pl-bar').classList.toggle('collapsed');
    });

    a.addEventListener('play', () => { updatePlayButton(); if (_session) acquireWake(); });
    a.addEventListener('pause', () => { updatePlayButton(); releaseWake(); });
    a.addEventListener('ended', () => { updatePlayButton(); releaseWake(); });
    a.addEventListener('error', () => {
        if (_session && a.getAttribute('src')) setNotice('Audio failed to load.');
    });
    document.addEventListener('visibilitychange', () => {
        if (document.visibilityState === 'visible' && _session && !a.paused) acquireWake();
    });
}

// ── Public API ─────────────────────────────────────────────────────────────

/**
 * Open the full-screen player.
 * @param {{mode: 'stream'|'offline', song: object}} args
 *   stream:  song is a /api/mobile/songs entry ({id, title, artist, ...})
 *   offline: song is a stored record or at least {id}
 */
export async function openPlayer({ mode, song }) {
    if (_session) closePlayer();
    wireOnce();
    if (!_hw) {
        if (typeof window.createHighway !== 'function') throw new Error('Highway renderer failed to load');
        _hw = window.createHighway();
    }
    // View settings may have changed in Settings since the last open.
    if (typeof _hw.setMinRenderScale === 'function') _hw.setMinRenderScale(minRenderScale());
    applyZoom();

    const s = {
        mode,
        song,
        record: null,
        arrangements: [],
        arrIndex: -1,
        audioUrl: null,
        objectUrls: new Map(),
        rate: 1,
        raf: 0,
        connectGen: 0,
        timeLabel: '',
        duration: Number(song.duration) || 0,
        songInfo: null,
    };
    _session = s;

    $('pl-title').textContent = song.title || song.id;
    $('pl-artist').textContent = song.artist || '';
    $('pl-start').hidden = false;
    $('pl-start').disabled = true;
    $('pl-start-label').textContent = 'Loading…';
    $('pl-speed-panel').hidden = true;
    $('pl-bar').classList.remove('collapsed');
    setNotice('');
    $('player').hidden = false;
    document.body.classList.add('playing');
    setRate(100);
    updatePlayButton();

    try {
        let initial = -1;
        if (mode === 'offline') {
            const rec = await getSong(song.id);
            if (_session !== s) return;
            if (!rec) throw new Error('This song is no longer downloaded.');
            s.record = rec;
            s.duration = Number(rec.duration) || s.duration;
            s.arrangements = rec.arrangements.map((x) => ({ index: x.index, name: x.name }));
            initial = rec.arrangements.length ? rec.arrangements[0].index : -1;
        }
        await chooseRenderer();
        if (_session !== s) return;
        await connect(initial);
        if (_session !== s) return;
        s.raf = requestAnimationFrame(clockLoop);
    } catch (e) {
        if (_session !== s) return;
        $('pl-start-label').textContent = 'Could not load song';
        setNotice(String((e && e.message) || e));
    }
}

export function closePlayer() {
    const s = _session;
    if (!s) return;
    _session = null;
    cancelAnimationFrame(s.raf);
    const a = audioEl();
    a.pause();
    a.removeAttribute('src');
    a.load();
    if (_hw) _hw.stop();
    for (const url of s.objectUrls.values()) URL.revokeObjectURL(url);
    s.objectUrls.clear();
    releaseWake();
    setNotice('');
    $('player').hidden = true;
    document.body.classList.remove('playing');
}

export function isPlayerOpen() {
    return !!_session;
}

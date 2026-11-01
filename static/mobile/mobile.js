// feedBack Mobile — app shell.
//
// Tabs: Library (online, from /api/mobile/songs), Downloaded (offline, from
// IndexedDB), Settings. All song metadata reaches the page through
// textContent / createElement only — never innerHTML.

import { downloadFbm, parseFbm } from './fbm.js';
import { saveSong, listSongs, deleteSong, deleteAll, estimate, ensureSpace } from './store.js';
import { openPlayer, isPlayerOpen, highwayFallbackReason } from './player.js';
import { getHighwayMode, getQuality, getZoom, setHighwayMode, setQuality, setZoom } from './h3d.js';

const PAGE = 200;
const PROBE_TIMEOUT_MS = 5000;

const $ = (id) => document.getElementById(id);

const state = {
    tab: 'library',
    online: null,          // null = unknown
    info: null,            // /api/mobile/info
    songs: null,           // server list, or null if never loaded
    songsError: '',
    shown: PAGE,
    downloaded: new Map(), // id -> record (no messages)
    jobs: new Map(),       // id -> {loaded, total, controller, error}
    rowRefs: new Map(),    // id -> {progress, button} for rendered library rows
};

// ── Helpers ──────────────────────────────────────────────────────────────
function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
}

function button(label, cls, onClick) {
    const b = el('button', `btn small ${cls || ''}`.trim(), label);
    b.type = 'button';
    b.addEventListener('click', (e) => { e.stopPropagation(); onClick(e); });
    return b;
}

function fmtBytes(n) {
    if (!Number.isFinite(n) || n < 0) return '?';
    if (n === 0) return '0 KB';
    if (n < 1024 * 1024) return `${Math.max(1, Math.round(n / 1024))} KB`;
    if (n < 1024 * 1024 * 1024) return `${(n / (1024 * 1024)).toFixed(1)} MB`;
    return `${(n / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}

function fmtDuration(s) {
    if (!Number.isFinite(s) || s <= 0) return '';
    const m = Math.floor(s / 60);
    return `${m}:${String(Math.floor(s % 60)).padStart(2, '0')}`;
}

function setStatus(id, text, isError) {
    const n = $(id);
    n.textContent = text || '';
    n.classList.toggle('error', !!isError);
}

function errMsg(e) {
    return (e && e.message) ? e.message : String(e);
}

function songUrl(id, suffix) {
    return `/api/mobile/songs/${encodeURIComponent(id)}/${suffix}`;
}

// ── Online / offline ─────────────────────────────────────────────────────
function setOnline(on) {
    const changed = state.online !== on;
    state.online = on;
    const n = $('net');
    n.classList.remove('online', 'offline', 'unknown');
    n.classList.add(on ? 'online' : 'offline');
    n.textContent = on ? 'Online' : 'Offline';
    if (changed) {
        renderLibrary();
        renderDownloads();
        renderSettings();
    }
}

let _probing = null;
async function probe() {
    if (_probing) return _probing;
    _probing = (async () => {
        if (typeof navigator.onLine === 'boolean' && !navigator.onLine) {
            setOnline(false);
            return false;
        }
        const ctl = new AbortController();
        const t = setTimeout(() => ctl.abort(), PROBE_TIMEOUT_MS);
        try {
            const res = await fetch('/api/mobile/info', { cache: 'no-store', signal: ctl.signal, credentials: 'same-origin' });
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            const info = await res.json();
            state.info = info && typeof info === 'object' ? info : null;
            setOnline(true);
            renderSettings();
            return true;
        } catch (_) {
            setOnline(false);
            return false;
        } finally {
            clearTimeout(t);
        }
    })();
    try { return await _probing; } finally { _probing = null; }
}

// ── Library ──────────────────────────────────────────────────────────────
async function loadLibrary() {
    if (!(await probe())) {
        renderLibrary();
        return;
    }
    setStatus('lib-status', 'Loading library…');
    try {
        const res = await fetch('/api/mobile/songs', { cache: 'no-store', credentials: 'same-origin' });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        state.songs = Array.isArray(data && data.songs) ? data.songs : [];
        state.songsError = '';
    } catch (e) {
        state.songsError = `Could not load the library: ${errMsg(e)}`;
    }
    renderLibrary();
    renderDownloads(); // update badges depend on server versions
}

function norm(s) {
    return String(s || '').toLocaleLowerCase();
}

function filteredSongs() {
    const q = norm($('lib-search').value).trim();
    const terms = q ? q.split(/\s+/) : [];
    const sortBy = $('lib-sort').value === 'title' ? 'title' : 'artist';
    const list = (state.songs || []).filter((s) => {
        if (!terms.length) return true;
        const hay = `${norm(s.title)} ${norm(s.artist)} ${norm(s.album)}`;
        return terms.every((t) => hay.includes(t));
    });
    const cmp = (a, b) => a.localeCompare(b, undefined, { sensitivity: 'base', numeric: true });
    list.sort((a, b) => {
        const primary = sortBy === 'title'
            ? cmp(a.title || a.id, b.title || b.id)
            : cmp(a.artist || '', b.artist || '') || cmp(a.title || a.id, b.title || b.id);
        return primary || cmp(a.id, b.id);
    });
    return list;
}

function libraryBadge(song) {
    const rec = state.downloaded.get(song.id);
    if (!rec) return null;
    if (song.version && rec.version !== song.version) return el('span', 'badge update', 'Update available');
    return el('span', 'badge', 'Downloaded');
}

function renderLibrary() {
    const list = $('lib-list');
    list.replaceChildren();
    state.rowRefs.clear();
    $('lib-more').hidden = true;

    if (state.online === false) {
        setStatus('lib-status', 'You are offline. Your downloaded songs still play — see the Downloaded tab.');
        return;
    }
    if (state.songsError) { setStatus('lib-status', state.songsError, true); return; }
    if (!state.songs) { setStatus('lib-status', state.online === null ? 'Connecting…' : 'Loading library…'); return; }

    const all = filteredSongs();
    const shown = all.slice(0, state.shown);
    setStatus('lib-status', all.length === state.songs.length
        ? `${all.length} songs`
        : `${all.length} of ${state.songs.length} songs`);

    const frag = document.createDocumentFragment();
    for (const song of shown) frag.appendChild(libraryRow(song));
    list.appendChild(frag);
    if (all.length > shown.length) {
        $('lib-more').hidden = false;
        $('lib-more').textContent = `Show more (${all.length - shown.length} left)`;
    }
}

function libraryRow(song) {
    const li = el('li', 'row');
    const main = el('div', 'row-main');
    const title = el('span', 'row-title', song.title || song.id);
    const badge = libraryBadge(song);
    if (badge) title.appendChild(badge);
    const subParts = [song.artist, song.album, fmtDuration(Number(song.duration))].filter(Boolean);
    if (Number.isFinite(song.size_bytes)) subParts.push(`${song.size_is_estimate ? '~' : ''}${fmtBytes(song.size_bytes)}`);
    const sub = el('span', 'row-sub', subParts.join(' · '));
    main.append(title, sub);

    const progress = el('progress');
    progress.max = 1;
    progress.hidden = true;
    main.appendChild(progress);

    const actions = el('div', 'row-actions');
    actions.appendChild(button('Stream', '', () => openPlayer({ mode: 'stream', song })));
    const dl = button('Download', 'primary', () => onDownloadClick(song));
    actions.appendChild(dl);
    li.append(main, actions);

    state.rowRefs.set(song.id, { progress, button: dl, song });
    paintJob(song.id);
    return li;
}

function paintJob(id) {
    const refs = state.rowRefs.get(id);
    if (!refs) return;
    const job = state.jobs.get(id);
    const rec = state.downloaded.get(id);
    const { progress, button: b, song } = refs;
    if (job) {
        progress.hidden = false;
        if (job.total && job.phase !== 'preparing') { progress.max = job.total; progress.value = job.loaded; } else { progress.removeAttribute('value'); }
        // Tapping while preparing/downloading cancels.
        b.textContent = job.phase === 'saving' ? 'Saving…' : (job.phase === 'preparing' ? 'Preparing…' : 'Cancel');
        b.disabled = job.phase === 'saving';
        b.classList.remove('primary');
        return;
    }
    progress.hidden = true;
    b.disabled = false;
    if (rec && (!song.version || rec.version === song.version)) {
        b.textContent = 'Re-download';
        b.classList.remove('primary');
    } else {
        b.textContent = rec ? 'Update' : 'Download';
        b.classList.add('primary');
    }
}

function onDownloadClick(song) {
    const job = state.jobs.get(song.id);
    if (job) {
        if (job.phase !== 'saving') job.controller.abort();
        return;
    }
    startDownload(song);
}

async function startDownload(song) {
    const controller = new AbortController();
    const job = { loaded: 0, total: null, controller, phase: 'preparing' };
    state.jobs.set(song.id, job);
    paintJob(song.id);
    const statusId = state.tab === 'downloads' ? 'dl-status' : 'lib-status';
    const name = song.title || song.id;
    // The server may build the pack before the response starts.
    setStatus(statusId, `Preparing ${name}…`);
    let result = ['', false];
    try {
        if (Number.isFinite(song.size_bytes)) await ensureSpace(song.size_bytes);
        const buf = await downloadFbm(songUrl(song.id, 'download'), {
            signal: controller.signal,
            onProgress: ({ loaded, total }) => {
                if (job.phase === 'preparing') {
                    job.phase = 'download';
                    if (statusId === 'lib-status') setStatus(statusId, `Downloading ${name}…`);
                }
                job.loaded = loaded;
                job.total = total;
                paintJob(song.id);
                if (statusId === 'dl-status') {
                    setStatus(statusId, total
                        ? `Downloading ${name}… ${Math.floor((loaded / total) * 100)}%`
                        : `Downloading ${name}… ${fmtBytes(loaded)}`);
                }
            },
        });
        job.phase = 'saving';
        paintJob(song.id);
        const parsed = parseFbm(buf);
        if (parsed.header.id !== song.id) throw new Error('The server sent a different song than requested');
        await saveSong(parsed.header, buf);
        result = [`Downloaded ${name}.`, false];
    } catch (e) {
        result = (e && e.name === 'AbortError')
            ? ['Download cancelled.', false]
            : [`${name}: ${errMsg(e)}`, true];
    } finally {
        state.jobs.delete(song.id);
        await refreshDownloads();
        paintJob(song.id);
    }
    // After the re-render, which rewrites the list status lines.
    setStatus(statusId, result[0], result[1]);
}

// ── Downloaded ───────────────────────────────────────────────────────────
async function refreshDownloads() {
    try {
        const list = await listSongs();
        state.downloaded = new Map(list.map((r) => [r.id, r]));
        setStatus('dl-status', '');
    } catch (e) {
        setStatus('dl-status', `Could not read downloads: ${errMsg(e)}`, true);
    }
    renderDownloads();
    // Refresh library badges/buttons in place without losing scroll.
    if (state.tab === 'library') renderLibrary();
    renderStorage();
}

function serverSong(id) {
    if (!state.songs) return null;
    return state.songs.find((x) => x.id === id) || null;
}

function renderDownloads() {
    const list = $('dl-list');
    list.replaceChildren();
    const recs = [...state.downloaded.values()].sort((a, b) =>
        (a.artist || '').localeCompare(b.artist || '', undefined, { sensitivity: 'base' })
        || (a.title || a.id).localeCompare(b.title || b.id, undefined, { sensitivity: 'base' }));
    $('dl-delete-all').hidden = recs.length === 0;
    if (!recs.length) {
        if (!$('dl-status').classList.contains('error')) {
            setStatus('dl-status', 'No downloads yet. Download songs from the Library tab while at home.');
        }
        return;
    }
    if (!$('dl-status').classList.contains('error')) setStatus('dl-status', `${recs.length} downloaded`);
    const frag = document.createDocumentFragment();
    for (const rec of recs) frag.appendChild(downloadRow(rec));
    list.appendChild(frag);
}

function downloadRow(rec) {
    const li = el('li', 'row');
    const main = el('div', 'row-main');
    const title = el('span', 'row-title', rec.title || rec.id);
    const server = serverSong(rec.id);
    const outdated = !!(server && server.version && server.version !== rec.version);
    if (outdated) title.appendChild(el('span', 'badge update', 'Update available'));
    const arrs = (rec.arrangements || []).length;
    const sub = [rec.artist, fmtBytes(rec.size_bytes), `${arrs} part${arrs === 1 ? '' : 's'}`].filter(Boolean).join(' · ');
    main.append(title, el('span', 'row-sub', sub));

    const actions = el('div', 'row-actions');
    if (outdated && state.online) {
        actions.appendChild(button('Update', '', () => {
            if (state.jobs.has(rec.id)) return;
            startDownload(server);
        }));
    }
    actions.appendChild(button('Play', 'primary', () => openPlayer({ mode: 'offline', song: rec })));
    actions.appendChild(button('Delete', 'danger', async () => {
        if (!confirm(`Delete "${rec.title || rec.id}" from this phone?`)) return;
        try { await deleteSong(rec.id); } catch (e) { setStatus('dl-status', errMsg(e), true); }
        await refreshDownloads();
    }));
    li.append(main, actions);
    return li;
}

async function renderStorage() {
    const { usage, quota, persisted } = await estimate();
    const ours = [...state.downloaded.values()].reduce((n, r) => n + (r.size_bytes || 0), 0);
    let text = `Downloads: ${fmtBytes(ours)}`;
    if (usage !== null && quota !== null) {
        text += ` · ${fmtBytes(Math.max(0, quota - usage))} free`;
        $('st-meter').max = quota || 1;
        $('st-meter').value = usage;
        $('st-meter').hidden = false;
    } else {
        $('st-meter').hidden = true;
    }
    $('st-text').textContent = text;
    const pill = $('st-persist');
    pill.classList.remove('ok', 'warn');
    if (persisted === true) { pill.textContent = 'Persistent'; pill.classList.add('ok'); }
    else if (persisted === false) { pill.textContent = 'Not persistent'; pill.classList.add('warn'); }
    else { pill.textContent = ''; }
}

// ── Settings ─────────────────────────────────────────────────────────────
function renderSettings() {
    const i = state.info;
    const on = state.online;
    $('set-origin').textContent = (i && i.origin) || location.origin;
    $('set-user').textContent = on && i ? (i.user || 'unknown') : (on === false ? 'offline' : '—');
    $('set-version').textContent = on && i ? (i.version || '?') : (on === false ? 'offline' : '—');
    renderHighwaySetting();
}

function renderHighwaySetting() {
    const mode = getHighwayMode();
    $('set-highway').value = mode;
    $('set-zoom').value = getZoom();
    $('set-quality').value = getQuality();
    const why = highwayFallbackReason();
    $('set-highway-note').textContent = mode === '3d' && why ? `3D is unavailable (${why}); the 2D highway is used instead.` : '';
}

// ── Service worker + update prompt ───────────────────────────────────────
let _swReg = null;

function showUpdateBanner(worker) {
    const banner = $('update-banner');
    banner.hidden = false;
    $('update-reload').onclick = () => {
        $('update-reload').disabled = true;
        worker.postMessage('SKIP_WAITING');
    };
}

function trackInstalling(reg, worker) {
    if (!worker) return;
    worker.addEventListener('statechange', () => {
        if (worker.state === 'installed' && navigator.serviceWorker.controller) showUpdateBanner(worker);
        if (worker.state === 'activated') $('set-sw').textContent = 'Ready for offline use';
    });
}

async function registerServiceWorker() {
    if (!('serviceWorker' in navigator)) {
        $('set-sw').textContent = 'Not supported — offline launch unavailable';
        return;
    }
    let reloading = false;
    navigator.serviceWorker.addEventListener('controllerchange', () => {
        if (reloading || !_swWasControlled) return;
        reloading = true;
        location.reload();
    });
    try {
        const reg = await navigator.serviceWorker.register('/mobile/sw.js', { scope: '/mobile/' });
        _swReg = reg;
        $('set-sw').textContent = reg.active ? 'Ready for offline use' : 'Installing…';
        if (reg.waiting && navigator.serviceWorker.controller) showUpdateBanner(reg.waiting);
        trackInstalling(reg, reg.installing);
        reg.addEventListener('updatefound', () => trackInstalling(reg, reg.installing));
    } catch (e) {
        $('set-sw').textContent = `Failed: ${errMsg(e)}`;
    }
}
// The first install claims the page (clients.claim) and fires
// controllerchange; only reload for a real update from an older worker.
const _swWasControlled = !!(navigator.serviceWorker && navigator.serviceWorker.controller);

async function checkForUpdate() {
    if (!_swReg) { setStatus('set-status', 'No service worker registered.'); return; }
    setStatus('set-status', 'Checking…');
    try {
        await _swReg.update();
        setStatus('set-status', _swReg.waiting || _swReg.installing ? 'Update found.' : 'You have the latest version.');
    } catch (e) {
        setStatus('set-status', `Update check failed: ${errMsg(e)}`, true);
    }
}

// ── Tabs ─────────────────────────────────────────────────────────────────
const TABS = { library: 'view-library', downloads: 'view-downloads', settings: 'view-settings' };

function showTab(name) {
    state.tab = name;
    for (const [k, view] of Object.entries(TABS)) {
        const active = k === name;
        $(view).hidden = !active;
        const tab = $(`tab-${k}`);
        tab.classList.toggle('active', active);
        tab.setAttribute('aria-selected', String(active));
    }
    if (name === 'library') {
        if (!state.songs && state.online !== false) loadLibrary();
        else renderLibrary();
    } else if (name === 'downloads') {
        refreshDownloads();
    } else if (name === 'settings') {
        renderSettings();
        probe();
    }
}

// ── Boot ─────────────────────────────────────────────────────────────────
function wire() {
    for (const k of Object.keys(TABS)) $(`tab-${k}`).addEventListener('click', () => showTab(k));
    let searchTimer = null;
    $('lib-search').addEventListener('input', () => {
        clearTimeout(searchTimer);
        searchTimer = setTimeout(() => { state.shown = PAGE; renderLibrary(); }, 150);
    });
    $('lib-sort').addEventListener('change', () => { state.shown = PAGE; renderLibrary(); });
    $('lib-refresh').addEventListener('click', () => { state.songsError = ''; loadLibrary(); });
    $('lib-more').addEventListener('click', () => { state.shown += PAGE; renderLibrary(); });
    $('dl-delete-all').addEventListener('click', async () => {
        if (!confirm('Delete ALL downloaded songs from this phone?')) return;
        try { await deleteAll(); } catch (e) { setStatus('dl-status', errMsg(e), true); }
        await refreshDownloads();
    });
    $('set-check-update').addEventListener('click', checkForUpdate);
    $('set-highway').addEventListener('change', (e) => {
        setHighwayMode(e.target.value);
        renderHighwaySetting();
    });
    $('set-zoom').addEventListener('change', (e) => setZoom(e.target.value));
    $('set-quality').addEventListener('change', (e) => setQuality(e.target.value));

    window.addEventListener('online', () => { probe().then((on) => { if (on && !state.songs) loadLibrary(); }); });
    window.addEventListener('offline', () => setOnline(false));
    document.addEventListener('visibilitychange', () => {
        if (document.visibilityState === 'visible' && !isPlayerOpen()) probe();
    });
}

async function boot() {
    wire();
    registerServiceWorker();
    await refreshDownloads();
    const on = await probe();
    if (on) {
        showTab('library');
    } else {
        showTab(state.downloaded.size ? 'downloads' : 'library');
    }
}

boot();

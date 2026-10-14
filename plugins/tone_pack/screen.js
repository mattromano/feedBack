// Tone pack (feedBack 0.3 port of the Slopsmith 0.2.9 user plugin): adds ready-made presets for every Tone Automation category to the Audio plugin
// (Clean, OD, Dist, Solo, Bass, Acoustic, Mod) and points the empty category targets at them.
// The Audio plugin is feedBack-desktop's renderer (Chain panel); 0.3 keeps its storage keys and
// window._aeToneAutomation / _toneSwitcher / _aeReplaceChainWithPresetBlob hooks. In a browser
// without the desktop engine none of that exists, and this plugin does nothing.
// routes.py builds the presets from the captures / IRs on this computer.
//
// Rules: presets the user made are never touched. Presets this plugin made
// (`generatedBy: 'tone_pack'`) are refreshed when the pack changes, unless the user deleted one
// (remembered in `tone-pack-removed`) — then it stays deleted. Targets the user already set stay.
(function () {
    'use strict';
    const PRESETS_KEY = 'slopsmith-chain-presets';
    const TA_KEY = 'slopsmith-tone-automation';
    const SEEN_KEY = 'tone-pack-seen';        // names this pack has installed before
    const STATE_KEY = 'tone-pack-state';      // {version, kilohearts}
    const TRIM_KEY = 'tone-pack-trims';       // {category: dB} volume trims from the settings page
    const HOME_KEY = 'tone-pack-home';        // preset to go back to after a song ('' = off)
    const METAL_KEY = 'tone-pack-metal-artists';  // newline list; songs by these use the metal preset
    const MIGRATE_KEY = 'tone-pack-targets-v';
    const TRIM_SEED_KEY = 'tone-pack-trims-seed';  // SEED_TRIMS applied once
    const SEED_TRIMS = { clean: 4, acoustic: 4 };   // dB

    // The user's own presets for the categories (asked for 2026-10-09): crunch and distortion play
    // Main Lead, leads play Metal Tone; songs by metal / MCR-style artists play Metal Tone for all
    // their crunch / distortion. Applied once (MIGRATE_KEY); later target changes are the user's.
    const PREFERRED_TARGETS = { od: 'Main Lead', dist: 'Main Lead', solo: 'Metal Tone' };
    // Clean and Acoustic had been left on Main Lead, so clean / acoustic songs (Royel Otis "moody")
    // played the crunch lead tone (found 2026-10-12): point them at this pack's Clean / Acoustic
    // presets, once (MIGRATE_KEY 3).
    const PACK_TARGETS_V3 = ['clean', 'acoustic'];
    const METAL_PRESET = 'Metal Tone';
    const DEFAULT_METAL_ARTISTS = [
        'My Chemical Romance', 'Metallica', 'Megadeth', 'Slayer', 'Anthrax', 'Testament', 'Annihilator',
        'Pantera', 'Sepultura', 'Death', 'Morbid Angel', 'Darkthrone', 'Opeth', 'Mastodon', 'Lamb of God',
        'Machine Head', 'Killswitch Engage', 'All That Remains', 'Trivium', 'Bullet for My Valentine',
        'Avenged Sevenfold', 'Hail To The King', 'Children of Bodom', 'Arch Enemy', 'Amon Amarth', 'Amaranthe',
        'DragonForce', 'Dream Theater', 'Between the Buried and Me', 'Bring Me the Horizon', 'Knocked Loose',
        'Deafheaven', 'Dethklok', 'Five Finger Death Punch', 'Disturbed', 'Godsmack', 'System of a Down',
        'Black Label Society', 'Iron Maiden', 'Judas Priest', 'Black Sabbath', 'Black Sabath', 'Ozzy Osbourne',
        'ozzy', 'Dio', 'Motorhead', 'Sabaton', 'Nightwish', 'BABYMETAL', 'Ghost', 'Ghost B.C.', 'Volbeat',
        'Type O Negative', 'White Zombie', 'Rob Zombie', 'Marilyn Manson', 'Tool', 'Deftones', 'Sevendust',
        'Drowning Pool', 'Black Veil Brides', 'Escape The Fate', 'The Used', 'Accept', 'Queensryche', 'Helmet',
        'Red Fang', 'Spinal Tap', 'Steel Panther', 'Limp Bizkit', 'P.O.D.', 'Papa Roach', 'Linkin Park',
        'Powerman 5000', 'Slipknot', 'Korn', 'Evanescence', 'Gojira',
    ];

    const _norm = (a) => String(a || '').toLowerCase().normalize('NFD').replace(/[\u0300-\u036f]/g, '')
        .replace(/[^a-z0-9]+/g, ' ').trim();

    function metalArtists() {
        let raw = null;
        try { raw = localStorage.getItem(METAL_KEY); } catch (_) { /* ignore */ }
        const list = raw === null ? DEFAULT_METAL_ARTISTS : raw.split(/\n+/);
        return new Set(list.map(_norm).filter(Boolean));
    }

    // "Lil Uzi Vert, BABYMETAL" / "Slash featuring ..." count when any credited artist is on the list.
    function isMetalArtist(artist, set) {
        const n = _norm(artist);
        if (!n) return false;
        if (set.has(n)) return true;
        return String(artist).split(/,|&| feat\.? | featuring | and | with /i).some(a => set.has(_norm(a)));
    }

    function readJson(key, fallback) {
        try {
            const v = JSON.parse(localStorage.getItem(key) || 'null');
            return v && typeof v === 'object' ? v : fallback;
        } catch (_) { return fallback; }
    }

    function writeJson(key, value) {
        try { localStorage.setItem(key, JSON.stringify(value)); return true; } catch (_) { return false; }
    }

    // Pure merge (tested): returns {presets, ta, seen, added, updated}.
    // Pack preset with the user's category trim folded into its output gain (also recorded in
    // packLevels, so a trim isn't mistaken for a manual tweak on the next refresh).
    function withTrim(p, trims) {
        const db = Number((trims || {})[p.category]);
        if (!Number.isFinite(db) || db === 0) return p;
        const g = Math.round(Math.pow(10, db / 20) * 10000) / 10000;
        return Object.assign({}, p, { outputGain: g, trimDb: db,
            packLevels: Object.assign({}, p.packLevels || {}, { outputGain: g }) });
    }

    function mergePack(pack, presets, ta, seen, trims, opts) {
        presets = Object.assign({}, presets || {});
        ta = Object.assign({ enabled: false, customKeywords: {}, targets: {} }, ta || {});
        ta.targets = Object.assign({}, ta.targets || {});
        seen = Array.isArray(seen) ? seen.slice() : [];
        const added = [], updated = [];
        for (const [name, p0] of Object.entries(pack.presets || {})) {
            const p = withTrim(p0, trims);
            const cur = presets[name];
            if (!cur) {
                if (seen.includes(name)) continue;           // the user deleted it: leave it gone
                presets[name] = Object.assign({}, p, { created: Date.now() });
                added.push(name);
            } else if (cur.generatedBy === 'tone_pack' && (cur.nativePreset !== p.nativePreset
                       || (cur.trimDb || 0) !== (p.trimDb || 0))) {
                // ours and the pack changed (new gear, level fix, Kilohearts installed): refresh.
                // Keep level / gate settings only where the user changed them from what the pack
                // set (packLevels); older pack presets have no record, so they take the new values.
                const was = cur.packLevels || null;
                const tweaked = (k) => !!was && JSON.stringify(cur[k]) !== JSON.stringify(was[k]);
                const next = Object.assign({}, p, { created: cur.created || Date.now() });
                for (const k of ['inputGain', 'outputGain', 'noiseGate', 'tonePolish']) {
                    if (tweaked(k)) next[k] = cur[k];
                }
                presets[name] = next;
                updated.push(name);
            }
            if (!seen.includes(name)) seen.push(name);
        }
        // Retarget categories that are empty or still point at a pack preset that no longer
        // exists (e.g. the Mod preset renamed when Kilohearts got installed).
        for (const [cat, name] of Object.entries(pack.targets || {})) {
            const cur = ta.targets[cat];
            const stale = cur && !presets[cur] && seen.includes(cur);
            if ((!cur || stale) && presets[name]) ta.targets[cat] = name;
        }
        // The user's preferred presets per category (once).
        if (opts && opts.preferred) {
            for (const [cat, name] of Object.entries(opts.preferred)) {
                if (presets[name]) ta.targets[cat] = name;
            }
        }
        if (opts && opts.packCats) {
            for (const cat of opts.packCats) {
                const name = (pack.targets || {})[cat];
                if (name && presets[name]) ta.targets[cat] = name;
            }
        }
        // Idle fallback: the user's own default lead if there is one, else Crunch.
        if (!ta.targets.idle) {
            const own = Object.keys(presets).find(n => presets[n].generatedBy !== 'tone_pack');
            ta.targets.idle = own || pack.targets.od || null;
            if (!ta.targets.idle) delete ta.targets.idle;
        }
        return { presets, ta, seen, added, updated };
    }

    async function install() {
        let pack;
        try {
            const r = await fetch('/api/plugins/tone_pack/presets');
            if (!r.ok) return;
            pack = await r.json();
        } catch (_) { return; }
        const firstRun = localStorage.getItem(TA_KEY) === null;
        let migrated = 0;
        try { migrated = Number(localStorage.getItem(MIGRATE_KEY)) || 0; } catch (_) { /* ignore */ }
        const trims = readJson(TRIM_KEY, {});
        let trimSeed = 0;
        try { trimSeed = Number(localStorage.getItem(TRIM_SEED_KEY)) || 0; } catch (_) { /* ignore */ }
        if (trimSeed < 1) {
            // Clean / Acoustic louder (asked for 2026-10-10), once; Settings → Tone Pack changes stay.
            for (const [cat, db] of Object.entries(SEED_TRIMS)) {
                if (!Number(trims[cat])) trims[cat] = db;
            }
            writeJson(TRIM_KEY, trims);
            try { localStorage.setItem(TRIM_SEED_KEY, '1'); } catch (_) { /* ignore */ }
        }
        const res = mergePack(pack, readJson(PRESETS_KEY, {}), readJson(TA_KEY, null), readJson(SEEN_KEY, []),
            trims, { preferred: migrated < 2 ? PREFERRED_TARGETS : null, packCats: migrated < 3 ? PACK_TARGETS_V3 : null });
        try { localStorage.setItem(MIGRATE_KEY, '3'); } catch (_) { /* ignore */ }
        if (firstRun) res.ta.enabled = true;            // never configured: turn Tone Automation on
        writeJson(PRESETS_KEY, res.presets);
        writeJson(TA_KEY, res.ta);
        writeJson(SEEN_KEY, res.seen);
        writeJson(STATE_KEY, { version: pack.version, kilohearts: !!pack.kilohearts, missing: pack.missing || [] });
        if (res.added.length || res.updated.length || migrated < 3) {
            console.log('[tone-pack] added', res.added, 'updated', res.updated);
            const ta = window._aeToneAutomation;
            try { if (ta && ta.renderSettings) ta.renderSettings(); } catch (_) { /* audio plugin UI not ready */ }
            try { if (ta && ta.renderTargets) ta.renderTargets(); } catch (_) { /* ignore */ }
        }
    }

    // ── Per-song categories from the song's gear ───────────────────────────
    // Tone Automation classifies tone *names*. Names it can't place ("Tone 1", "Default",
    // "George_Rhythm"...) fall back to Idle, every tone of a Bass arrangement whose name says
    // "dist" got the guitar Dist preset, and single-tone arrangements classify the song's file
    // name. For the loaded sloppak, routes.py classifies each tone from its amp / pedals; those
    // become session overrides (the Audio plugin's own per-song mechanism, cleared on each song).

    // Pure (tested): {toneName: presetName} overrides for one song.
    function songOverrides(gearCats, classify, targets, opts) {
        opts = opts || {};
        const out = {};
        for (const [name, cat] of Object.entries(gearCats || {})) {
            const key = name === '$song' ? opts.songKey : name;
            if (!key) continue;
            const byName = name === '$song' ? null : classify(name);
            // metal song: every crunch / distortion tone plays the metal preset
            const eff = opts.bass ? 'bass' : (byName || cat);
            if (opts.metalPreset && (eff === 'dist' || eff === 'od')) {
                if (targets[eff] !== opts.metalPreset) out[key] = opts.metalPreset;
                continue;
            }
            if (!cat) continue;
            // keep what the name says, except on a bass part, where everything is Bass
            if (byName && !(opts.bass && byName !== 'bass')) continue;
            const preset = targets[cat];
            if (preset && preset !== targets[byName || 'idle']) out[key] = preset;
        }
        return out;
    }

    let _songSeq = 0;
    async function applySongOverrides() {
        const ta = window._aeToneAutomation;
        if (!ta || !ta.isEnabled || !ta.isEnabled()) return;
        const hw = window.highway;
        const si = (hw && hw.getSongInfo && hw.getSongInfo()) || {};
        const host = window.feedBack || window.slopsmith;
        const cs = (host && host.currentSong) || {};
        const filename = window._currentSongFile || si.filename || cs.filename || '';
        let arrangement = si.arrangement || '';
        if (!arrangement && Array.isArray(si.arrangements)) {
            const a = si.arrangements.find(x => x && x.index === si.arrangement_index);
            if (a) arrangement = a.name || '';
        }
        if (!arrangement) return;
        const seq = ++_songSeq;
        let cats = {};
        if (/\.sloppak\/?$/i.test(filename)) {
            try {
                const r = await fetch('/api/plugins/tone_pack/song_tones?filename=' + encodeURIComponent(filename)
                    + '&arrangement=' + encodeURIComponent(arrangement));
                if (r.ok) cats = (await r.json()).tones || {};
            } catch (_) { /* name-only below */ }
        }
        if (seq !== _songSeq) return;
        // PSARCs / no gear data: the names the highway has (classified by name only)
        try {
            const names = [hw && hw.getToneBase && hw.getToneBase()]
                .concat(((hw && hw.getToneChanges && hw.getToneChanges()) || []).map(c => c && c.name));
            for (const n of names) if (n && !(n in cats)) cats[n] = null;
        } catch (_) { /* ignore */ }
        if (!Object.keys(cats).length) cats.$song = null;
        const artist = si.artist || cs.artist || '';
        const presetsNow = readJson(PRESETS_KEY, {});
        const metal = isMetalArtist(artist, metalArtists()) && presetsNow[METAL_PRESET] ? METAL_PRESET : null;
        const cfg = ta.getConfig ? ta.getConfig() : {};
        const ov = songOverrides(cats, (n) => ta.classify(n, cfg), cfg.targets || {},
            { songKey: window._currentSongFile || filename, bass: /\bbass\b/i.test(arrangement),
                metalPreset: metal });
        if (!Object.keys(ov).length) return;
        const cur = window._aeTaSessionOverrides || {};
        for (const [k, v] of Object.entries(ov)) {
            if (!Object.prototype.hasOwnProperty.call(cur, k)) cur[k] = v;   // a manual pick wins
        }
        window._aeTaSessionOverrides = cur;
        console.log('[tone-pack] gear-based tones for this song:', ov);
        // re-apply the tone that's playing now (the switcher skips a preset that's already loaded)
        const sw = window._toneSwitcher;
        if (sw && sw.taSwitcher && sw.activeTone) {
            try { await sw.switchToTone(sw.activeTone); } catch (_) { /* ignore */ }
        }
    }

    // ── Back to Main Lead after a song ─────────────────────────────────────
    // Tone Automation leaves the last song tone loaded. When a song ends or the player is left,
    // load the home preset (setting; default Main Lead, else the Audio plugin's Default, else the
    // Idle target). Skipped while the next song is starting (the Audio plugin's transition flag).
    function homePresetName(presets, ta) {
        let pick = null;
        try { pick = localStorage.getItem(HOME_KEY); } catch (_) { /* storage blocked */ }
        if (pick === '') return null;                                   // turned off
        if (pick && presets[pick]) return pick;
        if (presets['Main Lead']) return 'Main Lead';
        let def = null;
        try { def = localStorage.getItem('slopsmith-default-preset-name'); } catch (_) { /* ignore */ }
        if (def && presets[def]) return def;
        const idle = ta && ta.targets && ta.targets.idle;
        return idle && presets[idle] ? idle : null;
    }

    let _homeTimer = null;
    function goHome(reason) {
        clearTimeout(_homeTimer);
        _homeTimer = setTimeout(async () => {
            if (Date.now() < (window._aeSongTransitionUntil || 0)) return;   // next song loading
            const host = window.feedBack || window.slopsmith;
            if (host && host.isPlaying) return;      // replayed already
            const presets = readJson(PRESETS_KEY, {});
            const name = homePresetName(presets, readJson(TA_KEY, {}));
            if (!name || typeof window._aeReplaceChainWithPresetBlob !== 'function') return;
            const ok = await window._aeReplaceChainWithPresetBlob(presets[name], 'tone-pack:home', { snapshot: false });
            if (!ok) return;
            const sw = window._toneSwitcher;
            if (sw && sw.taSwitcher) sw.activePreset = name;   // a replay's first tone change loads again
            console.log('[tone-pack] back to', name, '(' + reason + ')');
        }, 600);
    }

    if (typeof window !== 'undefined') window._tonePackReinstall = install;   // settings page re-merge
    if (typeof window !== 'undefined') window._tonePackDefaultMetal = DEFAULT_METAL_ARTISTS.slice();

    if (typeof module !== 'undefined' && module.exports) {
        module.exports = { mergePack, songOverrides, withTrim, homePresetName, isMetalArtist, _norm };
        return;
    }
    const bus = window.feedBack || window.slopsmith;
    try {
        if (bus && typeof bus.on === 'function') {
            bus.on('song:ended', () => goHome('song ended'));
            bus.on('song:stop', () => goHome('song stopped'));
            // song:ready, then once more after the Audio plugin's own (debounced) tone setup
            bus.on('song:ready', () => {
                applySongOverrides();
                setTimeout(applySongOverrides, 2500);
            });
        }
    } catch (_) { /* no host */ }
    // Run once the Audio plugin has had a chance to start (its UI reads localStorage on render).
    // Only in the desktop app: the presets are native-engine chains.
    const desktop = window.feedBackDesktop || window.slopsmithDesktop;
    if (desktop && desktop.audio) setTimeout(install, 1500);
})();

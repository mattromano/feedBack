// Guitar Volume (feedBack 0.3 port of the Slopsmith 0.2.9 user plugin).
//
// A master level for the guitar you're playing. The desktop engine's 'chain'
// gain is the guitar-only amp output, and every preset sets it (Audio Engine
// presets, Tone Automation switches, the Output slider, Rig Builder's
// leveller), so 0.3's "Desktop Chain" mixer fader is overwritten on the next
// tone change. Instead, every setGain(kind, value) in those plugins is routed
// through window.__gvGain(kind, value): for 'chain' it remembers the preset's
// level and returns level x master; other kinds pass through. Moving the
// "Guitar" mixer fader re-applies the current preset's level at the new master.
//
// The bridge (window.feedBackDesktop.audio) is a frozen contextBridge object,
// so the callers' source is rewritten as it loads instead: this plugin sorts
// first ("_Guitar Volume") and sees the loader's <script data-plugin-id> tags.
// A plugin whose calls no longer match simply loads stock.
(function () {
    'use strict';
    const KEY = 'guitarVolumeDb';
    const MIN = -24, MAX = 12;
    const TARGETS = ['audio_engine', 'rig_builder'];
    const GAIN = '(window.__gvGain || ((k, v) => v))';

    // Rewrite every `<expr>.setGain(K, X)` call to `<expr>.setGain(K, __gvGain(K, X))`.
    // Arguments are split at the first top-level comma (strings and nested
    // calls respected); returns { code, count }.
    function rewriteSetGain(code) {
        const NEEDLE = '.setGain(';
        let out = '', i = 0, count = 0;
        for (;;) {
            const at = code.indexOf(NEEDLE, i);
            if (at === -1) break;
            const start = at + NEEDLE.length;
            let depth = 1, j = start, comma = -1, extra = false, quote = null;
            for (; j < code.length && depth > 0; j++) {
                const c = code[j];
                if (quote) { if (c === '\\') j++; else if (c === quote) quote = null; continue; }
                if (c === "'" || c === '"' || c === '`') quote = c;
                else if (c === '(' || c === '[' || c === '{') depth++;
                else if (c === ')' || c === ']' || c === '}') depth--;
                else if (c === ',' && depth === 1) { if (comma === -1) comma = j; else extra = true; }
            }
            const end = j - 1;   // the call's closing paren
            if (depth !== 0 || comma === -1 || extra) { out += code.slice(i, start); i = start; continue; }
            const k = code.slice(start, comma).trim();
            const v = code.slice(comma + 1, end).trim();
            if (!k || !v) { out += code.slice(i, start); i = start; continue; }
            out += code.slice(i, start) + k + ', ' + GAIN + '(' + k + ', ' + v + '))';
            i = end + 1;
            count++;
        }
        return { code: out + code.slice(i), count };
    }

    const clampDb = (db) => Math.max(MIN, Math.min(MAX, Math.round(Number(db) || 0)));
    const lin = (db) => Math.pow(10, db / 20);

    // State machine for the master level (pure apart from the injected setGain).
    function createMaster(db, setChain) {
        const m = {
            db: clampDb(db),
            lastRaw: null,
            gain(kind, value) {
                if (kind !== 'chain' || !Number.isFinite(Number(value))) return value;
                m.lastRaw = Number(value);
                return m.lastRaw * lin(m.db);
            },
            setDb(next) {
                m.db = clampDb(next);
                if (m.lastRaw !== null) setChain(m.lastRaw * lin(m.db));
                return m.db;
            },
        };
        return m;
    }

    if (typeof module !== 'undefined' && module.exports) {
        module.exports = { rewriteSetGain, createMaster, clampDb, MIN, MAX };
        return;
    }
    if (window.__guitarVolume) return;
    const desktop = window.feedBackDesktop || window.slopsmithDesktop;
    const api = desktop && desktop.audio;
    if (!api || typeof api.setGain !== 'function') return;   // browser: no native amp chain

    let saved = 0;
    try { saved = Number(localStorage.getItem(KEY)) || 0; } catch (_) { /* ignore */ }
    const master = createMaster(saved, (v) => { Promise.resolve(api.setGain('chain', v)).catch(() => {}); });
    window.__guitarVolume = master;
    window.__gvGain = master.gain;

    const origAppendChild = Node.prototype.appendChild;
    Node.prototype.appendChild = function (el) {
        const id = el && el.tagName === 'SCRIPT' && el.dataset ? el.dataset.pluginId : null;
        if (!id || !TARGETS.includes(id) || !el.src || el.__gvPatched || el.type === 'module') return origAppendChild.call(this, el);
        el.__gvPatched = true;
        const parent = this, src = el.src;
        fetch(src).then((r) => { if (!r.ok) throw new Error('HTTP ' + r.status); return r.text(); }).then((code) => {
            const res = rewriteSetGain(code);
            if (res.count) {
                el.src = URL.createObjectURL(new Blob([res.code + '\n//# sourceURL=' + src], { type: 'text/javascript' }));
                console.log('[guitar_volume] routed ' + res.count + ' setGain call(s) in ' + id);
            }
        }).catch((e) => console.warn('[guitar_volume] could not patch ' + id + ', loading stock:', e))
            .finally(() => origAppendChild.call(parent, el));
        return el;
    };

    function registerFader() {
        const mix = (window.feedBack || window.slopsmith || {}).audio;
        if (!mix || typeof mix.registerFader !== 'function') return false;
        mix.registerFader({
            id: 'guitar_volume',
            label: 'Guitar',
            unit: 'dB',
            min: MIN, max: MAX, step: 1,
            defaultValue: 0,
            getValue: () => master.db,
            setValue: (v) => {
                const db = master.setDb(v);
                try { localStorage.setItem(KEY, String(db)); } catch (_) { /* ignore */ }
            },
        });
        return true;
    }
    if (!registerFader()) window.addEventListener('feedBack:audio:ready', registerFader, { once: true });
})();

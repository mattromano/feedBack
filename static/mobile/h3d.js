// 3D Highway renderer for the phone player.
//
// The desktop app's default view is the bundled 3D Highway plugin
// (plugins/highway_3d), a feedBack#36 setRenderer factory. This module loads
// it the way core does, without core's app shell:
//   * plugin-loader.js: the plugin's screen.js is a CLASSIC script
//     (plugin.json has no scriptType), injected as <script src> into <body>,
//     plus its `styles` file as a <link rel=stylesheet>. Unlike core we omit
//     the ?v=<version> query so the URLs match the service-worker precache
//     keys (sw.js only serves query-less precached paths); the precache
//     version changes whenever these files change, so this does not go stale.
//   * viz.js: the factory is window.feedBackViz_highway_3d; it is gated on
//     WebGL2 (factory.contextType === 'webgl2', the same _canRun3D() probe),
//     the instance is tagged pluginId/source = 'highway_3d', and handed to
//     highway.setRenderer().
//
// No globals are stubbed. Every window.feedBack / feedBackSplitscreen /
// feedBackDesktop / registerShortcut access in the plugin is guarded, and
// with window.feedBack undefined it simply skips the bus subscriptions
// (highway:visibility, canvas-replaced, note:hit, v3 player-chrome control).
//
// The plugin loads three.js with import('/static/vendor/three/...'); its CDN
// fallback only runs if that local import fails, and the page's CSP
// (script-src 'self') blocks it anyway.

export const H3D_ID = 'highway_3d';
export const H3D_SCRIPT_URL = '/api/plugins/highway_3d/screen.js';
export const H3D_STYLES_URL = '/api/plugins/highway_3d/assets/plugin.css';
export const HW_MODE_KEY = 'fbm.highwayMode';

// Phone defaults, written through the plugin's own settings store (the
// h3d_bg_<key> localStorage slots that settings.html writes) only when the
// user has no value yet, so an explicit choice is never overwritten.
//   bloom: the UnrealBloom post-process chain (EffectComposer with a
//   4x-multisampled HalfFloat target + a mip chain of blur passes) is the
//   single most expensive effect in the plugin and the one an iPhone GPU
//   feels. Everything else stays at the desktop defaults.
export const H3D_PHONE_DEFAULTS = Object.freeze({
    h3d_bg_bloom: 'false',
});

// View settings (Settings tab), stored in the mobile app's own keys.
//   zoom: the 3D camera's vertical field of view. 70° is the plugin's
//   BASE_VFOV, tuned for a 16:9 monitor; a ~2.2:1 phone in landscape shows
//   the neck small and far away at that angle. Applied through the plugin's
//   own framing bridge (window.__h3dAspectTune, persisted under
//   h3d_aspect_tune2), which camUpdate() reads every frame. The bridge only
//   applies baseVfov while `enabled`, and enabled also arms its wide-pane
//   Hor+ hold and pose nudges past `startAspect`; a startAspect no screen
//   reaches keeps those off, so the only effect is the narrower fov.
//   quality: 'sharp' pins highway.js's load-adaptive render scale at full
//   resolution (setMinRenderScale(1)); 'auto' lets it drop to half when the
//   phone falls behind. highway.js's own floor is 0.25, which is visibly blurry.
export const ZOOM_KEY = 'fbm.zoom';
export const QUALITY_KEY = 'fbm.quality';
export const ZOOM_VFOV = Object.freeze({ normal: 70, close: 62, closer: 58 });
const ZOOM_NO_WIDE_ASPECT = 1000;
export const QUALITY_MIN_SCALE = Object.freeze({ sharp: 1, auto: 0.5 });
const H3D_TUNE_KEY = 'h3d_aspect_tune2';

function storage() {
    try { return globalThis.localStorage || null; } catch (_) { return null; }
}

/** '3d' or '2d' — the Settings choice. 3D unless the user picked 2D. */
export function getHighwayMode() {
    const ls = storage();
    let v = null;
    try { v = ls && ls.getItem(HW_MODE_KEY); } catch (_) { /* blocked */ }
    return v === '2d' ? '2d' : '3d';
}

export function setHighwayMode(mode) {
    const ls = storage();
    try { if (ls) ls.setItem(HW_MODE_KEY, mode === '2d' ? '2d' : '3d'); } catch (_) { /* blocked */ }
}

function readKey(key) {
    const ls = storage();
    try { return ls ? ls.getItem(key) : null; } catch (_) { return null; }
}

function writeKey(key, v) {
    const ls = storage();
    try { if (ls) ls.setItem(key, v); } catch (_) { /* blocked */ }
}

/** 'normal' | 'close' | 'closer'; 'close' unless the user picked another. */
export function getZoom() {
    const v = readKey(ZOOM_KEY);
    return Object.prototype.hasOwnProperty.call(ZOOM_VFOV, v) ? v : 'close';
}

export function setZoom(zoom) {
    writeKey(ZOOM_KEY, Object.prototype.hasOwnProperty.call(ZOOM_VFOV, zoom) ? zoom : 'close');
    applyZoom();
}

/**
 * Push the chosen zoom into the plugin's framing bridge: the live object if
 * the plugin is loaded (takes effect next frame), and its saved copy so the
 * next page load starts there. Other saved framing fields are kept.
 */
export function applyZoom(win = globalThis.window) {
    const vfov = ZOOM_VFOV[getZoom()];
    const on = vfov !== ZOOM_VFOV.normal;
    const patch = on
        ? { enabled: true, splitOnly: false, baseVfov: vfov, startAspect: ZOOM_NO_WIDE_ASPECT }
        : { enabled: false, baseVfov: vfov };
    if (win && win.__h3dAspectTune && typeof win.__h3dAspectTune === 'object') {
        Object.assign(win.__h3dAspectTune, patch);
    }
    let saved = {};
    try {
        const raw = readKey(H3D_TUNE_KEY);
        const parsed = raw ? JSON.parse(raw) : null;
        if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) saved = parsed;
    } catch (_) { /* corrupt: start over */ }
    Object.assign(saved, patch);
    writeKey(H3D_TUNE_KEY, JSON.stringify(saved));
}

/** 'sharp' | 'auto'; 'sharp' unless the user picked 'auto'. */
export function getQuality() {
    return readKey(QUALITY_KEY) === 'auto' ? 'auto' : 'sharp';
}

export function setQuality(q) {
    writeKey(QUALITY_KEY, q === 'auto' ? 'auto' : 'sharp');
}

export function minRenderScale() {
    return QUALITY_MIN_SCALE[getQuality()];
}

export function seedPhoneDefaults(ls = storage()) {
    if (!ls) return;
    for (const [k, v] of Object.entries(H3D_PHONE_DEFAULTS)) {
        try { if (ls.getItem(k) == null) ls.setItem(k, v); } catch (_) { /* blocked */ }
    }
}

// Same probe as core's viz.js _canRun3D(): create a context, then lose it so
// the probe canvas does not count against the browser's live-context cap.
let _webgl2 = null;
export function canRun3D(doc = globalThis.document) {
    if (_webgl2 !== null) return _webgl2;
    try {
        const c = doc.createElement('canvas');
        const gl = c.getContext('webgl2');
        _webgl2 = !!gl;
        const ext = gl && gl.getExtension && gl.getExtension('WEBGL_lose_context');
        if (ext && ext.loseContext) ext.loseContext();
    } catch (_) {
        _webgl2 = false;
    }
    return _webgl2;
}

function injectStyles(doc) {
    if (doc.querySelector(`link[data-plugin-id="${H3D_ID}"]`)) return;
    const link = doc.createElement('link');
    link.rel = 'stylesheet';
    link.href = H3D_STYLES_URL;
    link.dataset.pluginId = H3D_ID;
    // Optional: the renderer draws into its own WebGL canvas; plugin.css only
    // styles Tailwind utility classes on its optional DOM panels.
    link.onerror = () => console.warn('3D Highway: plugin.css failed to load');
    doc.head.appendChild(link);
}

let _loading = null;
/**
 * Load the plugin once per page. Resolves with the factory
 * (window.feedBackViz_highway_3d); rejects if the script fails to load or
 * does not register it. A failed load is not cached, so a later open retries.
 */
export function loadH3DFactory(win = globalThis.window, doc = globalThis.document) {
    if (typeof win.feedBackViz_highway_3d === 'function') return Promise.resolve(win.feedBackViz_highway_3d);
    if (_loading) return _loading;
    seedPhoneDefaults();
    applyZoom(win);
    injectStyles(doc);
    _loading = new Promise((resolve, reject) => {
        const script = doc.createElement('script');
        script.src = H3D_SCRIPT_URL;
        script.dataset.pluginId = H3D_ID;
        script.onload = () => {
            const f = win.feedBackViz_highway_3d;
            if (typeof f === 'function') resolve(f);
            else reject(new Error('3D Highway did not register a renderer'));
        };
        script.onerror = () => {
            script.remove();
            reject(new Error('3D Highway failed to load'));
        };
        doc.body.appendChild(script);
    });
    _loading.catch(() => { _loading = null; });
    return _loading;
}

/**
 * Build one renderer instance from the factory, as viz.js does, with one
 * phone-specific tweak: the plugin's audio-reactive background taps #audio
 * with createMediaElementSource(), which on iOS reroutes the song through a
 * Web Audio graph (silent until that context resumes, and it breaks
 * lock-screen / background playback). The plugin's documented per-render
 * opt-out, bundle.bgReactive === false (read once in init), turns that off
 * for this instance without touching the shared h3d_bg_reactive setting.
 */
export function createH3DRenderer(factory) {
    const r = factory();
    if (!r || typeof r.draw !== 'function') throw new Error('3D Highway returned an invalid renderer');
    try {
        if (!r.pluginId) r.pluginId = H3D_ID;
        if (!r.source) r.source = H3D_ID;
    } catch (_) { /* frozen renderer: tagging is cosmetic */ }
    const init = r.init;
    if (typeof init === 'function') {
        // `this` stays the renderer: the plugin sets this.readyPromise in
        // init(), and highway.js reads readyPromise off the installed object.
        r.init = function (canvas, bundle) {
            if (bundle && typeof bundle === 'object') bundle.bgReactive = false;
            return init.call(this, canvas, bundle);
        };
    }
    return r;
}

// Test hook: reset module-level memoization.
export function _resetForTests() {
    _webgl2 = null;
    _loading = null;
}

// feedBack Mobile service worker. Served from /mobile/sw.js (scope /mobile/)
// by lib/routers/mobile.py, which replaces the cache-version placeholder below
// with `<VERSION>-<hash of precached files>` — so any shell change installs a
// new worker and the page offers a reload.
//
// Song data lives in IndexedDB, never in this cache. /api/* and /ws/* always
// go to the network, except the 3D Highway plugin files precached below.

const CACHE_PREFIX = 'fbm-shell-';
const CACHE_NAME = CACHE_PREFIX + '__FBM_CACHE_VERSION__';

// The shell, plus static/highway.js and its full transitive import set
// (highway.js -> js/highway-{constants,geometry,state-primitives,draw,av-offset}.js;
// highway-draw.js and highway-geometry.js only import from that same set).
// Keep in sync with PRECACHE/the static allowlist in lib/mobile_gate.py.
const PRECACHE = [
    '/mobile/',
    '/mobile/manifest.webmanifest',
    '/static/mobile/mobile.css',
    '/static/mobile/mobile.js',
    '/static/mobile/player.js',
    '/static/mobile/fbm.js',
    '/static/mobile/store.js',
    '/static/mobile/fakews.js',
    '/static/mobile/h3d.js',
    '/static/mobile/icons/icon-180.png',
    '/static/mobile/icons/icon-192.png',
    '/static/mobile/icons/icon-512.png',
    '/static/highway.js',
    '/static/js/highway-constants.js',
    '/static/js/highway-geometry.js',
    '/static/js/highway-state-primitives.js',
    '/static/js/highway-draw.js',
    '/static/js/highway-av-offset.js',
];

// The 3D Highway renderer (plugins/highway_3d, loaded by h3d.js): three.js,
// the full transitive import set of the four postprocessing addons the
// plugin's bloom path imports (EffectComposer, RenderPass, UnrealBloomPass,
// OutputPass -> Pass, ShaderPass, MaskPass, CopyShader,
// LuminosityHighPassShader, OutputShader), the plugin's classic screen.js and
// its stylesheet. Best-effort: a failure here (e.g. the plugin is disabled)
// does not fail the install; the player then falls back to the 2D highway.
// Keep in sync with the gate allowlist (lib/mobile_gate.py) and the hashed
// file set in lib/routers/mobile.py _precache_hash().
const PRECACHE_OPTIONAL = [
    '/static/vendor/three/three.module.min.js',
    '/static/vendor/three/addons/postprocessing/EffectComposer.js',
    '/static/vendor/three/addons/postprocessing/RenderPass.js',
    '/static/vendor/three/addons/postprocessing/UnrealBloomPass.js',
    '/static/vendor/three/addons/postprocessing/OutputPass.js',
    '/static/vendor/three/addons/postprocessing/Pass.js',
    '/static/vendor/three/addons/postprocessing/ShaderPass.js',
    '/static/vendor/three/addons/postprocessing/MaskPass.js',
    '/static/vendor/three/addons/shaders/CopyShader.js',
    '/static/vendor/three/addons/shaders/LuminosityHighPassShader.js',
    '/static/vendor/three/addons/shaders/OutputShader.js',
    '/api/plugins/highway_3d/screen.js',
    '/api/plugins/highway_3d/assets/plugin.css',
];

const PRECACHE_PATHS = new Set([...PRECACHE, ...PRECACHE_OPTIONAL]);

// Only cache complete, same-origin, successful responses. A redirected
// response is re-wrapped: Safari refuses to serve a navigation from a cached
// response that still carries redirect info.
async function cacheable(res) {
    if (!res || !res.ok || res.status !== 200 || res.type !== 'basic') return null;
    if (!res.redirected) return res;
    const body = await res.blob();
    return new Response(body, { status: res.status, statusText: res.statusText, headers: res.headers });
}

self.addEventListener('install', (event) => {
    // No skipWaiting() here: an updated worker waits until the page asks
    // (SKIP_WAITING) so the user gets a "New version — reload" prompt.
    event.waitUntil((async () => {
        const cache = await caches.open(CACHE_NAME);
        const put = async (url) => {
            const res = await fetch(new Request(url, { cache: 'reload', credentials: 'same-origin' }));
            const ok = await cacheable(res);
            if (!ok) throw new Error(`precache failed for ${url}: ${res && res.status}`);
            await cache.put(url, ok);
        };
        await Promise.all([
            ...PRECACHE.map(put),
            ...PRECACHE_OPTIONAL.map((url) => put(url).catch((e) => {
                console.warn('optional precache skipped:', url, e && e.message);
            })),
        ]);
    })());
});

self.addEventListener('activate', (event) => {
    event.waitUntil((async () => {
        const keys = await caches.keys();
        await Promise.all(keys
            .filter((k) => k.startsWith(CACHE_PREFIX) && k !== CACHE_NAME)
            .map((k) => caches.delete(k)));
        await self.clients.claim();
    })());
});

self.addEventListener('message', (event) => {
    if (event.data === 'SKIP_WAITING' || (event.data && event.data.type === 'SKIP_WAITING')) {
        self.skipWaiting();
    }
});

async function cacheFirst(key, request) {
    const cache = await caches.open(CACHE_NAME);
    const hit = await cache.match(key);
    if (hit) return hit;
    return fetch(request);
}

self.addEventListener('fetch', (event) => {
    const req = event.request;
    if (req.method !== 'GET') return;
    const url = new URL(req.url);
    if (url.origin !== self.location.origin) return;
    const path = url.pathname;

    // Never cache or intercept API, WebSocket, or streamed audio traffic
    // (the precached 3D Highway plugin files under /api/plugins/ excepted).
    const precached = url.search === '' && PRECACHE_PATHS.has(path);
    if (!precached && (path.startsWith('/api/') || path.startsWith('/ws/') || path.startsWith('/audio/'))) return;
    if (path === '/mobile/sw.js') return;

    // App launches (including offline launches from the home screen).
    if (req.mode === 'navigate' && (path === '/mobile' || path.startsWith('/mobile/'))) {
        event.respondWith(cacheFirst('/mobile/', req));
        return;
    }

    if (precached) {
        event.respondWith(cacheFirst(path, req));
    }
    // Anything else: default network handling.
});

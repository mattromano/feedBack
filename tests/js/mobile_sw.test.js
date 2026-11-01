// Phone app service worker (static/mobile/sw.js): precache on install, cache
// routing on fetch, old-cache cleanup on activate, SKIP_WAITING on request.
// Runs the real worker script in a vm context with a fake `self`/caches/fetch.
'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const SRC = fs.readFileSync(path.join(__dirname, '..', '..', 'static', 'mobile', 'sw.js'), 'utf8');
const ORIGIN = 'https://mac.tailnet.ts.net';

function makeEnv({ failUrl = null, version = 'v1-abc' } = {}) {
    const stores = new Map(); // name -> Map(url -> Response)
    const caches = {
        async open(name) {
            if (!stores.has(name)) stores.set(name, new Map());
            const m = stores.get(name);
            return {
                async put(k, res) { m.set(typeof k === 'string' ? k : new URL(k.url).pathname, res); },
                async match(k) { return m.get(typeof k === 'string' ? k : new URL(k.url).pathname); },
            };
        },
        async keys() { return [...stores.keys()]; },
        async delete(name) { return stores.delete(name); },
    };
    const fetched = [];
    const fetchImpl = async (req) => {
        const url = typeof req === 'string' ? req : req.url;
        const p = new URL(url, ORIGIN).pathname;
        fetched.push(p);
        if (p === failUrl) return { ok: false, status: 404, type: 'basic', redirected: false };
        return { ok: true, status: 200, type: 'basic', redirected: false, body: `net:${p}` };
    };
    const listeners = {};
    let skipped = 0;
    let claimed = 0;
    const self = {
        location: new URL(`${ORIGIN}/mobile/sw.js`),
        addEventListener(type, fn) { listeners[type] = fn; },
        skipWaiting() { skipped++; },
        clients: { claim() { claimed++; } },
    };
    const ctx = {
        self, caches, fetch: fetchImpl, URL,
        Request: class { constructor(url, init) { this.url = new URL(url, ORIGIN).href; Object.assign(this, init); } },
        Response: class { constructor(body, init) { this.body = body; Object.assign(this, init); } },
        Promise, Set, Map, Error,
    };
    vm.runInNewContext(SRC.replace('__FBM_CACHE_VERSION__', version), ctx);
    return {
        listeners, stores, fetched,
        get skipped() { return skipped; },
        get claimed() { return claimed; },
        async dispatch(type, ev = {}) {
            let p = null;
            let responded;
            const e = Object.assign({
                waitUntil(x) { p = x; },
                respondWith(x) { responded = x; },
            }, ev);
            listeners[type](e);
            if (p) await p;
            return responded === undefined ? undefined : await responded;
        },
    };
}

const req = (p, mode = 'cors', method = 'GET') => ({ url: `${ORIGIN}${p}`, mode, method });

test('sw: template placeholder is present for the server to substitute', () => {
    assert.match(SRC, /__FBM_CACHE_VERSION__/);
});

test('sw: install precaches the shell + highway import set, without skipWaiting', async () => {
    const env = makeEnv();
    await env.dispatch('install');
    const cache = env.stores.get('fbm-shell-v1-abc');
    for (const p of ['/mobile/', '/mobile/manifest.webmanifest', '/static/mobile/mobile.js', '/static/mobile/player.js',
        '/static/mobile/fakews.js', '/static/highway.js', '/static/js/highway-draw.js', '/static/js/highway-av-offset.js']) {
        assert.ok(cache.has(p), `precached ${p}`);
    }
    assert.equal(env.skipped, 0);
});

test('sw: install precaches the 3D Highway renderer files', async () => {
    const env = makeEnv();
    await env.dispatch('install');
    const cache = env.stores.get('fbm-shell-v1-abc');
    for (const p of ['/static/mobile/h3d.js', '/static/vendor/three/three.module.min.js',
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
        '/api/plugins/highway_3d/screen.js', '/api/plugins/highway_3d/assets/plugin.css']) {
        assert.ok(cache.has(p), `precached ${p}`);
    }
});

test('sw: the precached 3D addon set is the transitive import closure of what the plugin imports', () => {
    const fsPath = (p) => path.join(__dirname, '..', '..', p.replace(/^\//, ''));
    const plugin = fs.readFileSync(fsPath('/plugins/highway_3d/screen.js'), 'utf8');
    const A = '/static/vendor/three/addons/';
    const roots = [...plugin.matchAll(/import\(A \+ '([^']+)'\)/g)].map((m) => A + m[1]);
    assert.ok(roots.length >= 4, 'plugin bloom imports found');
    const seen = new Set();
    const queue = [...roots];
    while (queue.length) {
        const p = queue.pop();
        if (seen.has(p)) continue;
        seen.add(p);
        const src = fs.readFileSync(fsPath(p), 'utf8');
        for (const m of src.matchAll(/from\s+'([^']+)'/g)) {
            queue.push(new URL(m[1], `https://x${p}`).pathname);
        }
    }
    const precached = new Set([...SRC.matchAll(/'(\/static\/vendor\/three\/[^']+)'/g)].map((m) => m[1]));
    assert.deepEqual([...precached].sort(), [...seen].sort());
});

test('sw: a missing 3D Highway file does not fail the install', async () => {
    const env = makeEnv({ failUrl: '/api/plugins/highway_3d/screen.js' });
    await env.dispatch('install');
    const cache = env.stores.get('fbm-shell-v1-abc');
    assert.ok(cache.has('/static/highway.js'));
    assert.ok(!cache.has('/api/plugins/highway_3d/screen.js'));
});

test('sw: install fails (no partial cache use) when a precache fetch fails', async () => {
    const env = makeEnv({ failUrl: '/static/highway.js' });
    await assert.rejects(env.dispatch('install'), /precache failed/);
});

test('sw: activate deletes old shell caches and claims clients', async () => {
    const env = makeEnv();
    await env.dispatch('install');
    env.stores.set('fbm-shell-old', new Map());
    env.stores.set('unrelated', new Map());
    await env.dispatch('activate');
    assert.deepEqual([...env.stores.keys()].sort(), ['fbm-shell-v1-abc', 'unrelated']);
    assert.equal(env.claimed, 1);
});

test('sw: SKIP_WAITING message activates the waiting worker', async () => {
    const env = makeEnv();
    env.listeners.message({ data: 'SKIP_WAITING' });
    env.listeners.message({ data: 'something else' });
    assert.equal(env.skipped, 1);
});

test('sw: fetch routing', async () => {
    const env = makeEnv();
    await env.dispatch('install');
    env.fetched.length = 0;

    // Navigation to the app is served from cache (offline launch).
    const nav = await env.dispatch('fetch', { request: req('/mobile/', 'navigate') });
    assert.equal(nav.body, 'net:/mobile/');
    assert.equal(env.fetched.length, 0);

    // Precached asset: cache-first.
    const js = await env.dispatch('fetch', { request: req('/static/highway.js') });
    assert.equal(js.body, 'net:/static/highway.js');
    assert.equal(env.fetched.length, 0);

    // The precached 3D Highway plugin files under /api/plugins/: cache-first,
    // but only query-less (a ?v= URL is not the precached response).
    const plug = await env.dispatch('fetch', { request: req('/api/plugins/highway_3d/screen.js', 'no-cors') });
    assert.equal(plug.body, 'net:/api/plugins/highway_3d/screen.js');
    assert.equal(env.fetched.length, 0);
    assert.equal(await env.dispatch('fetch', { request: req('/api/plugins/highway_3d/screen.js?v=1') }), undefined);
    assert.equal(await env.dispatch('fetch', { request: req('/api/plugins/highway_3d/settings.html') }), undefined);

    // API, WebSocket, audio, non-GET, cross-origin: not intercepted at all.
    for (const r of [req('/api/mobile/songs'), req('/api/mobile/songs/x/download'), req('/ws/highway/x'),
        req('/audio/a.mp3'), req('/static/mobile/mobile.js', 'cors', 'POST'),
        { url: 'https://cdn.example.com/x.js', mode: 'cors', method: 'GET' }]) {
        assert.equal(await env.dispatch('fetch', { request: r }), undefined, r.url);
    }
    // Not precached: default network handling.
    assert.equal(await env.dispatch('fetch', { request: req('/static/app.js') }), undefined);
});

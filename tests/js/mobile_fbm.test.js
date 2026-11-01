// Phone app (static/mobile/): .fbm pack parser and the offline FakeWebSocket
// replay that feeds stored arrangement messages to highway.js.
'use strict';

const { test, before } = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const { pathToFileURL } = require('node:url');

let fbm;
let fakews;

const mod = (name) => pathToFileURL(path.join(__dirname, '..', '..', 'static', 'mobile', name)).href;

before(async () => {
    fbm = await import(mod('fbm.js'));
    fakews = await import(mod('fakews.js'));
});

// Build a pack: files = [{key, mime, bytes}] laid out back to back unless an
// explicit offset/length override is given.
function buildFbm({ header = {}, files = [], magic = 'FBMP', truncateTo = null, fileTable = null } = {}) {
    let pos = 0;
    const table = files.map((f) => {
        const e = { key: f.key, mime: f.mime || 'audio/mp4', offset: pos, length: f.bytes.length };
        pos += f.bytes.length;
        return e;
    });
    const h = Object.assign({
        format: 1,
        id: 'Artist/song.feedpak',
        version: 'a'.repeat(40),
        title: 'Song',
        artist: 'Artist',
        album: '',
        duration: 10,
        built_at: '2026-10-31T00:00:00Z',
        arrangements: [{ index: 0, name: 'Lead', messages: [{ type: 'ready' }] }],
        files: fileTable || table,
    }, header);
    const json = Buffer.from(JSON.stringify(h), 'utf8');
    const blobSection = Buffer.concat(files.map((f) => Buffer.from(f.bytes)));
    const out = Buffer.alloc(8 + json.length + blobSection.length);
    out.write(magic, 0, 'ascii');
    out.writeUInt32BE(json.length, 4);
    json.copy(out, 8);
    blobSection.copy(out, 8 + json.length);
    const sliced = truncateTo === null ? out : out.subarray(0, truncateTo);
    return sliced.buffer.slice(sliced.byteOffset, sliced.byteOffset + sliced.byteLength);
}

// ── parseFbm ────────────────────────────────────────────────────────────────

test('parseFbm: happy path returns header, blob table and blob-section offset', () => {
    const buf = buildFbm({
        files: [
            { key: 'full', mime: 'audio/mp4', bytes: [1, 2, 3, 4] },
            { key: 'stem:guitar', mime: 'audio/mp4', bytes: [5, 6] },
            { key: 'cover', mime: 'image/jpeg', bytes: [7] },
        ],
    });
    const { header, blobs, blobSectionOffset } = fbm.parseFbm(buf);
    assert.equal(header.format, 1);
    assert.equal(header.id, 'Artist/song.feedpak');
    assert.equal(blobs.size, 3);
    assert.deepEqual(blobs.get('full'), { mime: 'audio/mp4', offset: 0, length: 4 });
    assert.deepEqual(blobs.get('stem:guitar'), { mime: 'audio/mp4', offset: 4, length: 2 });
    const bytes = new Uint8Array(buf);
    const g = blobs.get('stem:guitar');
    assert.deepEqual([...bytes.subarray(blobSectionOffset + g.offset, blobSectionOffset + g.offset + g.length)], [5, 6]);
    assert.equal(fbm.fbmBlobSectionOffset(buf), blobSectionOffset);
});

test('parseFbm: accepts a typed-array view', () => {
    const buf = buildFbm({ files: [{ key: 'audio', bytes: [9] }] });
    const { blobs } = fbm.parseFbm(new Uint8Array(buf));
    assert.equal(blobs.get('audio').length, 1);
});

test('parseFbm: rejects non-audio/image file types', () => {
    for (const mime of ['text/html', 'image/svg+xml', 'image/svg+xml;x', 'application/javascript']) {
        const buf = buildFbm({ files: [{ key: 'full', mime, bytes: [1, 2, 3] }] });
        assert.throws(() => fbm.parseFbm(buf), /unsupported type|no mime/, mime);
    }
    const ok = buildFbm({ files: [{ key: 'cover', mime: 'image/webp', bytes: [1] }] });
    assert.equal(fbm.parseFbm(ok).blobs.get('cover').mime, 'image/webp');
});

test('parseFbm: rejects bad magic', () => {
    const buf = buildFbm({ magic: 'ZIPX' });
    assert.throws(() => fbm.parseFbm(buf), /bad magic/);
});

test('parseFbm: rejects a file shorter than the fixed preamble', () => {
    assert.throws(() => fbm.parseFbm(new ArrayBuffer(5)), /truncated/);
});

test('parseFbm: rejects a header that runs past the end of the file', () => {
    const full = buildFbm({ files: [{ key: 'full', bytes: [1, 2, 3] }] });
    assert.throws(() => fbm.parseFbm(full.slice(0, 20)), /truncated/);
});

test('parseFbm: rejects a blob that runs past the end of the pack', () => {
    const full = buildFbm({ files: [{ key: 'full', bytes: [1, 2, 3, 4] }] });
    // Drop the last blob byte: header intact, blob section one byte short.
    assert.throws(() => fbm.parseFbm(full.slice(0, full.byteLength - 1)), /runs past the end/);
});

test('parseFbm: rejects an out-of-range offset in the file table', () => {
    const buf = buildFbm({
        files: [{ key: 'full', bytes: [1, 2, 3, 4] }],
        fileTable: [{ key: 'full', mime: 'audio/mp4', offset: 2, length: 4 }],
    });
    assert.throws(() => fbm.parseFbm(buf), /runs past the end/);
});

test('parseFbm: rejects negative / non-integer offsets', () => {
    for (const bad of [{ offset: -1, length: 1 }, { offset: 0.5, length: 1 }, { offset: 0, length: '1' }]) {
        const buf = buildFbm({
            files: [{ key: 'full', bytes: [1, 2] }],
            fileTable: [Object.assign({ key: 'full', mime: 'audio/mp4' }, bad)],
        });
        assert.throws(() => fbm.parseFbm(buf), /invalid offset/);
    }
});

test('parseFbm: rejects overlapping blobs', () => {
    const buf = buildFbm({
        files: [{ key: 'a', bytes: [1, 2, 3, 4, 5, 6] }],
        fileTable: [
            { key: 'full', mime: 'audio/mp4', offset: 0, length: 4 },
            { key: 'stem:bass', mime: 'audio/mp4', offset: 3, length: 3 },
        ],
    });
    assert.throws(() => fbm.parseFbm(buf), /overlap/);
});

test('parseFbm: rejects duplicate keys and unsupported format', () => {
    const dup = buildFbm({
        files: [{ key: 'a', bytes: [1, 2] }],
        fileTable: [
            { key: 'full', mime: 'audio/mp4', offset: 0, length: 1 },
            { key: 'full', mime: 'audio/mp4', offset: 1, length: 1 },
        ],
    });
    assert.throws(() => fbm.parseFbm(dup), /duplicated/);
    const v2 = buildFbm({ header: { format: 2 } });
    assert.throws(() => fbm.parseFbm(v2), /Unsupported pack format/);
});

test('parseFbm: rejects a header that is not JSON', () => {
    const out = Buffer.alloc(8 + 3);
    out.write('FBMP', 0, 'ascii');
    out.writeUInt32BE(3, 4);
    out.write('{{{', 8, 'ascii');
    assert.throws(() => fbm.parseFbm(out.buffer.slice(out.byteOffset, out.byteOffset + out.byteLength)), /not valid JSON/);
});

// ── fbm: URL rewriting ─────────────────────────────────────────────────────

const SONG_INFO = {
    type: 'song_info',
    title: 'Song',
    audio_url: 'fbm:stem:guitar',
    full_mix_url: 'fbm:full',
    original_audio_url: 'fbm:full',
    stems: [{ id: 'guitar', url: 'fbm:stem:guitar' }, { id: 'bass', url: 'fbm:stem:bass' }],
    arrangements: [{ index: 0, name: 'Lead' }],
};

test('collectFbmKeys / songInfoFbmKeys find every referenced key', () => {
    const keys = fakews.songInfoFbmKeys([SONG_INFO, { type: 'notes', data: [{ t: 1 }] }]);
    assert.deepEqual([...keys].sort(), ['full', 'stem:bass', 'stem:guitar']);
});

test('rewriteFbmUrls replaces fbm: refs, leaves everything else, does not mutate', () => {
    const before = JSON.stringify(SONG_INFO);
    const out = fakews.rewriteFbmUrls(SONG_INFO, (k) => (k === 'stem:bass' ? null : `blob:test/${k}`));
    assert.equal(out.audio_url, 'blob:test/stem:guitar');
    assert.equal(out.full_mix_url, 'blob:test/full');
    assert.equal(out.original_audio_url, 'blob:test/full');
    assert.equal(out.stems[0].url, 'blob:test/stem:guitar');
    assert.equal(out.stems[1].url, null, 'missing blob becomes null, never an fbm: URL');
    assert.equal(out.title, 'Song');
    assert.deepEqual(out.arrangements, SONG_INFO.arrangements);
    assert.equal(JSON.stringify(SONG_INFO), before);
});

test('rewriteSongInfoMessages only rewrites song_info messages', () => {
    const notes = { type: 'notes', data: [{ label: 'fbm:not-a-url-here' }] };
    const out = fakews.rewriteSongInfoMessages([SONG_INFO, notes], (k) => `blob:x/${k}`);
    assert.equal(out[0].full_mix_url, 'blob:x/full');
    assert.equal(out[1], notes, 'non-song_info messages pass through by reference');
});

// ── FakeWebSocket replay ───────────────────────────────────────────────────

function replay(messages, opts) {
    const Fake = fakews.createFakeWebSocketClass(messages, opts);
    const ws = new Fake('ws://host/ws/highway/x?arrangement=0');
    const got = [];
    const events = [];
    // highway.js assigns handlers synchronously after construction.
    ws.onopen = () => events.push('open');
    ws.onmessage = (ev) => got.push(JSON.parse(ev.data));
    ws.onclose = () => events.push('close');
    return { ws, got, events };
}

const until = async (cond, ms = 2000) => {
    const start = Date.now();
    while (!cond()) {
        if (Date.now() - start > ms) throw new Error('timed out');
        await new Promise((r) => setTimeout(r, 5));
    }
};

test('FakeWebSocket replays every message in order, asynchronously, after open', async () => {
    const msgs = [SONG_INFO, { type: 'beats', data: [] }];
    for (let i = 0; i < 230; i++) msgs.push({ type: 'notes', data: [{ i }] });
    msgs.push({ type: 'ready' });
    const { ws, got, events } = replay(msgs, { batchSize: 50 });
    assert.equal(ws.readyState, 0);
    assert.equal(got.length, 0, 'nothing is delivered synchronously from the constructor');
    assert.equal(ws.url, 'ws://host/ws/highway/x?arrangement=0');
    await until(() => got.length === msgs.length);
    assert.equal(events[0], 'open');
    assert.equal(ws.readyState, 1);
    assert.deepEqual(got, msgs);
    ws.send('ignored');
});

test('FakeWebSocket yields to the event loop between batches', async () => {
    const msgs = Array.from({ length: 120 }, (_, i) => ({ type: 'notes', data: [{ i }] }));
    const Fake = fakews.createFakeWebSocketClass(msgs, { batchSize: 50 });
    const ws = new Fake('ws://x');
    const got = [];
    const seen = [];
    // A timer queued during the first message must run before replay finishes.
    ws.onmessage = (ev) => {
        got.push(ev.data);
        if (got.length === 1) setTimeout(() => seen.push(got.length), 0);
    };
    await until(() => got.length === msgs.length);
    await until(() => seen.length === 1);
    assert.ok(seen[0] < msgs.length, `timer ran mid-replay (at ${seen[0]})`);
});

test('FakeWebSocket close() stops replay and fires onclose once', async () => {
    const msgs = Array.from({ length: 500 }, (_, i) => ({ type: 'notes', data: [{ i }] }));
    const { ws, got, events } = replay(msgs, { batchSize: 50 });
    await until(() => got.length >= 50);
    ws.close();
    ws.close();
    const n = got.length;
    await new Promise((r) => setTimeout(r, 30));
    assert.equal(got.length, n, 'no messages after close');
    assert.ok(n < msgs.length);
    assert.equal(ws.readyState, 3);
    assert.deepEqual(events, ['open', 'close']);
});

test('FakeWebSocket supports addEventListener alongside on* handlers', async () => {
    const Fake = fakews.createFakeWebSocketClass([{ type: 'ready' }]);
    const ws = new Fake('ws://x');
    const a = [];
    ws.addEventListener('message', (ev) => a.push(['listener', JSON.parse(ev.data).type]));
    ws.onmessage = (ev) => a.push(['on', JSON.parse(ev.data).type]);
    await until(() => a.length === 2);
    assert.deepEqual(a, [['on', 'ready'], ['listener', 'ready']]);
});

test('FakeWebSocket drives the real highway.js connect() when swapped in', async () => {
    // Minimal DOM shims, same shape as highway_secondary_connect.test.js.
    const saved = {};
    for (const k of ['window', 'document', 'localStorage', 'requestAnimationFrame', 'cancelAnimationFrame', 'addEventListener', 'removeEventListener', 'WebSocket']) {
        saved[k] = Object.getOwnPropertyDescriptor(global, k);
    }
    global.window = global;
    global.addEventListener = () => {};
    global.removeEventListener = () => {};
    const node = () => ({ style: {}, textContent: '', classList: { add() {}, remove() {}, toggle() {} }, appendChild() {}, remove() {} });
    global.document = { getElementById: () => null, createElement: node, body: { appendChild() {} }, documentElement: { clientWidth: 1000, clientHeight: 700 } };
    global.localStorage = { getItem: () => null, setItem() {}, removeItem() {} };
    global.requestAnimationFrame = () => 1;
    global.cancelAnimationFrame = () => {};
    global.WebSocket = class { constructor() { throw new Error('real WebSocket must not be used'); } };
    try {
        // highway.js has no exports; it publishes window.createHighway.
        await import(pathToFileURL(path.join(__dirname, '..', '..', 'static', 'highway.js')).href);
        assert.equal(typeof window.createHighway, 'function');
        const h = window.createHighway();
        const msgs = fakews.rewriteSongInfoMessages([
            Object.assign({}, SONG_INFO, { stringCount: 6, tuning: [0, 0, 0, 0, 0, 0] }),
            { type: 'beats', data: [{ time: 0, measure: 1 }] },
            { type: 'notes', data: [{ t: 1, s: 0, f: 3, sus: 0 }] },
            { type: 'ready' },
        ], (k) => `blob:test/${k}`);
        const infos = [];
        const RealWS = global.WebSocket;
        global.WebSocket = fakews.createFakeWebSocketClass(msgs);
        try {
            h.connect('ws://host/ws/highway/x?arrangement=0', { secondary: true, onSongInfo: (m) => infos.push(m), onError: () => {} });
        } finally {
            global.WebSocket = RealWS;
        }
        await until(() => h.getNotes().length === 1);
        assert.equal(infos.length, 1);
        assert.equal(infos[0].full_mix_url, 'blob:test/full');
        assert.equal(h.getBeats().length, 1);
        h.stop();
    } finally {
        for (const [k, d] of Object.entries(saved)) {
            if (d) Object.defineProperty(global, k, d); else delete global[k];
        }
    }
});

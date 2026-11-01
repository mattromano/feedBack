// IndexedDB storage for downloaded songs (docs/mobile-contract.md).
//
//   database `feedback-mobile`, version 1
//   songs  keyed by id:            {id, version, title, artist, album, duration,
//                                   size_bytes, downloaded_at, arrangements, files}
//   blobs  keyed by the array [id, key]: {mime, data: ArrayBuffer}
//          (array keys, not `${id}|${key}` strings, so no id or key
//          can collide with another song's blobs)
//
// A song and its blobs are written in ONE readwrite transaction across both
// stores, so a half-finished save is never visible.

import { fbmBlobSectionOffset } from './fbm.js';

const DB_NAME = 'feedback-mobile';
const DB_VERSION = 2;
const SONGS = 'songs';
const BLOBS = 'blobs';

let _dbPromise = null;

function reqP(req) {
    return new Promise((resolve, reject) => {
        req.onsuccess = () => resolve(req.result);
        req.onerror = () => reject(req.error);
    });
}

function txDone(tx) {
    return new Promise((resolve, reject) => {
        tx.oncomplete = () => resolve();
        tx.onerror = () => reject(tx.error || new Error('Storage transaction failed'));
        tx.onabort = () => reject(tx.error || new Error('Storage transaction aborted'));
    });
}

function openDb() {
    if (_dbPromise) return _dbPromise;
    _dbPromise = new Promise((resolve, reject) => {
        if (typeof indexedDB === 'undefined') {
            reject(new Error('This browser has no IndexedDB'));
            return;
        }
        const req = indexedDB.open(DB_NAME, DB_VERSION);
        req.onupgradeneeded = (ev) => {
            const db = req.result;
            // v1 used string blob keys; it never shipped, so drop it wholesale.
            if (ev.oldVersion < 2) {
                for (const name of [SONGS, BLOBS]) {
                    if (db.objectStoreNames.contains(name)) db.deleteObjectStore(name);
                }
            }
            if (!db.objectStoreNames.contains(SONGS)) db.createObjectStore(SONGS, { keyPath: 'id' });
            if (!db.objectStoreNames.contains(BLOBS)) db.createObjectStore(BLOBS);
        };
        req.onsuccess = () => {
            const db = req.result;
            // Another tab upgrading would block forever; let go instead.
            db.onversionchange = () => { db.close(); _dbPromise = null; };
            resolve(db);
        };
        req.onerror = () => { _dbPromise = null; reject(req.error); };
        req.onblocked = () => { /* wait; another tab holds the old version */ };
    });
    return _dbPromise;
}

// Every [id, <string key>] sorts between [id] and [id, []] (arrays sort after
// strings), and no other song's keys fall in that range.
function blobRange(id) {
    return IDBKeyRange.bound([id], [id, []]);
}

export function blobKey(id, key) {
    return [String(id), String(key)];
}

// Delete a song's blobs inside an open readwrite transaction. `then` runs
// (still inside the transaction) once the delete is queued, so writes it
// issues are ordered after it.
function deleteBlobsInTx(tx, id, then) {
    tx.objectStore(BLOBS).delete(blobRange(id));
    if (then) then();
}

function friendlyStorageError(e) {
    const name = e && e.name;
    if (name === 'QuotaExceededError') {
        return new Error('Not enough storage on this phone for that song. Delete some downloads and try again.');
    }
    return e instanceof Error ? e : new Error(String(e));
}

function fmtMB(n) {
    return `${(n / (1024 * 1024)).toFixed(0)} MB`;
}

/** Storage numbers, or nulls where the browser doesn't say. */
export async function estimate() {
    const out = { usage: null, quota: null, persisted: null };
    try {
        if (navigator.storage && navigator.storage.estimate) {
            const e = await navigator.storage.estimate();
            out.usage = Number.isFinite(e.usage) ? e.usage : null;
            out.quota = Number.isFinite(e.quota) ? e.quota : null;
        }
    } catch (_) { /* unsupported */ }
    try {
        if (navigator.storage && navigator.storage.persisted) out.persisted = await navigator.storage.persisted();
    } catch (_) { /* unsupported */ }
    return out;
}

/** Throw a friendly error when `bytes` (plus 10% headroom) won't fit. */
export async function ensureSpace(bytes) {
    if (!Number.isFinite(bytes) || bytes <= 0) return;
    const { usage, quota } = await estimate();
    if (usage === null || quota === null) return; // can't tell; let the write decide
    const free = quota - usage;
    const needed = bytes * 1.1;
    if (free < needed) {
        throw new Error(`Not enough storage: this song needs about ${fmtMB(needed)}, ${fmtMB(Math.max(0, free))} is free. Delete some downloads first.`);
    }
}

async function requestPersistence() {
    try {
        if (!navigator.storage || !navigator.storage.persist) return;
        if (navigator.storage.persisted && await navigator.storage.persisted()) return;
        await navigator.storage.persist();
    } catch (_) { /* best effort */ }
}

/**
 * Store a parsed pack.
 * @param {object} header       parseFbm(...).header
 * @param {ArrayBuffer} arrayBuffer  the whole .fbm file
 */
export async function saveSong(header, arrayBuffer) {
    await ensureSpace(arrayBuffer.byteLength);
    await requestPersistence();

    const base = fbmBlobSectionOffset(arrayBuffer);
    const id = header.id;
    const record = {
        id,
        version: header.version,
        title: header.title || '',
        artist: header.artist || '',
        album: header.album || '',
        duration: Number(header.duration) || 0,
        size_bytes: arrayBuffer.byteLength,
        downloaded_at: new Date().toISOString(),
        arrangements: header.arrangements.slice(),
        files: header.files.map((f) => ({ key: f.key, mime: f.mime, length: f.length })),
    };
    // Build the Blobs before opening the transaction: an IDB transaction
    // auto-commits as soon as it goes idle, so no awaits inside it.
    // Stored as {mime, data: ArrayBuffer} rather than Blob: WebKit refuses
    // Blobs in IndexedDB in some contexts (private browsing, automation), while
    // ArrayBuffers are always structured-cloneable. getBlob() rebuilds the Blob.
    const blobs = header.files.map((f) => [
        blobKey(id, f.key),
        { mime: f.mime, data: arrayBuffer.slice(base + f.offset, base + f.offset + f.length) },
    ]);

    const db = await openDb();
    try {
        const tx = db.transaction([SONGS, BLOBS], 'readwrite');
        const done = txDone(tx);
        // A re-download may drop old file keys, so clear the old blobs first.
        // Requests in one transaction run in order, so the puts land after.
        deleteBlobsInTx(tx, id, () => {
            try {
                const bstore = tx.objectStore(BLOBS);
                for (const [k, b] of blobs) bstore.put(b, k);
                tx.objectStore(SONGS).put(record);
            } catch (e) {
                // A synchronous put() failure (e.g. DataCloneError) must still
                // settle `done`; abort so onabort rejects it.
                try { tx.abort(); } catch { /* already finished */ }
            }
        });
        await done;
    } catch (e) {
        throw friendlyStorageError(e);
    }
    return record;
}

/** All song records without their (large) message arrays. */
export async function listSongs() {
    const db = await openDb();
    const tx = db.transaction(SONGS, 'readonly');
    const all = await reqP(tx.objectStore(SONGS).getAll());
    return all.map((s) => Object.assign({}, s, {
        arrangements: (s.arrangements || []).map((a) => ({ index: a.index, name: a.name })),
    }));
}

/** One full song record (with messages), or null. */
export async function getSong(id) {
    const db = await openDb();
    const tx = db.transaction(SONGS, 'readonly');
    const rec = await reqP(tx.objectStore(SONGS).get(id));
    return rec || null;
}

/** One stored Blob, or null. */
export async function getBlob(id, key) {
    const db = await openDb();
    const tx = db.transaction(BLOBS, 'readonly');
    const b = await reqP(tx.objectStore(BLOBS).get(blobKey(id, key)));
    if (!b) return null;
    if (typeof Blob !== 'undefined' && b instanceof Blob) return b;
    return new Blob([b.data], { type: b.mime || '' });
}

export async function deleteSong(id) {
    const db = await openDb();
    const tx = db.transaction([SONGS, BLOBS], 'readwrite');
    const done = txDone(tx);
    deleteBlobsInTx(tx, id);
    tx.objectStore(SONGS).delete(id);
    await done;
}

export async function deleteAll() {
    const db = await openDb();
    const tx = db.transaction([SONGS, BLOBS], 'readwrite');
    const done = txDone(tx);
    tx.objectStore(BLOBS).clear();
    tx.objectStore(SONGS).clear();
    await done;
}

// .fbm pack parser + downloader (format version 1, see docs/mobile-contract.md).
//
//   bytes 0..3   ASCII "FBMP"
//   bytes 4..7   uint32 big-endian N = header length
//   bytes 8..8+N UTF-8 JSON header
//   then         blob section; header.files[].offset is relative to it
//
// parseFbm is pure (no DOM) so it runs under node --test.

export const FBM_MAGIC = 'FBMP';
export const FBM_FORMAT = 1;

// Packs only carry audio and raster cover images (no SVG: it can hold script).
const MIME_RE = /^(?:audio\/[a-z0-9.+-]+|image\/(?:jpeg|png|webp|gif|avif))$/i;

export class FbmError extends Error {
    constructor(message) {
        super(message);
        this.name = 'FbmError';
    }
}

function isNonNegInt(n) {
    return Number.isSafeInteger(n) && n >= 0;
}

function toArrayBuffer(input) {
    if (input instanceof ArrayBuffer) return input;
    if (ArrayBuffer.isView(input)) {
        return input.buffer.slice(input.byteOffset, input.byteOffset + input.byteLength);
    }
    throw new FbmError('Pack data is not binary');
}

/**
 * Parse and validate a .fbm pack.
 * @param {ArrayBuffer|ArrayBufferView} input
 * @returns {{header: object, blobs: Map<string, {mime: string, offset: number, length: number}>, blobSectionOffset: number}}
 */
export function parseFbm(input) {
    const buf = toArrayBuffer(input);
    if (buf.byteLength < 8) throw new FbmError('Pack is truncated (no header)');
    const bytes = new Uint8Array(buf);
    const magic = String.fromCharCode(bytes[0], bytes[1], bytes[2], bytes[3]);
    if (magic !== FBM_MAGIC) throw new FbmError('Not a feedBack mobile pack (bad magic)');

    const headerLen = new DataView(buf).getUint32(4, false);
    const blobSectionOffset = 8 + headerLen;
    if (headerLen === 0 || blobSectionOffset > buf.byteLength) {
        throw new FbmError('Pack is truncated (header runs past end of file)');
    }

    let header;
    try {
        const text = new TextDecoder('utf-8', { fatal: true }).decode(new Uint8Array(buf, 8, headerLen));
        header = JSON.parse(text);
    } catch (e) {
        throw new FbmError('Pack header is not valid JSON');
    }
    if (!header || typeof header !== 'object' || Array.isArray(header)) {
        throw new FbmError('Pack header is not an object');
    }
    if (header.format !== FBM_FORMAT) {
        throw new FbmError(`Unsupported pack format ${JSON.stringify(header.format)} (expected ${FBM_FORMAT})`);
    }
    if (typeof header.id !== 'string' || !header.id) throw new FbmError('Pack header has no id');
    if (typeof header.version !== 'string' || !header.version) throw new FbmError('Pack header has no version');

    if (!Array.isArray(header.arrangements)) throw new FbmError('Pack header has no arrangements');
    for (const a of header.arrangements) {
        if (!a || typeof a !== 'object' || !isNonNegInt(a.index) || !Array.isArray(a.messages)) {
            throw new FbmError('Pack header has a malformed arrangement');
        }
    }

    if (!Array.isArray(header.files)) throw new FbmError('Pack header has no file table');
    const blobSectionLength = buf.byteLength - blobSectionOffset;
    const blobs = new Map();
    for (const f of header.files) {
        if (!f || typeof f !== 'object') throw new FbmError('Pack file entry is malformed');
        const { key, mime, offset, length } = f;
        if (typeof key !== 'string' || !key) throw new FbmError('Pack file entry has no key');
        if (key.includes('|')) throw new FbmError(`Pack file key ${JSON.stringify(key)} is invalid`);
        if (blobs.has(key)) throw new FbmError(`Pack file key ${JSON.stringify(key)} is duplicated`);
        if (typeof mime !== 'string' || !MIME_RE.test(mime)) {
            throw new FbmError(`Pack file ${key} has an unsupported type`);
        }
        if (!isNonNegInt(offset) || !isNonNegInt(length)) {
            throw new FbmError(`Pack file ${key} has an invalid offset/length`);
        }
        if (offset + length > blobSectionLength) {
            throw new FbmError(`Pack file ${key} runs past the end of the pack`);
        }
        blobs.set(key, { mime, offset, length });
    }

    // Reject overlapping ranges (zero-length entries cannot overlap anything).
    const ranges = [...blobs.entries()]
        .filter(([, b]) => b.length > 0)
        .sort((a, b) => a[1].offset - b[1].offset);
    for (let i = 1; i < ranges.length; i++) {
        const prev = ranges[i - 1][1];
        const cur = ranges[i][1];
        if (cur.offset < prev.offset + prev.length) {
            throw new FbmError(`Pack files ${ranges[i - 1][0]} and ${ranges[i][0]} overlap`);
        }
    }

    return { header, blobs, blobSectionOffset };
}

/** Read the blob-section offset from a pack without decoding the header. */
export function fbmBlobSectionOffset(buf) {
    if (!(buf instanceof ArrayBuffer) || buf.byteLength < 8) throw new FbmError('Pack is truncated');
    return 8 + new DataView(buf).getUint32(4, false);
}

async function errorText(res) {
    try {
        const ct = res.headers.get('content-type') || '';
        if (ct.includes('application/json')) {
            const j = await res.json();
            if (j && typeof j.detail === 'string') return j.detail;
            if (j && typeof j.error === 'string') return j.error;
        }
    } catch (_) { /* fall through */ }
    return '';
}

/**
 * Download a pack with progress.
 * The server may build the pack first, so several seconds can pass before
 * any headers arrive: callers show "Preparing…" until the first onProgress.
 * onProgress({loaded, total}) — total is null when the server sent no
 * Content-Length (show an indeterminate bar).
 * @returns {Promise<ArrayBuffer>}
 */
export async function downloadFbm(url, { onProgress, signal } = {}) {
    let res;
    try {
        res = await fetch(url, { signal, cache: 'no-store', credentials: 'same-origin' });
    } catch (e) {
        if (e && e.name === 'AbortError') throw e;
        throw new Error('Could not reach the server');
    }
    if (!res.ok) {
        const detail = await errorText(res);
        if (res.status === 503) {
            const ra = Number(res.headers.get('retry-after'));
            const when = Number.isFinite(ra) && ra > 0 ? `in about ${Math.ceil(ra)} s` : 'shortly';
            throw new Error(`The server is busy preparing another song — try again ${when}.`);
        }
        if (res.status === 500) throw new Error('The server could not prepare this song for download.');
        if (res.status === 404) throw new Error(detail || 'Song not found on the server.');
        throw new Error(detail || `Download failed (HTTP ${res.status})`);
    }
    const ct = (res.headers.get('content-type') || '').toLowerCase();
    if (!ct.startsWith('application/octet-stream')) {
        throw new Error(`Unexpected download type: ${ct || 'none'}`);
    }

    const lenHeader = res.headers.get('content-length');
    const total = lenHeader && /^\d+$/.test(lenHeader) ? Number(lenHeader) : null;
    const report = (loaded) => {
        if (typeof onProgress === 'function') {
            try { onProgress({ loaded, total }); } catch (_) { /* UI only */ }
        }
    };

    if (!res.body || typeof res.body.getReader !== 'function') {
        const ab = await res.arrayBuffer();
        report(ab.byteLength);
        return ab;
    }

    const reader = res.body.getReader();
    // Preallocate when the size is known so a 40 MB pack isn't copied twice.
    let out = total ? new Uint8Array(total) : null;
    const chunks = [];
    let loaded = 0;
    report(0);
    for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        if (out && loaded + value.byteLength <= out.byteLength) {
            out.set(value, loaded);
        } else {
            if (out) { chunks.push(out.subarray(0, loaded)); out = null; }
            chunks.push(value);
        }
        loaded += value.byteLength;
        report(loaded);
    }
    if (out) {
        if (loaded !== out.byteLength) throw new Error('Download was cut short');
        return out.buffer;
    }
    if (total !== null && loaded !== total) throw new Error('Download was cut short');
    const joined = new Uint8Array(loaded);
    let pos = 0;
    for (const c of chunks) { joined.set(c, pos); pos += c.byteLength; }
    return joined.buffer;
}

// Offline replay for highway.js.
//
// highway.js's only data path is `new WebSocket(url)`. For songs stored on the
// phone, player.js swaps window.WebSocket for a class made here, for the
// synchronous duration of highway.connect() only. The class replays the
// arrangement messages recorded in the .fbm pack, in order, exactly as the
// server would have sent them.
//
// Pure module: no DOM access, so it can be imported and tested under node.

const FBM_PREFIX = 'fbm:';

/** True when `v` is an `fbm:<key>` reference. */
export function isFbmRef(v) {
    return typeof v === 'string' && v.startsWith(FBM_PREFIX) && v.length > FBM_PREFIX.length;
}

/** Collect every `fbm:` key referenced anywhere inside `value`. */
export function collectFbmKeys(value, out = new Set()) {
    if (isFbmRef(value)) {
        out.add(value.slice(FBM_PREFIX.length));
    } else if (Array.isArray(value)) {
        for (const v of value) collectFbmKeys(v, out);
    } else if (value && typeof value === 'object') {
        for (const k of Object.keys(value)) collectFbmKeys(value[k], out);
    }
    return out;
}

/**
 * Deep-copy `value`, replacing every `fbm:<key>` string with `resolve(key)`.
 * When `resolve` returns null/undefined the reference becomes null, so the
 * player never hands an `fbm:` URL to an <audio> element.
 */
export function rewriteFbmUrls(value, resolve) {
    if (isFbmRef(value)) {
        const url = resolve(value.slice(FBM_PREFIX.length));
        return url == null ? null : url;
    }
    if (Array.isArray(value)) return value.map((v) => rewriteFbmUrls(v, resolve));
    if (value && typeof value === 'object') {
        const out = {};
        for (const k of Object.keys(value)) out[k] = rewriteFbmUrls(value[k], resolve);
        return out;
    }
    return value;
}

/** Keys referenced by the `song_info` messages of one arrangement. */
export function songInfoFbmKeys(messages) {
    const keys = new Set();
    for (const m of messages || []) {
        if (m && m.type === 'song_info') collectFbmKeys(m, keys);
    }
    return keys;
}

/**
 * Return a copy of `messages` with the `song_info` message(s) rewritten
 * through `resolve`. Other messages are passed through untouched (they carry
 * no URLs and can be large, so they are not copied).
 */
export function rewriteSongInfoMessages(messages, resolve) {
    return (messages || []).map((m) => (m && m.type === 'song_info' ? rewriteFbmUrls(m, resolve) : m));
}

const CONNECTING = 0;
const OPEN = 1;
const CLOSING = 2;
const CLOSED = 3;

/**
 * Build a WebSocket-compatible class whose instances replay `messages`.
 *
 * highway.js calls `new WebSocket(url)` and then assigns onmessage/onclose/
 * onerror synchronously, so replay starts on a later task. Every `batchSize`
 * messages it yields to the event loop so a long song doesn't freeze the UI.
 *
 * @param {object[]} messages  already-rewritten message objects
 * @param {{batchSize?: number, onCreate?: (ws) => void}} [opts]
 */
export function createFakeWebSocketClass(messages, opts = {}) {
    const batchSize = Math.max(1, opts.batchSize || 50);
    const payloads = (messages || []).map((m) => JSON.stringify(m));
    const yieldTask = () => new Promise((r) => setTimeout(r, 0));

    class FakeWebSocket {
        constructor(url) {
            this.url = String(url);
            this.readyState = CONNECTING;
            this.protocol = '';
            this.extensions = '';
            this.binaryType = 'blob';
            this.bufferedAmount = 0;
            this.onopen = null;
            this.onmessage = null;
            this.onclose = null;
            this.onerror = null;
            this._listeners = new Map();
            if (typeof opts.onCreate === 'function') opts.onCreate(this);
            setTimeout(() => { this._run(); }, 0);
        }

        addEventListener(type, fn) {
            if (typeof fn !== 'function') return;
            if (!this._listeners.has(type)) this._listeners.set(type, new Set());
            this._listeners.get(type).add(fn);
        }

        removeEventListener(type, fn) {
            const set = this._listeners.get(type);
            if (set) set.delete(fn);
        }

        _dispatch(type, extra) {
            const ev = Object.assign({ type, target: this, currentTarget: this }, extra);
            const handler = this['on' + type];
            if (typeof handler === 'function') {
                try { handler.call(this, ev); } catch (e) { console.error('[fakews] handler error', e); }
            }
            const set = this._listeners.get(type);
            if (set) {
                for (const fn of [...set]) {
                    try { fn.call(this, ev); } catch (e) { console.error('[fakews] listener error', e); }
                }
            }
        }

        async _run() {
            if (this.readyState !== CONNECTING) return;
            this.readyState = OPEN;
            this._dispatch('open', {});
            for (let i = 0; i < payloads.length; i++) {
                if (this.readyState !== OPEN) return;
                this._dispatch('message', { data: payloads[i] });
                if ((i + 1) % batchSize === 0) await yieldTask();
            }
        }

        send() { /* the highway protocol is server-push only */ }

        close(code = 1000, reason = '') {
            if (this.readyState === CLOSED || this.readyState === CLOSING) return;
            this.readyState = CLOSED;
            this._dispatch('close', { code, reason, wasClean: true });
        }
    }
    FakeWebSocket.CONNECTING = CONNECTING;
    FakeWebSocket.OPEN = OPEN;
    FakeWebSocket.CLOSING = CLOSING;
    FakeWebSocket.CLOSED = CLOSED;
    return FakeWebSocket;
}

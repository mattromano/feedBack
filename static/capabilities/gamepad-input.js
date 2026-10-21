// Core gamepad-input capability domain (spec 014).
//
// Lets a plugin claim one controller (a plastic guitar, a plastic drum kit, an
// Xbox pad) and receive its raw button/axis CHANGES while it plays it as an
// instrument. Core knows nothing about frets or pads: a consumer names the
// inputs through its own profiles. Core-owned (`core.gamepad-input`) for the
// same reason `midi-input` is: an input plane outlives any one feature.
//
// One owner per controller. Polling runs on requestAnimationFrame only while
// at least one claim exists. A claimed controller that unplugs keeps its handle
// (silent); when the same controllerKey reconnects, in any slot, events resume
// on that handle. `claimCount()` lets core's menu gamepad support stand down
// while any controller is being played.
//
// Live events go only through the in-page handle on
// `window.feedBack.gamepadInput`; the capability commands (`inspect`,
// `list-sources`) are read-only snapshots for the Capability Inspector.
(function () {
    'use strict';

    window.feedBack = window.feedBack || {};
    const capabilities = window.feedBack.capabilities;
    if (!capabilities || capabilities.version !== 1) return;
    if (window.feedBack.gamepadInput && window.feedBack.gamepadInput.version === 1) return;

    const AXIS_EPSILON = 0.02;      // axis changes smaller than this are ignored

    // controllerKey → claim { owner, id, key, index, connected, handle, listeners:Set, buttons:[{pressed,value}], axes:[] }
    const claims = new Map();
    const changeListeners = new Set();
    let rafPending = false;

    function _emit(name, detail) {
        try { capabilities.emitEvent('gamepad-input', name, detail || {}); }
        catch (_) { /* eventing must not break input */ }
    }

    function _now() {
        try { return window.performance.now() / 1000; } catch (_) { return 0; }
    }

    function _pads() {
        const nav = window.navigator;
        if (!nav || typeof nav.getGamepads !== 'function') return [];
        try { return Array.from(nav.getGamepads() || []); } catch (_) { return []; }
    }

    function _parseVendorProduct(id) {
        const m = /Vendor:\s*([0-9a-f]{4})\s+Product:\s*([0-9a-f]{4})/i.exec(String(id || ''));
        return m ? { vendor: m[1].toLowerCase(), product: m[2].toLowerCase() } : { vendor: null, product: null };
    }

    function _live(p) { return !!(p && p.connected); }

    // controllerKey for a new claim: `id#n`, the nth identical (same-id)
    // controller in slot order. If a claim already holds that key (its
    // identical twin unplugged and the slots renumbered), take the next free n.
    function _keyFor(pads, index) {
        const id = pads[index].id;
        let n = 0;
        for (const p of pads) {
            if (_live(p) && p.id === id) n += 1;
            if (p && p.index === index) break;
        }
        while (claims.has(`${id}#${n}`)) n += 1;
        return `${id}#${n}`;
    }

    function _snapshotState(p) {
        return {
            buttons: Array.from(p.buttons || [], (b) => ({ pressed: !!(b && b.pressed), value: _buttonValue(b) })),
            axes: Array.from(p.axes || [], (v) => Number(v) || 0),
        };
    }

    function _buttonValue(b) {
        if (!b) return 0;
        const v = Number(b.value);
        return Number.isFinite(v) ? v : (b.pressed ? 1 : 0);
    }

    function _fireChange(type, index) {
        const ev = { type, index };
        for (const fn of Array.from(changeListeners)) {
            try { fn(ev); } catch (_) { /* listener isolation */ }
        }
        _emit('change', ev);
    }

    // The claim bound to a slot: one whose controller is there, or a
    // disconnected one whose slot no other controller has taken.
    function _claimAt(index, pads) {
        const p = pads[index];
        for (const c of claims.values()) {
            if (c.index !== index) continue;
            if (!_live(p) || p.id === c.id) return c;
        }
        return null;
    }

    // Bring every claim's slot and connected flag in line with the pads. A
    // claim keeps its slot while the same controller (same id) is there;
    // otherwise it moves to a free slot holding an identical controller
    // (preferring its own slot), else it goes silent until one appears.
    // Returns the [type, index] change events to announce.
    function _reconcile(pads) {
        const taken = new Set();
        const unbound = [];
        const changes = [];
        for (const c of claims.values()) {
            const p = pads[c.index];
            if (_live(p) && p.id === c.id && !taken.has(c.index)) {
                taken.add(c.index);
                if (!c.connected) { c.connected = true; changes.push(['connect', c.index]); }
            } else {
                unbound.push(c);
            }
        }
        for (const c of unbound) {
            const p = pads.find((q) => _live(q) && q.id === c.id && !taken.has(q.index));
            if (p) {
                taken.add(p.index);
                c.index = p.index;
                c.handle.index = p.index;
                if (!c.connected) { c.connected = true; changes.push(['connect', p.index]); }
            } else if (c.connected) {
                c.connected = false;
                changes.push(['disconnect', c.index]);
            }
        }
        return changes;
    }

    function _poll() {
        rafPending = false;
        if (claims.size === 0) return;
        const pads = _pads();
        for (const [type, index] of _reconcile(pads)) _fireChange(type, index);
        const t = _now();
        for (const c of claims.values()) {
            if (!c.connected) continue;
            const p = pads[c.index];
            if (!p) continue;
            const cur = _snapshotState(p);
            const events = [];
            cur.buttons.forEach((b, i) => {
                const prev = c.buttons[i] || { pressed: false, value: 0 };
                if (b.pressed !== prev.pressed || Math.abs(b.value - prev.value) >= AXIS_EPSILON) {
                    c.buttons[i] = b;
                    events.push({ type: 'button', index: i, value: b.value, pressed: b.pressed, t });
                }
            });
            cur.axes.forEach((v, i) => {
                const prev = c.axes[i] || 0;
                if (Math.abs(v - prev) >= AXIS_EPSILON) {
                    c.axes[i] = v;
                    events.push({ type: 'axis', index: i, value: v, t });
                }
            });
            for (const ev of events) {
                for (const fn of Array.from(c.listeners)) {
                    try { fn(ev); } catch (_) { /* listener isolation */ }
                }
            }
        }
        _schedule();
    }

    function _schedule() {
        if (rafPending || claims.size === 0) return;
        const raf = window.requestAnimationFrame;
        if (typeof raf !== 'function') return;
        rafPending = true;
        raf.call(window, _poll);
    }

    // A browser connect/disconnect event: announce it once, plus any claim
    // that moved the other way in the same reconcile.
    function _onConnection(type, e) {
        const index = e && e.gamepad && Number.isInteger(e.gamepad.index) ? e.gamepad.index : null;
        const changes = claims.size ? _reconcile(_pads()) : [];
        _fireChange(type, index);
        for (const [t, i] of changes) if (t !== type) _fireChange(t, i);
    }

    try {
        window.addEventListener('gamepadconnected', (e) => _onConnection('connect', e));
        window.addEventListener('gamepaddisconnected', (e) => _onConnection('disconnect', e));
    } catch (_) { /* no window events: polling still reconciles */ }

    // ── public surface ──────────────────────────────────────────────────────
    function list() {
        const pads = _pads();
        const out = [];
        for (const p of pads) {
            if (!p) continue;
            const { vendor, product } = _parseVendorProduct(p.id);
            const c = _claimAt(p.index, pads);
            out.push({
                index: p.index,
                id: p.id,
                mapping: p.mapping,
                vendor,
                product,
                connected: !!p.connected,
                claimedBy: c ? c.owner : null,
            });
        }
        return out;
    }

    function claim(index, ownerId) {
        const owner = String(ownerId || '').trim();
        if (!owner) return null;
        const pads = _pads();
        const held = _claimAt(index, pads);
        if (held) return held.owner === owner ? held.handle : null;
        const p = pads[index];
        if (!_live(p)) return null;
        const key = _keyFor(pads, index);
        const state = _snapshotState(p);
        const c = { owner, id: p.id, key, index, connected: true, listeners: new Set(), buttons: state.buttons, axes: state.axes, handle: null };
        c.handle = {
            index,
            id: p.id,
            controllerKey: key,
            addListener(fn) { if (typeof fn === 'function') c.listeners.add(fn); },
            removeListener(fn) { c.listeners.delete(fn); },
            release() {
                if (claims.get(key) !== c) return;
                claims.delete(key);
                c.listeners.clear();
                _fireChange('release', c.index);
            },
        };
        claims.set(key, c);
        _fireChange('claim', index);
        _schedule();
        return c.handle;
    }

    function isClaimed(index) {
        return !!_claimAt(index, _pads());
    }

    function on(name, fn) {
        if (name !== 'change' || typeof fn !== 'function') return () => {};
        changeListeners.add(fn);
        return () => { changeListeners.delete(fn); };
    }

    function _snapshot() {
        return {
            available: typeof (window.navigator && window.navigator.getGamepads) === 'function',
            controllers: list(),
            claims: Array.from(claims.values(), (c) => ({ index: c.index, owner: c.owner, connected: c.connected })),
            polling: rafPending,
        };
    }

    capabilities.registerOwner('gamepad-input', {
        pluginId: 'core.gamepad-input',
        kind: 'provider-coordinator',
        safety: 'sensitive',
        commands: ['inspect', 'list-sources'],
        operations: ['source.enumerate', 'source.describe'],
        events: ['change'],
        description: 'Core-owned gamepad control plane: one owner claims a controller and receives raw button/axis changes in-page. Commands are read-only snapshots.',
        handlers: {
            inspect: () => ({ outcome: 'handled', payload: _snapshot() }),
            'list-sources': () => ({ outcome: 'handled', payload: _snapshot() }),
        },
    });

    window.feedBack.gamepadInput = {
        version: 1,
        list,
        claim,
        isClaimed,
        claimCount: () => claims.size,
        on,
    };
})();

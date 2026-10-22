// Core gamepad-input capability domain (spec 014).
//
// Lets a plugin claim one controller (a plastic guitar, a plastic drum kit, an
// Xbox pad) and receive its raw button/axis CHANGES while it plays it as an
// instrument. Core knows nothing about frets or pads: a consumer names the
// inputs through its own profiles. Core-owned (`core.gamepad-input`) for the
// same reason `midi-input` is: an input plane outlives any one feature.
//
// One owner per controller. Polling runs on requestAnimationFrame only while
// at least one claim or watcher exists, and its steady state allocates nothing.
// `watch(fn)` is a passive listener on controllers nobody has claimed (any
// mapping): it hears changes without taking a controller, so core's menus keep
// working. A watcher is not a claim and does not move `claimCount()`. A claimed
// controller that unplugs keeps its handle (silent). When a controller with the
// same `id` is back (its old slot preferred, else any free slot holding an
// identical controller), events resume on that handle. `claimCount()` lets
// core's menu gamepad support stand down while any controller is being played.
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
    const NO_PADS = Object.freeze([]);

    // claim { owner, id, key, index, connected, handle, listeners:[fn],
    //         pressed:[bool], values:[number], axes:[number] }
    // `claims` is for key lookup; `claimList` is iterated on the hot path
    // (indexed loops, no iterator allocation).
    const claims = new Map();
    let claimList = [];
    // Listener arrays are replaced, never mutated (copy-on-write), so dispatch
    // iterates them in place and a listener that adds/removes mid-dispatch
    // cannot disturb the loop.
    let changeListeners = [];
    // Passive watchers (copy-on-write, like the other listener arrays) and the
    // per-slot last-seen state they diff against: slot → { id, base, pressed,
    // values, axes }. The arrays are reused, so steady-state polling allocates
    // nothing; a slot's state is dropped when its pad leaves or watching stops.
    let watchers = [];
    const watchState = new Map();
    // slot → change type a poll tick already announced, so the browser's own
    // connect/disconnect event for the same transition is not announced twice.
    const pollAnnounced = new Map();
    let rafPending = false;
    let reconcileCount = 0;

    function _emit(name, detail) {
        try { capabilities.emitEvent('gamepad-input', name, detail || {}); }
        catch (_) { /* eventing must not break input */ }
    }

    function _now() {
        try { return window.performance.now() / 1000; } catch (_) { return 0; }
    }

    // The browser's gamepad list, indexed directly (not copied).
    function _pads() {
        const nav = window.navigator;
        if (!nav || typeof nav.getGamepads !== 'function') return NO_PADS;
        try { return nav.getGamepads() || NO_PADS; } catch (_) { return NO_PADS; }
    }

    function _live(p) { return !!(p && p.connected); }

    function _parseVendorProduct(id) {
        const m = /Vendor:\s*([0-9a-f]{4})\s+Product:\s*([0-9a-f]{4})/i.exec(String(id || ''));
        return m ? { vendor: m[1].toLowerCase(), product: m[2].toLowerCase() } : { vendor: null, product: null };
    }

    function _buttonValue(b) {
        if (!b) return 0;
        const v = Number(b.value);
        return Number.isFinite(v) ? v : (b.pressed ? 1 : 0);
    }

    // controllerKey for a new claim: `id#n`, the nth identical (same-id)
    // controller in slot order. If a claim already holds that key (its
    // identical twin unplugged and the slots renumbered), take the next free n.
    function _keyFor(pads, index) {
        const id = pads[index].id;
        let n = 0;
        for (let i = 0; i <= index && i < pads.length; i++) {
            if (_live(pads[i]) && pads[i].id === id) n += 1;
        }
        while (claims.has(`${id}#${n}`)) n += 1;
        return `${id}#${n}`;
    }

    function _call(listeners, ev) {
        for (let i = 0; i < listeners.length; i++) {
            try { listeners[i](ev); } catch (_) { /* listener isolation */ }
        }
    }

    function _fireChange(type, index) {
        const ev = { type, index };
        _call(changeListeners, ev);
        _emit('change', ev);
    }

    // The claim bound to a slot: one whose controller is there, or a
    // disconnected one whose slot no other controller has taken.
    function _claimAt(index, pads) {
        const p = pads[index];
        for (let i = 0; i < claimList.length; i++) {
            const c = claimList[i];
            if (c.index !== index) continue;
            if (!_live(p) || p.id === c.id) return c;
        }
        return null;
    }

    // True when every claim is connected and its controller is still in its
    // slot — the steady state, checked without allocating.
    function _allInPlace(pads) {
        for (let i = 0; i < claimList.length; i++) {
            const c = claimList[i];
            const p = pads[c.index];
            if (!c.connected || !_live(p) || p.id !== c.id) return false;
        }
        return true;
    }

    // Bring every claim's slot and connected flag in line with the pads. A
    // claim keeps its slot while a controller with its `id` is there;
    // otherwise it moves to a free slot holding an identical controller
    // (preferring its own slot), else it goes silent until one appears.
    // Returns the [type, index] change events to announce.
    function _reconcile(pads) {
        reconcileCount += 1;
        const taken = new Set();
        const unbound = [];
        const changes = [];
        for (const c of claimList) {
            const p = pads[c.index];
            if (_live(p) && p.id === c.id && !taken.has(c.index)) {
                taken.add(c.index);
                if (!c.connected) { c.connected = true; changes.push(['connect', c.index]); }
            } else {
                unbound.push(c);
            }
        }
        for (const c of unbound) {
            let found = -1;
            for (let i = 0; i < pads.length; i++) {
                if (_live(pads[i]) && pads[i].id === c.id && !taken.has(i)) { found = i; break; }
            }
            if (found >= 0) {
                taken.add(found);
                c.index = found;
                c.handle.index = found;
                if (!c.connected) { c.connected = true; changes.push(['connect', found]); }
            } else if (c.connected) {
                c.connected = false;
                changes.push(['disconnect', c.index]);
            }
        }
        return changes;
    }

    // Diff one controller against the claim's last reported state, in place.
    // Allocates only an event object, and only when something changed.
    function _diff(c, p, t) {
        const buttons = p.buttons || NO_PADS;
        for (let i = 0; i < buttons.length; i++) {
            const b = buttons[i];
            const pressed = !!(b && b.pressed);
            const value = _buttonValue(b);
            const prevValue = c.values[i] || 0;
            if (pressed !== !!c.pressed[i] || Math.abs(value - prevValue) >= AXIS_EPSILON) {
                c.pressed[i] = pressed;
                c.values[i] = value;
                if (c.listeners.length) _call(c.listeners, { type: 'button', index: i, value, pressed, t });
            }
        }
        const axes = p.axes || NO_PADS;
        for (let i = 0; i < axes.length; i++) {
            const v = Number(axes[i]) || 0;
            if (Math.abs(v - (c.axes[i] || 0)) >= AXIS_EPSILON) {
                c.axes[i] = v;
                if (c.listeners.length) _call(c.listeners, { type: 'axis', index: i, value: v, t });
            }
        }
    }

    function _polling() { return claimList.length > 0 || watchers.length > 0; }

    // Record a pad's current state as the watch baseline (nothing fires).
    function _watchBaseline(st, p) {
        st.id = p.id;
        st.base = true;
        const buttons = p.buttons || NO_PADS;
        for (let i = 0; i < buttons.length; i++) {
            st.pressed[i] = !!(buttons[i] && buttons[i].pressed);
            st.values[i] = _buttonValue(buttons[i]);
        }
        const axes = p.axes || NO_PADS;
        for (let i = 0; i < axes.length; i++) st.axes[i] = Number(axes[i]) || 0;
    }

    // Diff one unclaimed pad against its watch state, in place.
    function _watchDiff(st, index, p, t) {
        const buttons = p.buttons || NO_PADS;
        for (let i = 0; i < buttons.length; i++) {
            const b = buttons[i];
            const pressed = !!(b && b.pressed);
            const value = _buttonValue(b);
            if (pressed !== !!st.pressed[i] || Math.abs(value - (st.values[i] || 0)) >= AXIS_EPSILON) {
                st.pressed[i] = pressed;
                st.values[i] = value;
                _call(watchers, { pad: index, type: 'button', index: i, value, pressed, t });
            }
        }
        const axes = p.axes || NO_PADS;
        for (let i = 0; i < axes.length; i++) {
            const v = Number(axes[i]) || 0;
            if (Math.abs(v - (st.axes[i] || 0)) >= AXIS_EPSILON) {
                st.axes[i] = v;
                _call(watchers, { pad: index, type: 'axis', index: i, value: v, t });
            }
        }
    }

    function _watchPads(pads, t) {
        for (let i = 0; i < pads.length; i++) {
            const p = pads[i];
            let st = watchState.get(i);
            if (!_live(p)) { if (st) st.base = false; continue; }
            if (_claimAt(i, pads)) { if (st) st.base = false; continue; }
            if (!st) {
                st = { id: null, base: false, pressed: [], values: [], axes: [] };
                watchState.set(i, st);
            }
            // New pad, a different pad in the slot, or just released: baseline.
            if (!st.base || st.id !== p.id) { _watchBaseline(st, p); continue; }
            _watchDiff(st, i, p, t);
        }
    }

    function _poll() {
        rafPending = false;
        if (!_polling()) return;
        const pads = _pads();
        if (!_allInPlace(pads)) {
            const changes = _reconcile(pads);
            for (const [type, index] of changes) {
                pollAnnounced.set(index, type);
                _fireChange(type, index);
            }
        }
        const t = _now();
        const list = claimList;
        for (let i = 0; i < list.length; i++) {
            const c = list[i];
            if (!c.connected) continue;
            const p = pads[c.index];
            if (_live(p)) _diff(c, p, t);
        }
        if (watchers.length) _watchPads(pads, t);
        _schedule();
    }

    function _schedule() {
        if (rafPending || !_polling()) return;
        const raf = window.requestAnimationFrame;
        if (typeof raf !== 'function') return;
        rafPending = true;
        raf.call(window, _poll);
    }

    // A browser connect/disconnect event: announce it once (unless a poll tick
    // already announced the same transition for that slot), plus any other
    // claim change the same reconcile turns up.
    function _onConnection(type, e) {
        const index = e && e.gamepad && Number.isInteger(e.gamepad.index) ? e.gamepad.index : null;
        const changes = claimList.length ? _reconcile(_pads()) : [];
        const already = index !== null && pollAnnounced.get(index) === type;
        if (index !== null) pollAnnounced.delete(index);
        if (!already) _fireChange(type, index);
        for (const [t, i] of changes) {
            if (t === type && i === index) continue;
            _fireChange(t, i);
        }
    }

    try {
        window.addEventListener('gamepadconnected', (e) => _onConnection('connect', e));
        window.addEventListener('gamepaddisconnected', (e) => _onConnection('disconnect', e));
    } catch (_) { /* no window events: polling still reconciles */ }

    // ── public surface ──────────────────────────────────────────────────────
    function list() {
        const pads = _pads();
        const out = [];
        for (let i = 0; i < pads.length; i++) {
            const p = pads[i];
            if (!p) continue;
            const { vendor, product } = _parseVendorProduct(p.id);
            const c = _claimAt(i, pads);
            out.push({
                index: i,
                id: p.id,
                mapping: p.mapping,
                vendor,
                product,
                connected: !!p.connected,
                claimedBy: c ? c.owner : null,
                // The claim's key, else what claim(index) would assign now.
                controllerKey: c ? c.key : (_live(p) ? _keyFor(pads, i) : null),
            });
        }
        return out;
    }

    function claim(index, ownerId) {
        if (!Number.isInteger(index) || index < 0) return null;
        const owner = String(ownerId || '').trim();
        if (!owner) return null;
        const pads = _pads();
        const held = _claimAt(index, pads);
        if (held) return held.owner === owner ? held.handle : null;
        const p = pads[index];
        if (!_live(p)) return null;
        const key = _keyFor(pads, index);
        const c = {
            owner, id: p.id, key, index, connected: true, handle: null, listeners: [],
            pressed: [], values: [], axes: [],
        };
        // Baseline: the controller's state now, so inputs held at claim time don't fire.
        const buttons = p.buttons || NO_PADS;
        for (let i = 0; i < buttons.length; i++) {
            c.pressed[i] = !!(buttons[i] && buttons[i].pressed);
            c.values[i] = _buttonValue(buttons[i]);
        }
        const axes = p.axes || NO_PADS;
        for (let i = 0; i < axes.length; i++) c.axes[i] = Number(axes[i]) || 0;
        c.handle = {
            index,
            id: p.id,
            controllerKey: key,
            addListener(fn) {
                if (typeof fn === 'function' && !c.listeners.includes(fn)) c.listeners = c.listeners.concat(fn);
            },
            removeListener(fn) { c.listeners = c.listeners.filter((l) => l !== fn); },
            release() {
                if (claims.get(key) !== c) return;
                claims.delete(key);
                claimList = claimList.filter((x) => x !== c);
                c.listeners = [];
                _fireChange('release', c.index);
            },
        };
        claims.set(key, c);
        claimList = claimList.concat(c);
        _fireChange('claim', index);
        _schedule();
        return c.handle;
    }

    // Passive listener for changes on connected, unclaimed controllers (any
    // mapping). Baselines are taken now, when a pad connects and when its claim
    // is released, so a held input never fires. Returns an unsubscribe.
    function watch(fn) {
        if (typeof fn !== 'function') return () => {};
        if (watchers.length === 0) {
            const pads = _pads();
            for (let i = 0; i < pads.length; i++) {
                const p = pads[i];
                if (!_live(p) || _claimAt(i, pads)) continue;
                const st = { id: null, base: false, pressed: [], values: [], axes: [] };
                _watchBaseline(st, p);
                watchState.set(i, st);
            }
        }
        watchers = watchers.concat(fn);
        _schedule();
        return () => {
            watchers = watchers.filter((l) => l !== fn);
            if (watchers.length === 0) watchState.clear();
        };
    }

    function isClaimed(index) {
        return !!_claimAt(index, _pads());
    }

    function on(name, fn) {
        if (name !== 'change' || typeof fn !== 'function') return () => {};
        changeListeners = changeListeners.concat(fn);
        return () => { changeListeners = changeListeners.filter((l) => l !== fn); };
    }

    function _snapshot() {
        return {
            available: typeof (window.navigator && window.navigator.getGamepads) === 'function',
            controllers: list(),
            claims: claimList.map((c) => ({ index: c.index, owner: c.owner, connected: c.connected })),
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
        claimCount: () => claimList.length,
        watch,
        on,
        // Test/diagnostic hook: how many times the slow reconcile path ran.
        _reconcileCount: () => reconcileCount,
    };
})();

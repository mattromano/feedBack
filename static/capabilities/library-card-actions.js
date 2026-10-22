/*
 * ui.library-card-injection — core capability for plugin-contributed library
 * card actions (fee[dB]ack v0.3.0).
 *
 * Replaces the legacy pattern where plugins (Sloppak Converter, Find More,
 * editor) injected buttons by DOM-observing `.song-card`. Plugins now REGISTER
 * card actions with placement, applicability, enabled state, and a run handler;
 * the library renders + dispatches them and emits action-result events. The
 * owner is registered in the capability runtime so the Capability Inspector and
 * diagnostics can see it (design/05; docs/capability-roadmap.md domain #9).
 *
 * Public API: window.feedBack.libraryCardActions
 *   register(spec) -> unregister()    spec: { id, pluginId, label, icon?,
 *       placement?('menu'|'inline'|'overlay'|'play-as'), order?, destructive?,
 *       group?, hint?, variants?(song)->[{ key, label, hint?, enabled? }],
 *       applies?(song)->bool, enabled?(song)->bool, run(song, ctx) }
 *   list(song) -> [actionSummary]      applicable actions, sorted
 *   run(id, song, ctx) -> Promise<{ ok, outcome, result?|error? }>
 *
 * 'play-as' actions are the ways to play a song (the Songs table's "Play as"
 * menu, the card ⋮ "Play as…"; static/v3/play-as.js): `group` heads a section
 * of the menu ("Real instrument", "Controller", ...), `hint` is a short note
 * beside the label. An action with `variants` shows one row per variant (e.g.
 * one per arrangement); run() then gets the chosen one as ctx.variant and
 * refuses a key the song doesn't offer. An action whose variants() returns
 * nothing is not listed.
 *   snapshot() -> redaction-safe registry snapshot
 */
(function () {
    'use strict';
    const w = (typeof window !== 'undefined') ? window : {};
    w.feedBack = w.feedBack || {};
    if (w.feedBack.libraryCardActions && w.feedBack.libraryCardActions.version === 1) return;
    const capabilities = w.feedBack.capabilities;
    const DOMAIN = 'ui.library-card-injection';
    const PLACEMENTS = ['menu', 'inline', 'overlay', 'play-as'];

    const actions = new Map(); // id -> normalized spec

    function _emit(event, payload) {
        if (capabilities && typeof capabilities.emitEvent === 'function') {
            try { capabilities.emitEvent(DOMAIN, event, payload); } catch (e) { /* */ }
        }
    }

    function _normalize(spec) {
        if (!spec || !spec.id || typeof spec.run !== 'function') return null;
        return {
            id: String(spec.id),
            pluginId: String(spec.pluginId || 'unknown'),
            label: String(spec.label || spec.id),
            icon: spec.icon || '',
            placement: PLACEMENTS.includes(spec.placement) ? spec.placement : 'menu',
            order: Number.isFinite(spec.order) ? spec.order : 100,
            destructive: !!spec.destructive,
            group: typeof spec.group === 'string' ? spec.group : '',
            hint: typeof spec.hint === 'string' ? spec.hint : '',
            variants: typeof spec.variants === 'function' ? spec.variants : null,
            applies: typeof spec.applies === 'function' ? spec.applies : () => true,
            enabled: typeof spec.enabled === 'function' ? spec.enabled : () => true,
            run: spec.run,
        };
    }

    function register(spec) {
        const a = _normalize(spec);
        if (!a) { return () => {}; }
        // Reject ID collisions: silently overwriting would make unregister()/
        // run() nondeterministic once two providers share an action id.
        if (actions.has(a.id)) {
            if (typeof console !== 'undefined' && console.warn) {
                console.warn('[library-card-actions] duplicate action id ignored:', a.id);
            }
            return () => {};
        }
        actions.set(a.id, a);
        _emit('action-registered', { id: a.id, pluginId: a.pluginId, placement: a.placement });
        return function unregister() {
            if (actions.delete(a.id)) _emit('action-unregistered', { id: a.id, pluginId: a.pluginId });
        };
    }

    function unregister(id) {
        // Normalize the id once (so the lookup, delete, and event payload all
        // agree for non-string ids) and capture the action before deleting, so
        // the action-unregistered payload always carries a defined pluginId —
        // matching the register()-returned unregister path.
        const key = String(id);
        const a = actions.get(key);
        if (!actions.delete(key)) return;
        _emit('action-unregistered', { id: key, pluginId: a ? a.pluginId : undefined });
    }

    // An action's variants for a song: [{ key, label, hint, enabled }] (keys
    // unique strings), or null for an action without variants. A throwing
    // variants() offers nothing.
    function _variants(a, song) {
        if (!a.variants) return null;
        let raw;
        try { raw = a.variants(song); } catch (e) { raw = null; }
        const out = [];
        const seen = new Set();
        for (const v of Array.isArray(raw) ? raw : []) {
            if (!v || v.key == null) continue;
            const key = String(v.key);
            if (seen.has(key)) continue;
            seen.add(key);
            out.push({ key, label: String(v.label || key), hint: typeof v.hint === 'string' ? v.hint : '', enabled: v.enabled !== false });
        }
        return out;
    }

    function list(song) {
        const out = [];
        for (const a of actions.values()) {
            let applicable = true;
            try { applicable = a.applies(song) !== false; } catch (e) { applicable = false; }
            if (!applicable) continue;
            const variants = _variants(a, song);
            if (variants && !variants.length) continue;
            let enabled = true;
            try { enabled = a.enabled(song) !== false; } catch (e) { enabled = true; }
            const item = { id: a.id, pluginId: a.pluginId, label: a.label, icon: a.icon, placement: a.placement, order: a.order, destructive: a.destructive, enabled };
            if (a.group) item.group = a.group;
            if (a.hint) item.hint = a.hint;
            if (variants) item.variants = variants;
            out.push(item);
        }
        out.sort((x, y) => (x.order - y.order) || x.label.localeCompare(y.label));
        return out;
    }

    async function run(id, song, ctx) {
        const a = actions.get(String(id));
        if (!a) return { ok: false, outcome: 'no-action' };
        // Mirror list()'s defensive semantics for the predicate checks: an
        // applies() error means not-applicable, an enabled() error is non-fatal
        // (treat as enabled). Only a.run() throwing is a 'failed' outcome.
        let applicable = true;
        try { applicable = a.applies(song) !== false; } catch (e) { applicable = false; }
        if (!applicable) return { ok: false, outcome: 'not-applicable' };
        let enabled = true;
        try { enabled = a.enabled(song) !== false; } catch (e) { enabled = true; }
        if (!enabled) return { ok: false, outcome: 'disabled' };
        if (a.variants) {
            const want = ctx && ctx.variant != null ? String(ctx.variant) : null;
            const v = (_variants(a, song) || []).find((x) => x.key === want);
            if (!v) return { ok: false, outcome: 'not-applicable' };
            if (!v.enabled) return { ok: false, outcome: 'disabled' };
        }
        try {
            const result = await a.run(song, ctx || {});
            _emit('action-result', { id: a.id, pluginId: a.pluginId, outcome: 'handled' });
            return { ok: true, outcome: 'handled', result };
        } catch (e) {
            _emit('action-result', { id: a.id, pluginId: a.pluginId, outcome: 'failed', reason: e && e.message ? e.message : String(e) });
            return { ok: false, outcome: 'failed', error: e && e.message ? e.message : String(e) };
        }
    }

    function snapshot() {
        return {
            schema: 'feedBack.library_card_actions.v1',
            actions: Array.from(actions.values()).map((a) => ({ id: a.id, pluginId: a.pluginId, placement: a.placement, order: a.order, destructive: a.destructive })),
        };
    }

    // Register the capability owner (Inspector/diagnostics visibility). The
    // ergonomic register/list/run API above is what the library + plugins use;
    // these commands mirror it for inspection.
    if (capabilities && typeof capabilities.registerOwner === 'function') {
        try {
            capabilities.registerOwner(DOMAIN, {
                pluginId: 'core.ui.library-card-injection',
                kind: 'provider-coordinator',
                commands: ['list', 'run', 'inspect'],
                operations: ['action.run'],
                events: ['action-registered', 'action-unregistered', 'action-result'],
                ownership: 'multi-provider',
                safety: 'safe',
                version: 1,
                description: 'Coordinates plugin-contributed library-card actions (placement, applicability, enabled state, action-result events).',
                handlers: {
                    list: (ctx) => ({ outcome: 'handled', payload: list(ctx && ctx.payload && ctx.payload.song) }),
                    run: (ctx) => run(((ctx && ctx.payload) || {}).id, ((ctx && ctx.payload) || {}).song, ctx).then((r) => ({ outcome: r.ok ? 'handled' : (r.outcome === 'failed' ? 'failed' : 'denied'), payload: r })),
                    inspect: () => ({ outcome: 'handled', payload: snapshot() }),
                },
            });
        } catch (e) { /* owner registration is best-effort */ }
    }

    const api = { version: 1, register, unregister, list, run, snapshot };
    if (typeof module !== 'undefined' && module.exports) module.exports = api;  // node tests
    w.feedBack.libraryCardActions = api;
})();

// The A/V offset (picture vs sound) is one calibration per machine, so every
// highway draws with it: the main `window.highway` AND every highway a plugin
// builds with window.createHighway() (splitscreen panels, one per player).
//
// Before this, setAvOffsetMs() only reached window.highway; panel highways
// stayed at 0 ms, so in split view the gems — and Note Detection, which judges
// on the highway's render clock — ran on a different clock than in single
// player.
//
// createHighway() registers each instance here (seeded with the current
// value); setAvOffsetMs() calls setAllHighwaysAvOffset(). Instances are held
// weakly, so a panel that is dropped is simply forgotten. A plugin that wants
// its own offset on one highway can still call highway.setAvOffset(ms) — it
// holds until the next global change.

const _live = new Set();   // WeakRef<highway api>
let _sharedMs = 0;

export function sharedAvOffsetMs() {
    return _sharedMs;
}

export function registerHighwayAvOffset(api) {
    if (!api || typeof api.setAvOffset !== 'function') return;
    try { api.setAvOffset(_sharedMs); } catch (_) { /* never block highway creation */ }
    _live.add(new WeakRef(api));
}

export function setAllHighwaysAvOffset(ms) {
    const n = Number(ms);
    _sharedMs = Number.isFinite(n) ? n : 0;
    for (const ref of [..._live]) {
        const hw = ref.deref();
        if (!hw) { _live.delete(ref); continue; }
        try { hw.setAvOffset(_sharedMs); } catch (_) { /* a torn-down panel must not stop the rest */ }
    }
}

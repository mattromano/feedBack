// Display time: the single clock the highway (and any panel or lane that
// mirrors it) should draw from. Normally it is the audio clock, but while the
// A-B loop count-in rewind sweep runs, the chart is scrubbed back to loop A
// ahead of the audio, so the sweep time takes over until the sweep ends.

let _sweepT = null; // null when no sweep is active

export function setSweepTime(t) {
    if (typeof t === 'number' && Number.isFinite(t)) _sweepT = t;
}

export function clearSweepTime() {
    _sweepT = null;
}

export function getDisplayTime(audioTimeFn) {
    return _sweepT !== null ? _sweepT : audioTimeFn();
}

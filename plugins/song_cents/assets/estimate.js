// Song Cents: tuning-offset estimator (how many cents a recording sits off A440 / the chart's
// tuning). Shared by the plugin (browser) and tools/song_cents_check.mjs (Node).
//
// Method: Hann-windowed FFT frames spread across the song; in each frame take the strongest
// spectral peaks between 70 and 1000 Hz (fundamentals and low harmonics, below where string
// inharmonicity drags partials sharp), refine each to a sub-bin frequency with a parabola on
// log magnitude, and fold its deviation from the nearest 12-TET note into [-50, 50) cents.
// A magnitude-weighted circular mean of those deviations is the offset; the resultant length
// (0..1) is the confidence. A residual near +/-50 is inherently ambiguous (quarter-tone).
(function (root) {
    'use strict';

    function fft(re, im) {
        const n = re.length;
        for (let i = 1, j = 0; i < n; i++) {
            let bit = n >> 1;
            for (; j & bit; bit >>= 1) j ^= bit;
            j ^= bit;
            if (i < j) {
                let t = re[i]; re[i] = re[j]; re[j] = t;
                t = im[i]; im[i] = im[j]; im[j] = t;
            }
        }
        for (let len = 2; len <= n; len <<= 1) {
            const ang = -2 * Math.PI / len;
            const wr = Math.cos(ang), wi = Math.sin(ang);
            const half = len >> 1;
            for (let i = 0; i < n; i += len) {
                let cr = 1, ci = 0;
                for (let k = 0; k < half; k++) {
                    const a = i + k, b = a + half;
                    const xr = re[b] * cr - im[b] * ci;
                    const xi = re[b] * ci + im[b] * cr;
                    re[b] = re[a] - xr; im[b] = im[a] - xi;
                    re[a] += xr; im[a] += xi;
                    const t = cr * wr - ci * wi;
                    ci = cr * wi + ci * wr;
                    cr = t;
                }
            }
        }
    }

    // samples: mono Float32Array; sampleRate in Hz. Returns { cents, confidence, peaks }.
    function estimateCents(samples, sampleRate, opts) {
        const o = Object.assign({ frames: 160, minHz: 70, maxHz: 1000, peaksPerFrame: 12, refHz: 440 }, opts || {});
        let n = 1;
        while (n < sampleRate * 0.35) n <<= 1;              // ~0.37 s frames: ~2.7 Hz bins at 44.1 kHz
        if (samples.length < n * 4) return { cents: 0, confidence: 0, peaks: 0 };
        const win = new Float32Array(n);
        for (let i = 0; i < n; i++) win[i] = 0.5 - 0.5 * Math.cos(2 * Math.PI * i / (n - 1));
        const re = new Float64Array(n), im = new Float64Array(n), mag = new Float64Array(n / 2);
        const kLo = Math.max(2, Math.floor(o.minHz * n / sampleRate));
        const kHi = Math.min(n / 2 - 2, Math.ceil(o.maxHz * n / sampleRate));
        // Skip the first/last 8% (count-ins, fades, applause).
        const start = Math.floor(samples.length * 0.08);
        const span = Math.floor(samples.length * 0.84) - n;
        let C = 0, S = 0, W = 0, peaks = 0;
        const cand = [];
        for (let f = 0; f < o.frames; f++) {
            const off = start + Math.floor(span * f / Math.max(1, o.frames - 1));
            let energy = 0;
            for (let i = 0; i < n; i++) {
                const v = samples[off + i] || 0;
                re[i] = v * win[i]; im[i] = 0;
                energy += v * v;
            }
            if (energy / n < 1e-7) continue;                  // silence
            fft(re, im);
            let frameMax = 0;
            for (let k = kLo - 1; k <= kHi + 1; k++) {
                mag[k] = Math.hypot(re[k], im[k]);
                if (mag[k] > frameMax) frameMax = mag[k];
            }
            cand.length = 0;
            const floor = frameMax * 0.05;                     // within ~26 dB of the frame's peak
            for (let k = kLo; k <= kHi; k++) {
                const m = mag[k];
                if (m > floor && m > mag[k - 1] && m >= mag[k + 1]) cand.push(k);
            }
            cand.sort((a, b) => mag[b] - mag[a]);
            for (let c = 0; c < Math.min(o.peaksPerFrame, cand.length); c++) {
                const k = cand[c];
                const a = Math.log(mag[k - 1] + 1e-12), b = Math.log(mag[k] + 1e-12), g = Math.log(mag[k + 1] + 1e-12);
                const den = a - 2 * b + g;
                const d = den < 0 ? 0.5 * (a - g) / den : 0;
                const hz = (k + d) * sampleRate / n;
                const cents = 1200 * Math.log2(hz / o.refHz);
                const r = cents - 100 * Math.round(cents / 100);
                const th = 2 * Math.PI * r / 100;
                const w = mag[k];
                C += w * Math.cos(th); S += w * Math.sin(th); W += w;
                peaks++;
            }
        }
        if (!W) return { cents: 0, confidence: 0, peaks: 0 };
        let cents = Math.atan2(S, C) * 100 / (2 * Math.PI);
        if (cents >= 50) cents -= 100;
        return { cents, confidence: Math.hypot(C, S) / W, peaks };
    }

    const api = { estimateCents };
    if (typeof module !== 'undefined' && module.exports) module.exports = api;
    else root.__songCentsEstimate = api;
})(typeof window !== 'undefined' ? window : globalThis);

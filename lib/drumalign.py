"""Align a drum chart to a recording and check how well it fits.

mattromano custom build (ported from the Slopsmith 0.2.9 tools): used by lib/drumjoin.py,
scripts/drums_join.py and scripts/drums_library.py to put a YARG / Clone Hero drum chart, or
a Guitar Pro drum track, onto the user's own audio. Offline tooling only: needs librosa and
scipy (``pip install librosa``), which the app itself does not ship.

Envelopes are 3-band onset envelopes (low / mid / high spectral flux: kick, snare+toms,
cymbals), shape (3, frames).  Correlating the bands jointly is far more selective than a
single flux curve: a groove of identical-looking hits correlates almost as well one bar
off, but kick/snare/hat patterns don't.

* ``global_offset``: whole-song cross-correlation (the chart's own audio, or an envelope
  synthesised from its hits, against our drums stem) cross-checked by independent 12 s
  window votes, which survive drift that smears a whole-song correlation.
* ``local_lags``: windowed lags after the global shift, to spot drift (a different
  master/edit/tempo than the chart was synced to).
* ``warp_pairs``: when correlation is weak or drifts, a beat-level warp: a global tempo
  ratio (from the lag track's slope), dynamic-programming lag tracking over 4 s windows (a
  DTW over lag space), sampled at the chart's beats and refined per beat against the
  stem's onsets (the refine idea from gp_to_sloppak's beat-level sync).
* ``validate``: onset detection on stems/drums.ogg, then median offset, % of notes
  within +-30 ms and drift over time for the placed notes.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

SR = 22050
HOP = 128                      # ~5.8 ms envelope frames
FR = SR / HOP
BANDS_HZ = (0, 180, 3000, SR // 2)     # low (kick) / mid (snare, toms) / high (cymbals)


def load_audio(path, sr=SR) -> np.ndarray:
    import librosa
    try:
        y, _ = librosa.load(str(path), sr=sr, mono=True)
    except Exception:
        # libsndfile rejects some Ogg/Opus chart stems as "malformed" (seen with the Windows
        # wheel); ffmpeg decodes them fine.
        y = _ffmpeg_load(path, sr)
    return y.astype(np.float32)


def _ffmpeg_load(path, sr) -> np.ndarray:
    import subprocess
    try:
        import audio as _audio
        ff = _audio._ffmpeg_cmd() or "ffmpeg"
    except Exception:
        ff = "ffmpeg"
    r = subprocess.run([ff, "-v", "error", "-nostdin", "-i", str(path), "-ac", "1", "-ar", str(sr),
                        "-f", "f32le", "-"], capture_output=True, check=True)
    return np.frombuffer(r.stdout, dtype=np.float32)


def onset_env(y: np.ndarray, sr=SR, hop=HOP) -> np.ndarray:
    """(3, frames) band-wise onset strength (half-wave rectified log-mel flux), each band
    normalised by its 99th percentile."""
    import librosa
    if len(y) < hop * 8:
        return np.zeros((3, 8), dtype=np.float32)
    S = librosa.feature.melspectrogram(y=y, sr=sr, hop_length=hop, n_fft=1024, n_mels=64, fmin=20, fmax=sr // 2)
    S = librosa.power_to_db(S, ref=np.max)
    freqs = librosa.mel_frequencies(n_mels=64, fmin=20, fmax=sr // 2)
    flux = np.maximum(0.0, np.diff(S, axis=1, prepend=S[:, :1]))
    out = []
    for lo, hi in zip(BANDS_HZ[:-1], BANDS_HZ[1:]):
        sel = (freqs >= lo) & (freqs < hi)
        e = flux[sel].mean(axis=0)
        out.append(np.clip(e / (np.percentile(e, 99) + 1e-9), 0, 1.5))
    return np.array(out, dtype=np.float32)


def env1d(env) -> np.ndarray:
    env = np.asarray(env)
    return env if env.ndim == 1 else env.mean(axis=0)


PAD_BANDS = {"kick": (1.0, 0.0, 0.0), "red": (0.0, 1.0, 0.4), "tom": (0.5, 0.8, 0.0), "cymbal": (0.0, 0.1, 1.0)}


def synth_env(hits, n_frames: int, fr=FR) -> np.ndarray:
    """(3, frames) envelope synthesised from (time, pad, cymbal) hits (chart without audio)."""
    from scipy.ndimage import gaussian_filter1d
    env = np.zeros((3, n_frames), dtype=np.float32)
    for t, pad, cym in hits:
        k = int(round(t * fr))
        if not 0 <= k < n_frames:
            continue
        key = pad if pad in ("kick", "red") else ("cymbal" if cym else "tom")
        env[:, k] += PAD_BANDS[key]
    return gaussian_filter1d(env, 1.0, axis=1)


def _z(v):
    v = np.asarray(v, dtype=np.float64)
    return (v - v.mean(axis=-1, keepdims=True)) / (v.std(axis=-1, keepdims=True) + 1e-9)


def _bands(env):
    env = np.asarray(env, dtype=np.float64)
    return env[None, :] if env.ndim == 1 else env


def xcorr(ref, src, max_lag_s: float, fr=FR):
    """Lag (s) maximising sum_b ref_b[n + lag] * src_b[n], i.e. ref_time = src_time + lag.
    Returns (lag, confidence, peak_ratio): confidence = z-score of the peak among all
    candidate lags; peak_ratio = peak over the best lag at least 60 ms away."""
    from scipy.signal import correlate
    A, B = _z(_bands(ref)), _z(_bands(src))
    c = sum(correlate(a, b, mode="full", method="fft") for a, b in zip(A, B))
    c = c / max(1, min(A.shape[-1], B.shape[-1])) / len(A)
    lags = np.arange(-(B.shape[-1] - 1), A.shape[-1])
    m = int(round(max_lag_s * fr))
    sel = (lags >= -m) & (lags <= m)
    c, lags = c[sel], lags[sel]
    if not len(c):
        return 0.0, 0.0, 0.0
    i = int(np.argmax(c))
    peak = c[i]
    frac = 0.0       # sub-frame peak via parabola
    if 0 < i < len(c) - 1:
        d = c[i - 1] - 2 * c[i] + c[i + 1]
        if d < 0:
            frac = 0.5 * (c[i - 1] - c[i + 1]) / d
    lag = (lags[i] + frac) / fr
    conf = float((peak - c.mean()) / (c.std() + 1e-9))
    far = np.abs(lags - lags[i]) > int(0.06 * fr)
    second = c[far].max() if far.any() else 0.0
    ratio = float(peak / second) if second > 1e-9 else float("inf")
    return float(lag), conf, ratio


def _ref_slice(env, r0, length):
    R = _bands(env)
    out = np.zeros((R.shape[0], length))
    lo, hi = max(0, r0), min(R.shape[-1], r0 + length)
    if hi > lo:
        out[:, lo - r0:hi - r0] = R[:, lo:hi]
    return out


def _norm_corr(ref_seg, seg):
    """Band-averaged normalised correlation of seg (B, w) over every offset in ref_seg (B, w + L)."""
    from scipy.signal import correlate
    w = seg.shape[-1]
    tot, used = 0.0, 0
    for rb, sb in zip(ref_seg, seg):
        if sb.std() < 1e-6:
            continue
        c = correlate(rb, _z(sb), mode="valid", method="fft")
        e = np.sqrt(np.convolve(rb ** 2, np.ones(w), mode="valid")) + 1e-9
        tot = tot + c / e
        used += 1
    return None if not used else tot / used


@dataclass
class Alignment:
    method: str                     # "offset" | "warp"
    offset: float                   # global lag (s): audio_time = chart_time + offset
    confidence: float
    peak_ratio: float
    local: list = field(default_factory=list)    # [(time, lag, conf)]
    drift: float = 0.0              # max-min of confident local lags (s)
    warp_pairs: list | None = None  # [(chart_time, audio_time)] for method == "warp"
    snap: float = 0.0               # final onset nudge added on top of warp()
    rate: float = 1.0               # tempo ratio found by the warp

    def warp(self):
        if self.method == "warp" and self.warp_pairs:
            ct = np.array([p[0] for p in self.warp_pairs])
            at = np.array([p[1] for p in self.warp_pairs])
            return _pw_linear(ct, at)
        off = self.offset
        return lambda t: t + off

    def summary(self) -> dict:
        return {"method": self.method, "offset": round(self.offset + self.snap, 4),
                "xcorr_offset": round(self.offset, 4), "confidence": round(self.confidence, 2),
                "peak_ratio": round(min(self.peak_ratio, 99.0), 2), "drift": round(self.drift, 4),
                "snap": round(self.snap, 4), "rate": round(self.rate, 4), "windows": len(self.local)}


def _pw_linear(xs, ys):
    xs, ys = np.asarray(xs, float), np.asarray(ys, float)

    def f(t):
        t = float(t)
        if len(xs) == 1:
            return ys[0] + (t - xs[0])
        if t <= xs[0]:
            r = (ys[1] - ys[0]) / (xs[1] - xs[0])
            return float(ys[0] + (t - xs[0]) * r)
        if t >= xs[-1]:
            r = (ys[-1] - ys[-2]) / (xs[-1] - xs[-2])
            return float(ys[-1] + (t - xs[-1]) * r)
        return float(np.interp(t, xs, ys))
    return f


def local_lags(ref_env, src_env, base_lag, win_s=24.0, hop_s=12.0, search_s=0.6, fr=FR, min_conf=2.5):
    """Windowed lags (after shifting src by base_lag) -> ([(centre_time_in_src, total_lag, conf)], drift)."""
    out = []
    S = _bands(src_env)
    n = S.shape[-1]
    w, h = int(win_s * fr), int(hop_s * fr)
    shift = int(round(base_lag * fr))
    pad = int(search_s * fr) + 2
    for s0 in range(0, max(1, n - w // 2), h):
        seg = S[:, s0:s0 + w]
        if seg.shape[-1] < w // 2 or seg.std() < 1e-6:
            continue
        r0 = s0 + shift - pad
        if r0 < 0 or r0 + seg.shape[-1] + 2 * pad > _bands(ref_env).shape[-1]:
            continue
        ref = _ref_slice(ref_env, r0, seg.shape[-1] + 2 * pad)
        lag, conf, _ = xcorr(ref, seg, search_s + pad / fr, fr)
        lag = lag - pad / fr        # back to "relative to base_lag"
        if abs(lag) > search_s:
            continue
        out.append(((s0 + seg.shape[-1] / 2) / fr, base_lag + lag, conf))
    good = [l for _, l, c in out if c >= min_conf]
    return out, (max(good) - min(good) if len(good) >= 2 else 0.0)


def window_votes(ref_env, src_env, max_lag_s=60.0, win_s=12.0, hop_s=6.0, fr=FR):
    """Each window of the source correlated on its own over the full lag range ->
    [(lag, conf)].  Robust to drift, which smears a whole-song correlation."""
    S = _bands(src_env)
    w, h, m = int(win_s * fr), int(hop_s * fr), int(max_lag_s * fr)
    out = []
    for s0 in range(0, max(1, S.shape[-1] - w + 1), h):
        seg = S[:, s0:s0 + w]
        if seg.std() < 1e-6:
            continue
        c = _norm_corr(_ref_slice(ref_env, s0 - m, w + 2 * m), seg)
        if c is None:
            continue
        k = int(np.argmax(c))
        out.append(((k - m) / fr, float((c[k] - c.mean()) / (c.std() + 1e-9))))
    return out


def vote_offset(votes, spread_s=0.6):
    """Lag at the densest cluster of window votes (confidence-weighted), or None."""
    if not votes:
        return None, 0.0
    lags = np.array([l for l, _ in votes])
    wts = np.array([max(c, 0.0) for _, c in votes])
    best, best_w = None, 0.0
    for l in lags:
        sel = np.abs(lags - l) <= spread_s
        if wts[sel].sum() > best_w:
            best_w, best = wts[sel].sum(), float(np.median(lags[sel]))
    return best, best_w / (wts.sum() + 1e-9)


def global_offset(ref_env, src_env, max_lag_s=60.0, fr=FR) -> Alignment:
    lag, conf, ratio = xcorr(ref_env, src_env, max_lag_s, fr)
    vlag, share = vote_offset(window_votes(ref_env, src_env, max_lag_s, fr=fr))
    if vlag is not None and abs(vlag - lag) > 0.6 and share >= 0.25:
        # the whole-song peak disagrees with what most windows say: drift or an edit.
        # Trust the windows, and mark the correlation weak so the beat-level warp runs.
        lag, conf, ratio = vlag, 0.0, 1.0
    loc, drift = local_lags(ref_env, src_env, lag, fr=fr)
    return Alignment("offset", lag, conf, ratio, loc, drift)


# ── warp fallback (dynamic-programming lag tracking) ────────────────────────

def lag_track(ref_env, src_env, base_lag, *, win_s=4.0, hop_s=1.0, span_s=None, step_frames=2,
              jump_cost=1.0, jump_scale=0.05, jump_free=0.1, jump_cap=25.0, fr=FR):
    """Time-varying lag by dynamic programming (a DTW over lag space).

    For every window of the source envelope, the normalised cross-correlation with our
    envelope is scored on a lag grid (base_lag +- span_s).  A Viterbi pass picks one lag
    per window maximising total score minus a transition cost between neighbouring windows:
    ``|dlag| / jump_scale`` for drift-sized moves (<= ``jump_free``), a flat ``jump_cap`` for
    anything bigger.  Slow tempo drift is nearly free; a jump (an edit / cut section) needs
    many windows of evidence, and a small beat-ambiguous jump is no cheaper than a real one.  Returns [(src_time, lag)]
    at window centres.

    Plain log-mel DTW (as gp_to_sloppak uses for tabs vs. audio) put 10% of beats >250 ms
    off on synthetic drum stems, whose frames all look alike; correlating whole windows of
    band-wise onsets doesn't have that problem."""
    S = _bands(src_env)
    dur = S.shape[-1] / fr
    span = span_s if span_s is not None else max(6.0, 0.04 * dur)
    w, h = int(win_s * fr), int(hop_s * fr)
    m = int(span * fr)
    b0 = int(round(base_lag * fr))
    lags = np.arange(-m, m + 1, step_frames)
    rows, centres = [], []
    for s0 in range(0, max(1, S.shape[-1] - w + 1), h):
        seg = S[:, s0:s0 + w]
        if seg.std() < 1e-6:
            continue
        c = _norm_corr(_ref_slice(ref_env, s0 + b0 - m, w + 2 * m), seg)
        if c is None:
            continue
        c = c[lags + m]
        rows.append((c - c.mean()) / (c.std() + 1e-9))
        centres.append((s0 + w / 2) / fr)
    if not rows:
        return [(0.0, base_lag)]
    Sc = np.array(rows, dtype=np.float32)
    lag_s = (lags + b0) / fr
    d = np.abs(lag_s[:, None] - lag_s[None, :])
    # drift-sized moves cost in proportion; any real jump costs the same flat cap, so a
    # beat-ambiguous small jump is no cheaper than the true (larger) edit
    pen = (jump_cost * np.where(d <= jump_free, d / jump_scale, jump_cap)).astype(np.float32)
    acc = Sc[0].copy()
    back = np.zeros(Sc.shape, np.int32)
    for i in range(1, len(Sc)):
        tot = acc[None, :] - pen               # [to, from]
        back[i] = np.argmax(tot, axis=1)
        acc = tot[np.arange(len(lags)), back[i]] + Sc[i]
    k = int(np.argmax(acc))
    path = [k]
    for i in range(len(Sc) - 1, 0, -1):
        k = back[i][k]
        path.append(k)
    path.reverse()
    return [(c, float(lag_s[k])) for c, k in zip(centres, path)]


def refine_beats(beats, coarse, chart_hits, ref_env, max_shift=0.08):
    """Nudge each beat (+-max_shift) so the chart hits around it land on our onset peaks
    (gp_to_sloppak's refine_with_onsets), median-smoothed, kept increasing."""
    from scipy.ndimage import maximum_filter1d, median_filter
    beats, coarse = np.asarray(beats, float), np.asarray(coarse, float)
    envm = maximum_filter1d(env1d(ref_env), size=3)
    hits = np.asarray(sorted(chart_hits), float)
    shifts = np.arange(-max_shift, max_shift + 1e-9, 1 / FR)
    w0 = _pw_linear(beats, coarse)
    wh = np.array([w0(t) for t in hits])
    best = np.zeros(len(beats))
    conf = np.zeros(len(beats))
    for i in range(len(beats)):
        lo, hi = beats[max(0, i - 4)], beats[min(len(beats) - 1, i + 4)]
        sel = wh[(hits >= lo) & (hits <= hi)]
        if len(sel) < 3:
            continue
        sc = []
        for s in shifts:
            ix = np.round((sel + s) * FR).astype(int)
            ix = ix[(ix >= 0) & (ix < len(envm))]
            sc.append(envm[ix].mean() if len(ix) else 0.0)
        sc = np.asarray(sc)
        sc = sc - 0.15 * np.abs(shifts) / max_shift * sc.std()
        j = int(np.argmax(sc))
        best[i], conf[i] = shifts[j], (sc[j] - np.median(sc)) / (sc.std() + 1e-9)
    good = conf > 1.0
    if good.sum() >= 2:
        ii = np.arange(len(best))
        best = np.interp(ii, ii[good], best[good])
    best = median_filter(best, size=5, mode="nearest")
    out = coarse + best
    for i in range(1, len(out)):
        out[i] = max(out[i], out[i - 1] + 0.02)
    return out


def resample_env(env, rate, fr=FR):
    """env re-timed so that source time x lands at rate * x."""
    E = _bands(env)
    n = int(E.shape[-1] * rate)
    x = np.arange(n) / rate
    return np.array([np.interp(x, np.arange(E.shape[-1]), e) for e in E], dtype=np.float32)


def track_rate(track):
    """Robust tempo ratio from a lag track [(t, lag)]: Theil-Sen slope of lag vs time
    over the longest run without jumps.  Returns 1 + slope (1.0 when there's no evidence)."""
    if len(track) < 6:
        return 1.0
    t = np.array([c for c, _ in track])
    l = np.array([x for _, x in track])
    # split at jumps (> 0.3 s between neighbours) and use the longest segment
    cuts = np.where(np.abs(np.diff(l)) > 0.3)[0] + 1
    segs = np.split(np.arange(len(t)), cuts)
    seg = max(segs, key=len)
    if len(seg) < 6:
        return 1.0
    ts, ls = t[seg], l[seg]
    i, j = np.triu_indices(len(ts), 1)
    ok = ts[j] - ts[i] > 4.0
    if not ok.any():
        return 1.0
    slope = float(np.median((ls[j] - ls[i])[ok] / (ts[j] - ts[i])[ok]))
    return 1.0 + slope if abs(slope) > 0.003 else 1.0


LAST_RATE: dict = {}


def warp_pairs(ref_env, src_env, chart_beats, chart_hits, base_lag, onset_ref_env=None) -> list[tuple]:
    """(chart_time, audio_time) at every chart beat: global rate, lag tracking, per-beat refinement."""
    from scipy.ndimage import median_filter
    track = lag_track(ref_env, src_env, base_lag)
    rate = track_rate(track)
    LAST_RATE["rate"] = rate
    if rate != 1.0:         # different tempo: re-time the chart envelope and track the residual
        track = lag_track(ref_env, resample_env(src_env, rate), base_lag)
    tc = np.array([c for c, _ in track]) / rate          # back to chart time
    tl = np.array([c + l for c, l in track]) - tc         # ours - chart at each window
    beats = np.asarray(chart_beats, float)
    if len(tl) >= 5:
        # despike, then a sliding linear fit over +-3 windows (tempo drifts smoothly)
        tl = median_filter(tl, size=3, mode="nearest")
        sm = tl.copy()
        for i in range(len(tl)):
            a, b = max(0, i - 3), min(len(tl), i + 4)
            sm[i] = np.polyval(np.polyfit(tc[a:b], tl[a:b], 1), tc[i])
        tl = sm
        # beyond the first/last window centre: continue the end slopes, not a flat lag
        k = min(4, len(tc))
        p0 = np.polyfit(tc[:k], tl[:k], 1)
        p1 = np.polyfit(tc[-k:], tl[-k:], 1)
        lag_at = np.interp(beats, tc, tl)
        lag_at = np.where(beats < tc[0], np.polyval(p0, beats), lag_at)
        lag_at = np.where(beats > tc[-1], np.polyval(p1, beats), lag_at)
    else:
        lag_at = np.interp(beats, tc, tl)
    coarse = beats + lag_at
    for i in range(1, len(coarse)):
        coarse[i] = max(coarse[i], coarse[i - 1] + 0.02)
    out = refine_beats(beats, coarse, chart_hits, onset_ref_env if onset_ref_env is not None else ref_env)
    return list(zip(beats.tolist(), out.tolist()))


def snap_correction(note_times, onsets, window=0.04, min_matched=0.3) -> float:
    """Median (onset - note) over notes with an onset within +-window; 0 when too few match.
    Envelope cross-correlation is only good to a few ms (flux peaks depend on timbre); this
    final nudge puts notes on the detected attacks."""
    nt = np.unique(np.round(np.asarray(note_times, float), 3))
    on = np.asarray(onsets, float)
    if len(nt) == 0 or len(on) < 2:
        return 0.0
    idx = np.clip(np.searchsorted(on, nt), 1, len(on) - 1)
    left, right = on[idx - 1], on[idx]
    near = np.where(np.abs(left - nt) <= np.abs(right - nt), left, right)
    err = near - nt
    m = np.abs(err) <= window
    if m.mean() < min_matched:
        return 0.0
    return float(np.median(err[m]))


# ── validation ──────────────────────────────────────────────────────────────

def detect_onsets(y, sr=SR) -> np.ndarray:
    """Drum onsets (s).  Spectral-flux peaks land ~10 ms after the attack, so each one is
    pulled back to where the waveform envelope first reaches 30% of its local peak."""
    import librosa
    from scipy.ndimage import maximum_filter1d
    hop = 128
    env = librosa.onset.onset_strength(y=y, sr=sr, hop_length=hop, max_size=1)
    on = librosa.onset.onset_detect(onset_envelope=env, sr=sr, hop_length=hop, units="samples",
                                    backtrack=False, delta=0.05, wait=2)
    amp = maximum_filter1d(np.abs(y), size=max(1, int(0.001 * sr)))
    out = []
    pre, post = int(0.03 * sr), int(0.015 * sr)
    for o in on:
        a, b = max(0, o - pre), min(len(amp), o + post)
        seg = amp[a:b]
        if not len(seg):
            continue
        k = int(np.argmax(seg))
        floor = seg[:k + 1].min() if k else seg[0]
        thr = floor + 0.3 * (seg[k] - floor)
        j = k
        while j > 0 and seg[j - 1] >= thr:
            j -= 1
        out.append((a + j) / sr)
    return np.unique(np.round(np.asarray(out, float), 4))


@dataclass
class Validation:
    n_notes: int
    n_onsets: int
    median_offset: float        # median(onset - note) over matched notes (s)
    within_30ms: float          # fraction of notes with an onset within +-30 ms (after nothing)
    within_30ms_centered: float  # same after removing the median offset
    drift_ms_per_min: float     # slope of the error over time
    drift_span: float           # max-min of per-quarter median errors (s)
    quarters: list
    ok: bool
    reasons: list
    pads: dict = field(default_factory=dict)   # {"kick": frac within 30 ms of a low-band onset, "cymbal": ...}

    def summary(self) -> dict:
        return {"notes": self.n_notes, "onsets": self.n_onsets, "median_offset_ms": round(self.median_offset * 1000, 1),
                "within_30ms": round(self.within_30ms, 3), "within_30ms_centered": round(self.within_30ms_centered, 3),
                "drift_ms_per_min": round(self.drift_ms_per_min, 1), "drift_span_ms": round(self.drift_span * 1000, 1),
                "quarters_ms": [None if q is None else round(q * 1000, 1) for q in self.quarters],
                "pads_within_30ms": {k: round(v, 3) for k, v in self.pads.items()},
                "ok": self.ok, "reasons": self.reasons}


THRESHOLDS = {"min_within_30ms": 0.5, "max_abs_median_ms": 25.0, "max_drift_span_ms": 40.0,
              "min_pad_within_30ms": 0.35, "min_kick_within_30ms": 0.5}


def band_onsets(y, sr=SR) -> dict:
    """Onsets in the kick band (< 150 Hz) and the cymbal band (> 5 kHz) of a drum stem."""
    from scipy.signal import butter, sosfiltfilt
    lo = sosfiltfilt(butter(4, 150 / (sr / 2), "low", output="sos"), y).astype(np.float32)
    hi = sosfiltfilt(butter(4, 5000 / (sr / 2), "high", output="sos"), y).astype(np.float32)
    return {"kick": detect_onsets(lo, sr), "cymbal": detect_onsets(hi, sr)}


def _within(nt, on, win=0.03):
    nt, on = np.asarray(nt, float), np.asarray(on, float)
    if not len(nt) or not len(on):
        return 0.0
    idx = np.clip(np.searchsorted(on, nt), 1, max(1, len(on) - 1))
    left, right = on[np.maximum(idx - 1, 0)], on[np.minimum(idx, len(on) - 1)]
    return float(np.mean(np.minimum(np.abs(left - nt), np.abs(right - nt)) <= win))


def pad_check(hits, bands: dict, min_notes=16) -> dict:
    """Fraction of kick notes near a low-band onset and of cymbal notes near a high-band
    onset.  Catches a chart that's a whole beat off on a steady groove: the overall
    onset check passes (hats are everywhere) but the kicks no longer line up."""
    out = {}
    kicks = sorted({round(t, 3) for t, pad, _ in hits if pad == "kick"})
    cyms = sorted({round(t, 3) for t, pad, cym in hits if cym})
    if len(kicks) >= min_notes and "kick" in bands:
        out["kick"] = _within(kicks, bands["kick"])
    if len(cyms) >= min_notes and "cymbal" in bands:
        out["cymbal"] = _within(cyms, bands["cymbal"])
    return out


def validate(note_times, onsets, thresholds=None, pad_hits=None, bands=None) -> Validation:
    """pad_hits [(time, pad, cymbal)] + bands (band_onsets of the drum stem) add the per-pad check."""
    th = {**THRESHOLDS, **(thresholds or {})}
    nt = np.unique(np.round(np.asarray(note_times, float), 3))    # chords count once
    on = np.asarray(onsets, float)
    if not len(nt) or not len(on):
        return Validation(len(nt), len(on), 0.0, 0.0, 0.0, 0.0, 0.0, [], False, ["no notes or no onsets"])
    idx = np.clip(np.searchsorted(on, nt), 1, len(on) - 1) if len(on) > 1 else np.zeros(len(nt), int)
    if len(on) > 1:
        left, right = on[idx - 1], on[idx]
        near = np.where(np.abs(left - nt) <= np.abs(right - nt), left, right)
    else:
        near = np.full(len(nt), on[0])
    err = near - nt
    matched = np.abs(err) <= 0.1
    med = float(np.median(err[matched])) if matched.any() else float(np.median(err))
    w30 = float(np.mean(np.abs(err) <= 0.03))
    w30c = float(np.mean(np.abs(err - med) <= 0.03))
    slope, quarters = 0.0, []
    if matched.sum() >= 8:
        slope = float(np.polyfit(nt[matched], err[matched], 1)[0]) * 60 * 1000
    t0, t1 = nt.min(), nt.max()
    for q in range(4):
        a, b = t0 + (t1 - t0) * q / 4, t0 + (t1 - t0) * (q + 1) / 4 + 1e-9
        sel = matched & (nt >= a) & (nt <= b)
        quarters.append(float(np.median(err[sel])) if sel.sum() >= 4 else None)
    qs = [q for q in quarters if q is not None]
    span = (max(qs) - min(qs)) if len(qs) >= 2 else 0.0
    reasons = []
    if w30 < th["min_within_30ms"]:
        reasons.append(f"only {w30:.0%} of notes within 30 ms of a drum onset (< {th['min_within_30ms']:.0%})")
    if abs(med) * 1000 > th["max_abs_median_ms"]:
        reasons.append(f"median offset {med * 1000:+.0f} ms")
    if span * 1000 > th["max_drift_span_ms"]:
        reasons.append(f"drifts {span * 1000:.0f} ms across the song")
    pads = pad_check(pad_hits, bands) if pad_hits is not None and bands else {}
    # Kicks are the reliable signal on real stems (90-99% on good joins); a cymbal wash hides
    # cymbal onsets (Helter Skelter: kicks 95%, cymbals 29%), so cymbals only gate when
    # there aren't enough kicks to check.
    if "kick" in pads:
        if pads["kick"] < th["min_kick_within_30ms"]:
            reasons.append(f"only {pads['kick']:.0%} of kick notes line up with kick-band onsets (a beat off?)")
    elif "cymbal" in pads and pads["cymbal"] < th["min_pad_within_30ms"]:
        reasons.append(f"only {pads['cymbal']:.0%} of cymbal notes line up with cymbal-band onsets (a beat off?)")
    return Validation(len(nt), len(on), med, w30, w30c, slope, span, quarters, not reasons, reasons, pads)

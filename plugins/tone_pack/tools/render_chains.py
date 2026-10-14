"""Offline level / tone check for tone_pack presets (dev tool; needs torch, numpy, scipy, soundfile).

Plays a synthetic guitar DI through NAM captures (a WaveNet forward pass written from the .nam
format, same structure as NeuralAmpModelerCore's WaveNet) and cab IRs (normalised like the engine's
JUCE Convolution: energy -> 0.125), then measures integrated loudness (BS.1770 K-weighting) and the
spectrum. Used to level-match every Auto chain to Main Lead and to pick the cab mic whose spectrum is
closest to Main Lead's.

  python -I render_chains.py CONFIG_DIR REFERENCE.nam OUT.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import soundfile as sf
import torch
from scipy.signal import fftconvolve, lfilter, resample_poly

SR = 48000


# ── NAM WaveNet ──────────────────────────────────────────────────────────────

class _W:
    def __init__(self, w):
        self.w, self.i = w, 0

    def take(self, n, shape=None):
        a = torch.tensor(self.w[self.i:self.i + n], dtype=torch.float32)
        self.i += n
        return a.reshape(shape) if shape else a


def _act(name):
    return {"Tanh": torch.tanh, "ReLU": torch.relu, "Sigmoid": torch.sigmoid,
            "Hardtanh": torch.nn.functional.hardtanh,
            "LeakyReLU": torch.nn.functional.leaky_relu}.get(name, torch.tanh)


def load_wavenet(path: Path):
    m = json.loads(Path(path).read_text(encoding="utf-8"))
    if m.get("architecture") != "WaveNet":
        raise ValueError(f"{path.name}: {m.get('architecture')} not supported")
    cfg, w = m["config"], _W(m["weights"])
    arrays = []
    for la in cfg["layers"]:
        ch, k, cin, cond, hs = la["channels"], la["kernel_size"], la["input_size"], la["condition_size"], la["head_size"]
        gated = la.get("gated", False)
        mult = 2 if gated else 1
        arr = {"rechannel": w.take(ch * cin, (ch, cin, 1)), "layers": [], "act": _act(la.get("activation", "Tanh")),
               "gated": gated, "ch": ch}
        for d in la["dilations"]:
            arr["layers"].append({
                "d": d,
                "conv_w": w.take(mult * ch * ch * k, (mult * ch, ch, k)), "conv_b": w.take(mult * ch),
                "mix_w": w.take(mult * ch * cond, (mult * ch, cond, 1)),
                "o_w": w.take(ch * ch, (ch, ch, 1)), "o_b": w.take(ch),
            })
        arr["head_w"] = w.take(hs * ch, (hs, ch, 1))
        arr["head_b"] = w.take(hs) if la.get("head_bias") else None
        arrays.append(arr)
    head_scale = float(w.take(1)[0])
    if w.i != len(m["weights"]):
        raise ValueError(f"{path.name}: weight count mismatch {w.i} != {len(m['weights'])}")
    sr = m.get("sample_rate") or 48000

    def run(x: np.ndarray) -> np.ndarray:
        F = torch.nn.functional
        with torch.no_grad():
            src = x if sr == SR else resample_poly(x, int(sr), SR)
            c = torch.tensor(src, dtype=torch.float32)[None, None, :]
            y, head = c, None
            for arr in arrays:
                h = F.conv1d(y, arr["rechannel"])
                for L in arr["layers"]:
                    pad = (L["conv_w"].shape[2] - 1) * L["d"]
                    z = F.conv1d(F.pad(h, (pad, 0)), L["conv_w"], L["conv_b"], dilation=L["d"])
                    z = z + F.conv1d(c, L["mix_w"])
                    if arr["gated"]:
                        post = torch.tanh(z[:, :arr["ch"]]) * torch.sigmoid(z[:, arr["ch"]:])
                    else:
                        post = arr["act"](z)
                    head = post if head is None or head.shape[1] != post.shape[1] else head + post
                    h = h + F.conv1d(post, L["o_w"], L["o_b"])
                head = F.conv1d(head, arr["head_w"], arr["head_b"])
                y = h
            out = (head_scale * head)[0, 0].numpy()
        return out if sr == SR else resample_poly(out, SR, int(sr))
    return run


# ── signal, IR, measurement ──────────────────────────────────────────────────

def guitar_di(seconds=12.0, seed=1) -> np.ndarray:
    """Karplus-Strong strums of power chords + single notes, peaking around -6 dBFS like a DI."""
    rng = np.random.default_rng(seed)
    out = np.zeros(int(SR * seconds))
    roots = [82.4, 110.0, 98.0, 146.8, 123.5, 87.3]
    t = 0.0
    while t < seconds - 1.0:
        f0 = rng.choice(roots)
        notes = [f0, f0 * 1.4983, f0 * 2] if rng.random() < 0.7 else [f0 * rng.choice([2, 3, 4])]
        dur = rng.choice([0.25, 0.5, 0.5, 1.0])
        for k, f in enumerate(notes):
            n = int(SR / f)
            buf = rng.uniform(-1, 1, n)
            seg = np.empty(int(SR * (dur + 0.6)))
            for i in range(len(seg)):
                seg[i] = buf[i % n]
                buf[i % n] = 0.996 * 0.5 * (buf[i % n] + buf[(i + 1) % n])
            s = int(SR * (t + k * 0.008))
            e = min(len(out), s + len(seg))
            out[s:e] += seg[:e - s] * 0.6
        t += dur
    return out / np.max(np.abs(out)) * 0.5


def load_ir(path: Path) -> np.ndarray:
    h, sr = sf.read(str(path))
    h = h if h.ndim == 1 else h[:, 0]
    if sr != SR:
        h = resample_poly(h, SR, sr)
    return h * 0.125 / np.sqrt(np.sum(h ** 2))       # JUCE Convolution Normalise::yes


def _biquad(b, a, x):
    return lfilter(b, a, x)


def lufs(x: np.ndarray) -> float:
    """BS.1770 integrated loudness (K-weighting, 400 ms blocks, absolute + relative gates)."""
    # stage 1 (high shelf) and 2 (high pass) at 48 kHz
    x = _biquad([1.53512485958697, -2.69169618940638, 1.19839281085285], [1.0, -1.69065929318241, 0.73248077421585], x)
    x = _biquad([1.0, -2.0, 1.0], [1.0, -1.99004745483398, 0.99007225036621], x)
    blk, hop = int(0.4 * SR), int(0.1 * SR)
    ms = np.array([np.mean(x[i:i + blk] ** 2) for i in range(0, len(x) - blk, hop)])
    ms = ms[ms > 0]
    lk = -0.691 + 10 * np.log10(ms)
    ms = ms[lk > -70]
    rel = -0.691 + 10 * np.log10(np.mean(ms)) - 10
    ms = ms[-0.691 + 10 * np.log10(ms) > rel]
    return float(-0.691 + 10 * np.log10(np.mean(ms)))


BANDS = [(60, 150), (150, 400), (400, 1000), (1000, 2500), (2500, 6000), (6000, 12000)]


def spectrum(x: np.ndarray) -> list[float]:
    """Energy per band in dB relative to the total (the tone's balance, level-independent)."""
    X = np.abs(np.fft.rfft(x)) ** 2
    f = np.fft.rfftfreq(len(x), 1 / SR)
    tot = X[(f >= 60) & (f < 12000)].sum()
    return [float(10 * np.log10(X[(f >= a) & (f < b)].sum() / tot)) for a, b in BANDS]


def render(stages, di):
    y = di
    for kind, path in stages:
        y = load_wavenet(path)(y) if kind == "NAM" else fftconvolve(y, load_ir(path))[:len(y)]
    return y


if __name__ == "__main__":
    cfg, ref, out = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
    di = guitar_di()
    ref_y = render([("NAM", ref)], di)
    res = {"reference": {"lufs": lufs(ref_y), "spectrum": spectrum(ref_y)}}
    print("reference", res["reference"])
    json.dump(res, open(out, "w"), indent=1)

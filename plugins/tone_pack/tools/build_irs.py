"""Build the tone pack's matched cab IRs and level table (dev tool; torch/numpy/scipy/soundfile).

For each category: take the chosen Rocksmith cab IR, add a smooth EQ (linear-phase FIR, at most
+-MAX_DB) that moves the chain's spectrum toward the reference's (Main Lead) by `amount`, save it
to CONFIG_DIR/nam_irs/tone_pack/<category>.wav (never shipped: they are
EQ'd copies of the Rocksmith cab IRs on this computer), then render the full chain (with the engine's IR
normalisation) and store the make-up gain that matches its loudness (BS.1770) to the reference.

  python -I build_irs.py CONFIG_DIR REFERENCE.nam
"""
import json
import sys
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.signal import fftconvolve, firwin2

sys.path.insert(0, str(Path(__file__).resolve().parent))
from render_chains import BANDS, SR, guitar_di, load_ir, load_wavenet, lufs, spectrum  # noqa: E402

PLUGIN = Path(__file__).resolve().parent.parent
MAX_DB = 8.0

# category: (NAM stages before the cab, rocksmith cab IR, how far to EQ toward the reference)
PLAN = {
    "clean": (["amps/Tim R Fender Twin Reverb Ch1 BR G06.nam"], "cab_hg212c_04.wav", 0.5),
    "od": (["amps/Marshall DSL 40 C Crunch.nam"], "cab_orangeppc412_00.wav", 1.0),
    "dist": (["other/5153.nam"], "cab_en212c_08.wav", 1.0),
    "solo": (["amps/Mesa Boogie Mark V Lead V2.2 Amp Only.nam"], "cab_orangeppc212ob_04.wav", 1.0),
    "mod": (["pedals/tone3000_26327_m99790_Pedal_Chorus.nam", "amps/Tim R Fender Twin Reverb Ch1 BR G06.nam"],
            "cab_hg212c_04.wav", 0.5),
    "bass": (["amps/SVT CLEAN.nam"], None, 0.0),             # cab picked by low end below
    "acoustic": (["pedals/tone3000_29201_m125801_Pedal_AcousticEmulator.nam"], "", 0.0),   # no cab
}


def eq_fir(gains_db, n=2047):
    """Linear-phase FIR through the band gains (interpolated on log frequency)."""
    centers = [np.sqrt(a * b) for a, b in BANDS]
    f = np.concatenate([[0.0], np.geomspace(20, SR / 2 - 1, 200), [SR / 2]])
    g_db = np.interp(np.log(np.maximum(f, 20)), np.log(centers), gains_db)
    return firwin2(n, f / (SR / 2), 10 ** (g_db / 20))


def run_chain(stages_y, ir):
    return fftconvolve(stages_y, ir * 0.125 / np.sqrt(np.sum(ir ** 2)))[:len(stages_y)] if ir is not None else stages_y


if __name__ == "__main__":
    cfg, ref_path = Path(sys.argv[1]), Path(sys.argv[2])
    models, rs = cfg / "nam_models", cfg / "nam_irs" / "rocksmith"
    di = guitar_di()
    ref = load_wavenet(ref_path)(di)
    ref_l, ref_s = lufs(ref), np.array(spectrum(ref))
    pack_irs = cfg / "nam_irs" / "tone_pack"
    pack_irs.mkdir(parents=True, exist_ok=True)
    table = {"reference_lufs": round(ref_l, 2), "categories": {}}
    for cat, (stages, cab, amount) in PLAN.items():
        y = di
        for p in stages:
            y = load_wavenet(models / p)(y)
        if cab is None:      # bass: the bass cab / mic with the most low end
            best = None
            for f in sorted(rs.glob("bass_cab_*.wav")):
                s = spectrum(run_chain(y, load_ir(f)))
                score = s[0] + s[1] - 0.5 * s[5]
                if best is None or score > best[0]:
                    best = (score, f)
            cab = best[1].name
        if cab:
            raw, sr = sf.read(str(rs / cab))
            raw = raw if raw.ndim == 1 else raw[:, 0]
            before = np.array(spectrum(run_chain(y, raw)))
            corr = np.clip((ref_s - before) * amount, -MAX_DB, MAX_DB)
            ir = fftconvolve(raw, eq_fir(corr))
            ir = ir / np.max(np.abs(ir)) * 0.9
            out_ir = pack_irs / f"{cat}.wav"
            sf.write(str(out_ir), ir.astype(np.float32), SR, subtype="FLOAT")
            z = run_chain(y, ir)
        else:
            corr, out_ir, before, z = None, None, None, y
        lz, sz = lufs(z), spectrum(z)
        table["categories"][cat] = {
            "source_ir": cab or None, "ir": out_ir.name if out_ir else None,
            "eq_db": None if corr is None else [round(float(c), 1) for c in corr],
            "chain_lufs": round(lz, 2), "makeup_db": round(ref_l - lz, 2),
            "spectrum": [round(v, 1) for v in sz],
        }
        print(f"{cat:8} {cab or '-':28} make-up {ref_l - lz:+5.1f} dB  spectrum {np.round(sz, 1)}"
              + ("" if before is None else f"  (was {np.round(before, 1)})"))
    print("reference spectrum", np.round(ref_s, 1))
    json.dump(table, open(PLUGIN / "levels.json", "w"), indent=1)

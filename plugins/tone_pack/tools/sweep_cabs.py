"""For each tone_pack recipe: render amp (+ pre stages) through candidate cab IRs / mics, measure
loudness and spectral distance to the reference (Main Lead), print the best matches.

  python -I sweep_cabs.py CONFIG_DIR REFERENCE.nam OUT.json
"""
import json
import sys
from pathlib import Path

import numpy as np
from scipy.signal import fftconvolve

sys.path.insert(0, str(Path(__file__).resolve().parent))
from render_chains import guitar_di, load_ir, load_wavenet, lufs, spectrum  # noqa: E402

GUITAR_CABS = ["cab_marshall1960a", "cab_marshall1960ax", "cab_marshall1960tv", "cab_marshall1936",
               "cab_orangeppc412", "cab_orangeppc212ob", "cab_gb412cmkiii", "cab_en4120c", "cab_en212c",
               "cab_ca412c", "cab_ca212c", "cab_cs212c", "cab_tw112c", "cab_tw410c", "cab_bt410c", "cab_hg212c"]
BASS_CABS = ["bass_cab_at810bc", "bass_cab_bt410bc", "bass_cab_ch410bc", "bass_cab_edend410xst", "bass_cab_gb415bc"]

# (category, pre/amp NAM stages, cab family, post NAM stages)
CHAINS = {
    "clean": (["amps/Tim R Fender Twin Reverb Ch1 BR G06.nam"], GUITAR_CABS, []),
    "od": (["amps/Marshall DSL 40 C Crunch.nam"], GUITAR_CABS, []),
    "dist": (["other/5153.nam"], GUITAR_CABS, []),
    "solo": (["amps/Mesa Boogie Mark V Lead V2.2 Amp Only.nam"], GUITAR_CABS, []),
    "bass": (["amps/SVT CLEAN.nam"], BASS_CABS, []),
    "mod": (["pedals/tone3000_26327_m99790_Pedal_Chorus.nam", "amps/Tim R Fender Twin Reverb Ch1 BR G06.nam"],
            GUITAR_CABS, []),
}
# how much each band's mismatch counts (low end matters most for "thin")
W = np.array([2.0, 2.0, 1.2, 1.0, 1.0, 0.5])

if __name__ == "__main__":
    cfg, ref_path, out = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
    models, irs = cfg / "nam_models", cfg / "nam_irs" / "rocksmith"
    di = guitar_di()
    ref = load_wavenet(ref_path)(di)
    ref_l, ref_s = lufs(ref), np.array(spectrum(ref))
    print(f"reference {ref_l:.1f} LUFS spectrum {np.round(ref_s, 1)}")
    results = {"reference": {"lufs": ref_l, "spectrum": ref_s.tolist()}, "chains": {}}
    for cat, (pre, cabs, post) in CHAINS.items():
        y = di
        for p in pre:
            y = load_wavenet(models / p)(y)
        rows = []
        for cab in cabs:
            for mic in range(9):
                f = irs / f"{cab}_{mic:02d}.wav"
                if not f.exists():
                    continue
                z = fftconvolve(y, load_ir(f))[:len(y)]
                for p in post:
                    z = load_wavenet(models / p)(z)
                s = np.array(spectrum(z))
                rows.append({"ir": f.name, "lufs": lufs(z), "dist": float(np.sqrt(np.mean((W * (s - ref_s)) ** 2))),
                             "spectrum": s.tolist()})
        rows.sort(key=lambda r: r["dist"])
        results["chains"][cat] = rows
        cur = next((r for r in rows if r["ir"].startswith(("cab_tw112c_03", "cab_marshall1960a_03",
                                                            "cab_marshall1960ax_04", "bass_cab_at810bc_03"))), None)
        print(f"\n{cat}: best", [(r["ir"], round(r["dist"], 2), round(r["lufs"], 1)) for r in rows[:4]])
        if cur:
            print(f"  current {cur['ir']} dist {cur['dist']:.2f} lufs {cur['lufs']:.1f}  spectrum {np.round(cur['spectrum'], 1)}")
        print(f"  best spectrum {np.round(rows[0]['spectrum'], 1)}")
    json.dump(results, open(out, "w"), indent=1)
